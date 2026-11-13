using System.Collections.Concurrent;
using System.Net.WebSockets;
using System.Reflection;
using BotNexus.Domain;
using BotNexus.Domain.Primitives;
using BotNexus.Gateway.Abstractions.Agents;
using BotNexus.Gateway.Abstractions.Configuration;
using BotNexus.Gateway.Abstractions.Conversations;
using BotNexus.Gateway.Abstractions.Models;
using BotNexus.Gateway.Abstractions.Services;
using BotNexus.Gateway.Dispatching;

namespace Connector.Tests;

// A strict fake: any aggregate listing, transcript, configuration or unexpected store access fails.
public class StoreProxy : DispatchProxy
{
    public Dictionary<string, Conversation> Rows { get; } = new(StringComparer.Ordinal);
    public List<ConversationSummary> Summaries { get; } = [];
    public List<PendingAskUserCheckpoint> Pending { get; } = [];
    public List<string> Calls { get; } = [];
    public Exception? Failure { get; set; }
    public int Creates { get; private set; }
    public static (IConversationStore Store, StoreProxy Fake) New()
    {
        var store = Create<IConversationStore, StoreProxy>();
        return (store, (StoreProxy)(object)store);
    }
    protected override object? Invoke(MethodInfo? method, object?[]? args)
    {
        Calls.Add(method!.Name);
        if (Failure is not null) throw Failure;
        switch (method.Name)
        {
            case "GetAsync":
                return Task.FromResult(Rows.GetValueOrDefault(((ConversationId)args![0]!).Value));
            case "GetSummariesAsync":
                return Task.FromResult<IReadOnlyList<ConversationSummary>>(Summaries);
            case "GetPendingAskUserCheckpointsAsync":
                return Task.FromResult<IReadOnlyList<PendingAskUserCheckpoint>>(Pending);
            case "CreateAsync":
                var conversation = (Conversation)args![0]!;
                if (!Rows.TryAdd(conversation.ConversationId.Value, conversation))
                    throw new InvalidOperationException("synthetic duplicate");
                Creates++;
                return Task.FromResult(conversation);
            default:
                throw new InvalidOperationException("Unexpected store method: " + method.Name);
        }
    }
}
internal sealed class FakeAgents(params AgentDescriptor[] descriptors) : IAgentRegistry
{
    public List<AgentDescriptor> Items { get; } = [.. descriptors];
    public AgentDescriptor? Get(AgentId id) => Items.FirstOrDefault(x => x.AgentId == id);
    public IReadOnlyList<AgentDescriptor> GetAll() => Items;
    public bool Contains(AgentId id) => Get(id) is not null;
    public void Register(AgentDescriptor descriptor) => Items.Add(descriptor);
    public void Unregister(AgentId id) => Items.RemoveAll(x => x.AgentId == id);
}
internal sealed class FakeWorld : IWorldContext
{
    public WorldIdentity Current { get; } = new() { Id = "actual-local-world", Name = "Synthetic local world", Description = "MUST_NOT_EXPORT_DESCRIPTION" };
}
internal sealed class FakeOrchestrator : IInboundMessageOrchestrator
{
    public List<InboundMessage> Messages { get; } = [];
    public InboundDispatchStatus Status { get; set; } = InboundDispatchStatus.Accepted;
    public Exception? Failure { get; set; }
    public bool Post(InboundMessage message) => throw new InvalidOperationException("Use PostAsync admission");
    public Task<InboundDispatchResult> AcceptAsync(InboundMessage message, CancellationToken cancellationToken = default)
        => throw new InvalidOperationException("Must not await an LLM turn");
    public Task<InboundDispatchStatus> PostAsync(InboundMessage message, CancellationToken cancellationToken = default)
    {
        Messages.Add(message);
        if (Failure is not null) throw Failure;
        return Task.FromResult(Status);
    }
}
internal sealed class FakeResolver : IAskUserPromptResolver
{
    public string? PendingId { get; set; } = "req-1";
    public AskUserResolutionResult Result { get; set; } = AskUserResolutionResult.Resolved("req-1");
    public List<AskUserSubmission> Submissions { get; } = [];
    public bool TryGetPendingRequestId(ConversationId conversationId, out string requestId)
    {
        requestId = PendingId ?? "";
        return PendingId is not null;
    }
    public ValueTask<AskUserResolutionResult> ResolveAsync(AskUserSubmission submission, CancellationToken cancellationToken = default)
    {
        Submissions.Add(submission);
        return ValueTask.FromResult(Result);
    }
}
internal sealed class FakeCheckpoints : IAskUserCheckpointService
{
    public List<AskUserResponse> Responses { get; } = [];
    public AskUserResolveOutcome Result { get; set; } = AskUserResolveOutcome.ResumedFromCheckpoint;
    public Task<AskUserResolveOutcome> ResolveAsync(ConversationId conversationId, string requestId,
        AskUserResponse response, CancellationToken cancellationToken = default)
    {
        Responses.Add(response);
        return Task.FromResult(Result);
    }
    public Task<bool> TryResolveInboundTextAsync(ConversationId conversationId, string freeFormText,
        CancellationToken cancellationToken = default) => throw new InvalidOperationException("Explicit request id required");
}
internal sealed class FakeSocket : WebSocket
{
    public Queue<(byte[] Bytes, WebSocketMessageType Type, bool End)> Incoming { get; } = [];
    public ConcurrentQueue<byte[]> Sent { get; } = new();
    private int _sending;
    public int MaxConcurrentSends { get; private set; }
    public override WebSocketCloseStatus? CloseStatus => null;
    public override string? CloseStatusDescription => null;
    public override WebSocketState State => WebSocketState.Open;
    public override string? SubProtocol => null;
    public override void Abort() { }
    public override Task CloseAsync(WebSocketCloseStatus closeStatus, string? statusDescription, CancellationToken cancellationToken) => Task.CompletedTask;
    public override Task CloseOutputAsync(WebSocketCloseStatus closeStatus, string? statusDescription, CancellationToken cancellationToken) => Task.CompletedTask;
    public override void Dispose() { }
    public override async Task<WebSocketReceiveResult> ReceiveAsync(ArraySegment<byte> buffer, CancellationToken cancellationToken)
    {
        if (Incoming.Count == 0)
        {
            await Task.Delay(Timeout.Infinite, cancellationToken);
            throw new InvalidOperationException("unreachable");
        }
        var next = Incoming.Dequeue();
        next.Bytes.AsSpan().CopyTo(buffer.AsSpan());
        return new WebSocketReceiveResult(next.Bytes.Length, next.Type, next.End);
    }
    public override async Task SendAsync(ArraySegment<byte> buffer, WebSocketMessageType messageType,
        bool endOfMessage, CancellationToken cancellationToken)
    {
        var active = Interlocked.Increment(ref _sending);
        MaxConcurrentSends = Math.Max(MaxConcurrentSends, active);
        try
        {
            await Task.Yield();
            cancellationToken.ThrowIfCancellationRequested();
            Sent.Enqueue(buffer.ToArray());
        }
        finally { Interlocked.Decrement(ref _sending); }
    }
}
