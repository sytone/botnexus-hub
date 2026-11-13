using System.Text.Json.Serialization;

namespace BotNexus.Extensions.HubConnector;

internal sealed record HubAgent(string Id, string Name, string? Description);
internal sealed record HubWorldIdentity(string Id, string Name);
internal sealed record HubChoice(string Value, string Label, string? Description);
internal sealed record HubQuestion(string ConversationId, string AgentId, string Title, string RequestId,
    string Prompt, string InputType, IReadOnlyList<HubChoice> Choices, bool AllowMultiple, bool AllowFreeForm);
internal sealed record HubConversation(string Id, string AgentId, string Title, DateTimeOffset UpdatedAt,
    string Status, HubQuestion? PendingQuestion = null);
internal sealed record HubSnapshot(string WorldId, string Name, string BaseUrl, string Version,
    DateTimeOffset ObservedAt, IReadOnlyList<HubAgent> Agents, IReadOnlyList<HubConversation> Conversations,
    IReadOnlyList<HubQuestion> Questions, HubWorldIdentity? WorldIdentity = null, bool Truncated = false)
{
    public string Type => "snapshot";
    public int ProtocolVersion => 1;
}

internal sealed record HubCommandPayload(string AgentId, string? ConversationId = null, string? Title = null,
    string? Message = null, string? RequestId = null, string? Text = null,
    IReadOnlyList<string>? SelectedValues = null, bool Cancelled = false);
internal sealed record HubCommand(string Id, string Action, HubCommandPayload Payload);
internal sealed record HubAccepted(string ConversationId, string Status = "accepted");
internal sealed record HubResult(string Id, bool Ok,
    [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] HubAccepted? Data = null,
    [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? Error = null)
{
    public string Type => "result";
    public static HubResult Accepted(string id, string conversationId) => new(id, true, new(conversationId));
    public static HubResult Rejected(string id, string error) => new(id, false, Error: error);
}
internal sealed record HubCommandParse(HubCommand? Command, HubResult? Error);
internal sealed class HubProtocolException(string code) : Exception(code)
{
    public string Code { get; } = code;
}
