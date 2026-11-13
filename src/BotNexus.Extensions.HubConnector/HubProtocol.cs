using System.Text.Json;

namespace BotNexus.Extensions.HubConnector;

internal static class HubProtocol
{
    public const int MaxFrameBytes = 1024 * 1024;
    public const int MaxMessageCharacters = 32_768;
    public const int MaxChoices = 20;
    internal static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        MaxDepth = 16
    };

    public static HubCommandParse ParseCommand(ReadOnlyMemory<byte> bytes)
    {
        var id = "";
        if (bytes.Length > MaxFrameBytes) return new(null, HubResult.Rejected(id, "frame_too_large"));
        try
        {
            using var document = JsonDocument.Parse(bytes, new JsonDocumentOptions { MaxDepth = 16 });
            var root = document.RootElement;
            if (!HasOnly(root, ["type", "protocolVersion", "id", "action", "payload"])) throw new JsonException();
            var rawId = String(root, "id");
            if (!TryCommandId(rawId, out id)) throw new JsonException();
            if (String(root, "type") != "command") throw new JsonException();
            if (root.TryGetProperty("protocolVersion", out var version) && (!version.TryGetInt32(out var number) || number != 1))
                throw new JsonException();
            var action = String(root, "action");
            if (!KnownAction(action)) return new(null, HubResult.Rejected(id, "unknown_action"));
            if (!root.TryGetProperty("payload", out var payload)) throw new JsonException();
            string[] fields = action switch
            {
                "start_conversation" => ["agentId", "title", "message"],
                "send_message" => ["agentId", "conversationId", "message"],
                _ => ["agentId", "conversationId", "requestId", "text", "selectedValues", "cancelled"]
            };
            if (!HasOnly(payload, fields)) throw new JsonException();
            IReadOnlyList<string>? selections = null;
            if (payload.TryGetProperty("selectedValues", out var selected) && selected.ValueKind != JsonValueKind.Null)
            {
                if (selected.ValueKind != JsonValueKind.Array || selected.GetArrayLength() > MaxChoices) throw new JsonException();
                selections = selected.EnumerateArray().Select(item => item.ValueKind == JsonValueKind.String
                    ? item.GetString()! : throw new JsonException()).ToArray();
            }
            var cancelled = false;
            if (payload.TryGetProperty("cancelled", out var cancel)) cancelled = cancel.GetBoolean();
            var command = new HubCommand(id, action!, new HubCommandPayload(
                String(payload, "agentId") ?? "", String(payload, "conversationId"), String(payload, "title"),
                String(payload, "message"), String(payload, "requestId"), String(payload, "text"), selections, cancelled));
            var error = Validate(command);
            return error is null ? new(command, null) : new(null, HubResult.Rejected(id, error));
        }
        catch (Exception ex) when (ex is JsonException or InvalidOperationException or FormatException or ArgumentException)
        {
            return new(null, HubResult.Rejected(id, "invalid_command"));
        }
    }

    internal static string? Validate(HubCommand command)
    {
        if (!TryCommandId(command.Id, out _) || command.Payload is null) return "invalid_command";
        if (!KnownAction(command.Action)) return "unknown_action";
        var payload = command.Payload;
        if (!IsRoutingId(payload.AgentId, 128)) return "invalid_command";
        if (command.Action == "start_conversation")
        {
            if (!Text(payload.Message, MaxMessageCharacters, required: true)
                || !Text(payload.Title, 200, required: false)
                || payload.ConversationId is not null || payload.RequestId is not null || payload.Text is not null
                || payload.SelectedValues is not null || payload.Cancelled) return "invalid_command";
        }
        else if (command.Action == "send_message")
        {
            if (!IsRoutingId(payload.ConversationId, 256) || !Text(payload.Message, MaxMessageCharacters, required: true)
                || payload.Title is not null || payload.RequestId is not null || payload.Text is not null
                || payload.SelectedValues is not null || payload.Cancelled) return "invalid_command";
        }
        else
        {
            if (!IsRoutingId(payload.ConversationId, 256) || !IsRoutingId(payload.RequestId, 128)
                || !Text(payload.Text, MaxMessageCharacters, required: false) || payload.Title is not null || payload.Message is not null
                || payload.SelectedValues?.Count > MaxChoices
                || payload.SelectedValues?.Any(v => v is null || v.Length > 128 || v.Any(char.IsControl)) == true)
                return "invalid_command";
        }
        return null;
    }

    internal static bool KnownAction(string? action) => action is "start_conversation" or "send_message" or "answer_question";
    internal static bool TryCommandId(string? value, out string canonical)
    {
        var valid = Guid.TryParseExact(value, "D", out var id) && id != Guid.Empty;
        canonical = valid ? id.ToString("D") : "";
        return valid;
    }
    internal static bool IsRoutingId(string? value, int max)
        => !string.IsNullOrWhiteSpace(value) && value.Length <= max && !value.Any(c => char.IsControl(c) || char.IsWhiteSpace(c));
    private static bool Text(string? value, int max, bool required)
        => value is null ? !required : value.Length <= max && (!required || !string.IsNullOrWhiteSpace(value)) && !value.Contains('\0');
    private static string? String(JsonElement element, string property)
        => !element.TryGetProperty(property, out var value) || value.ValueKind == JsonValueKind.Null ? null
            : value.ValueKind == JsonValueKind.String ? value.GetString() : throw new JsonException();
    private static bool HasOnly(JsonElement element, IReadOnlySet<string> allowed)
    {
        if (element.ValueKind != JsonValueKind.Object) return false;
        var seen = new HashSet<string>(StringComparer.Ordinal);
        return element.EnumerateObject().All(property => allowed.Contains(property.Name) && seen.Add(property.Name));
    }
    private static bool HasOnly(JsonElement element, string[] allowed)
        => HasOnly(element, new HashSet<string>(allowed, StringComparer.Ordinal));

    public static byte[] Serialize<T>(T value) => JsonSerializer.SerializeToUtf8Bytes(value, JsonOptions);

    public static byte[] EncodeSnapshot(HubSnapshot snapshot)
    {
        var agents = snapshot.Agents.Take(100).ToList();
        var ids = agents.Select(agent => agent.Id).ToHashSet(StringComparer.Ordinal);
        var conversations = snapshot.Conversations.Where(row => ids.Contains(row.AgentId)).Take(100).ToList();
        var questions = snapshot.Questions.Where(row => ids.Contains(row.AgentId)).Take(100).ToList();
        var truncated = snapshot.Truncated || agents.Count != snapshot.Agents.Count
            || conversations.Count != snapshot.Conversations.Count || questions.Count != snapshot.Questions.Count;
        while (true)
        {
            var bytes = Serialize(snapshot with
            {
                Agents = agents, Conversations = conversations, Questions = questions, Truncated = truncated
            });
            if (bytes.Length <= MaxFrameBytes) return bytes;
            truncated = true;
            // Questions may reference old conversations outside the recent list, but never a removed agent.
            if (conversations.Count > 0) conversations.RemoveAt(conversations.Count - 1);
            else if (questions.Count > 0) questions.RemoveAt(questions.Count - 1);
            else if (agents.Count > 0) agents.RemoveAt(agents.Count - 1);
            else throw new HubProtocolException("frame_too_large");
        }
    }

    internal static string Clamp(string? value, int length)
    {
        if (value is null) return "";
        if (value.Length <= length) return value;
        if (length > 0 && char.IsHighSurrogate(value[length - 1])) length--;
        return value[..length];
    }
}
