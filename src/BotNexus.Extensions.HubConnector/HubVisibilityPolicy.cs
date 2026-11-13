using System.Text.Json;
using BotNexus.Domain.World;
using BotNexus.Gateway.Abstractions.Models;

namespace BotNexus.Extensions.HubConnector;

internal static class HubVisibilityPolicy
{
    public static bool AllowsAgent(HubConnectorOptions options, AgentDescriptor agent)
        => agent.Enabled != false && agent.Kind == AgentKind.Named
            && HubProtocol.IsRoutingId(agent.AgentId.Value, 128) && options.AllowedAgents.Contains(agent.AgentId.Value)
            && !Flag(agent, "hidden", true) && !Flag(agent, "visible", false);

    private static bool Flag(AgentDescriptor agent, string key, bool match)
    {
        if (!agent.Metadata.TryGetValue(key, out var value)) return false;
        return value switch
        {
            bool flag => flag == match,
            string text => bool.TryParse(text, out var flag) && flag == match,
            JsonElement element when element.ValueKind is JsonValueKind.True or JsonValueKind.False => element.GetBoolean() == match,
            _ => false
        };
    }

    public static bool AllowsConversation(Conversation conversation, string agentId, string conversationId)
        => conversation.AgentId.Value == agentId && conversation.ConversationId.Value == conversationId
            && conversation.Status == ConversationStatus.Active
            && conversation.Visibility == ConversationVisibility.UserFacing
            && conversation.ParentConversationId is null;
}

internal static class HubCheckpointProjection
{
    // Only the explicit presentation and routing fields are read. Session, tool, timeout and arbitrary JSON are never exported.
    public static HubQuestion? Project(string? json, Conversation conversation)
    {
        if (string.IsNullOrWhiteSpace(json) || json.Length > HubProtocol.MaxFrameBytes) return null;
        try
        {
            using var document = JsonDocument.Parse(json, new JsonDocumentOptions { MaxDepth = 16 });
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object) return null;
            var fields = new Dictionary<string, JsonElement>(StringComparer.OrdinalIgnoreCase);
            foreach (var property in root.EnumerateObject()) if (!fields.TryAdd(property.Name, property.Value)) return null;
            string? Text(string key) => fields.TryGetValue(key, out var value) && value.ValueKind == JsonValueKind.String ? value.GetString() : null;
            bool Flag(string key) => fields.TryGetValue(key, out var value) && value.ValueKind == JsonValueKind.True;
            var id = Text("requestId");
            if (!HubProtocol.IsRoutingId(id, 128) || Text("conversationId") != conversation.ConversationId.Value
                || Text("agentId") != conversation.AgentId.Value || string.IsNullOrWhiteSpace(Text("prompt"))) return null;
            var input = Text("inputType") switch
            {
                "FreeForm" or "free_form" => "free_form",
                "SingleChoice" or "single_choice" => "single_choice",
                "MultipleChoice" or "multiple_choice" => "multiple_choice",
                "ChoiceOrFreeForm" or "choice_or_free_form" => "choice_or_free_form",
                null => "free_form",
                _ => null
            };
            if (input is null) return null;
            var choices = new List<HubChoice>();
            if (fields.TryGetValue("choices", out var array) && array.ValueKind != JsonValueKind.Null)
            {
                if (array.ValueKind != JsonValueKind.Array) return null;
                foreach (var choice in array.EnumerateArray().Take(HubProtocol.MaxChoices))
                {
                    if (choice.ValueKind != JsonValueKind.Object) continue;
                    string? Read(string name) => choice.EnumerateObject().FirstOrDefault(p => p.Name.Equals(name, StringComparison.OrdinalIgnoreCase)).Value is var v
                        && v.ValueKind == JsonValueKind.String ? v.GetString() : null;
                    var value = Read("value");
                    // Never truncate a machine choice value into a different answer.
                    if (!HubProtocol.IsRoutingId(value, 128)) continue;
                    var label = Read("label");
                    choices.Add(new(value!, HubProtocol.Clamp(string.IsNullOrWhiteSpace(label) ? value : label, 160),
                        Read("description") is { } description ? HubProtocol.Clamp(description, 256) : null));
                }
            }
            return new(conversation.ConversationId.Value, conversation.AgentId.Value, HubProtocol.Clamp(conversation.Title, 200),
                id!, HubProtocol.Clamp(Text("prompt"), 4000), input, choices, Flag("allowMultiple"), Flag("allowFreeForm"));
        }
        catch (Exception ex) when (ex is JsonException or InvalidOperationException or ArgumentException)
        {
            return null;
        }
    }
}
