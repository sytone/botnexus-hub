using System.Security.Cryptography;
using System.Text;
using BotNexus.Domain.Primitives;
using BotNexus.Domain.World;
using BotNexus.Gateway.Abstractions.Agents;
using BotNexus.Gateway.Abstractions.Configuration;
using BotNexus.Gateway.Abstractions.Conversations;
using BotNexus.Gateway.Abstractions.Models;
using BotNexus.Gateway.Abstractions.Services;
using BotNexus.Gateway.Dispatching;

namespace BotNexus.Extensions.HubConnector;

internal sealed class HubCommandProcessor(HubConnectorOptions options, IConversationStore store, IAgentRegistry agents,
    IInboundMessageOrchestrator orchestrator, IWorldContext world, IAskUserPromptResolver? resolver = null,
    IAskUserCheckpointService? checkpoints = null, int cacheCapacity = 4096)
{
    private static readonly CitizenId Operator = CitizenId.Of(UserId.From("hub-operator"));
    private readonly SemaphoreSlim _gate = new(1, 1);
    private readonly Dictionary<string, CachedCommand> _commands = new(StringComparer.Ordinal);
    private sealed record CachedCommand(string Fingerprint, HubResult Result);

    public async Task<HubResult> ProcessAsync(HubCommand command, CancellationToken ct = default)
    {
        var validation = HubProtocol.Validate(command);
        if (validation is not null) return HubResult.Rejected(command.Id, validation);
        HubProtocol.TryCommandId(command.Id, out var id);
        command = command with { Id = id };
        var fingerprint = Convert.ToHexString(SHA256.HashData(HubProtocol.Serialize(command)));
        await _gate.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            if (_commands.TryGetValue(id, out var previous))
                return previous.Fingerprint == fingerprint ? previous.Result : HubResult.Rejected(id, "command_conflict");
            // Never evict an admitted command and silently allow its replay. At the bound, new work fails closed.
            if (_commands.Count >= cacheCapacity) return HubResult.Rejected(id, "command_capacity");
            var result = HubResult.Rejected(id, "outcome_unknown");
            _commands.Add(id, new(fingerprint, result));
            try
            {
                result = await ExecuteAsync(command, ct).ConfigureAwait(false);
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested)
            {
                // The unknown marker remains for this process lifetime even if admission raced cancellation.
                throw;
            }
            catch (Exception)
            {
                // Store/queue/resolver exceptions may include user text, credentials or paths. Never forward them.
                result = HubResult.Rejected(id, "outcome_unknown");
            }
            _commands[id] = new(fingerprint, result);
            return result;
        }
        finally { _gate.Release(); }
    }

    private async Task<HubResult> ExecuteAsync(HubCommand command, CancellationToken ct)
    {
        ct.ThrowIfCancellationRequested();
        var payload = command.Payload;
        var agentId = AgentId.From(payload.AgentId);
        if (!options.AllowedAgents.Contains(payload.AgentId)) return HubResult.Rejected(command.Id, "agent_not_allowed");
        var agent = agents.Get(agentId);
        if (agent is null || agent.AgentId != agentId || !HubVisibilityPolicy.AllowsAgent(options, agent))
            return HubResult.Rejected(command.Id, "agent_unavailable");

        if (command.Action == "start_conversation")
        {
            // Stable across reconnects and restarts. An existing row means the earlier seed may have run: do not replay it.
            var digest = SHA256.HashData(Encoding.UTF8.GetBytes(options.HubId + "\0" + command.Id));
            var id = ConversationId.From("c_" + Convert.ToHexStringLower(digest.AsSpan(0, 16)));
            if (await store.GetAsync(id, ct).ConfigureAwait(false) is not null)
                return HubResult.Rejected(command.Id, "outcome_unknown");
            var conversation = ConversationFactory.CreateForChannel(id, agentId, payload.Title, Operator);
            conversation.WorldId = world.Current.Id;
            await store.CreateAsync(conversation, ct).ConfigureAwait(false);
            return await AdmitAsync(command, agentId, id, payload.Message!, ct).ConfigureAwait(false);
        }

        var conversationId = ConversationId.From(payload.ConversationId!);
        var row = await store.GetAsync(conversationId, ct).ConfigureAwait(false);
        if (row is null) return HubResult.Rejected(command.Id, "conversation_not_found");
        if (!HubVisibilityPolicy.AllowsConversation(row, agentId.Value, conversationId.Value))
            return HubResult.Rejected(command.Id, "conversation_not_writable");
        return command.Action == "send_message"
            ? await AdmitAsync(command, agentId, conversationId, payload.Message!, ct).ConfigureAwait(false)
            : await AnswerAsync(command, row, ct).ConfigureAwait(false);
    }

    private async Task<HubResult> AdmitAsync(HubCommand command, AgentId agentId, ConversationId conversationId,
        string message, CancellationToken ct)
    {
        var status = await orchestrator.PostAsync(new InboundMessage
        {
            ChannelType = ChannelKey.From("hub"), SenderId = "hub-operator", Sender = Operator,
            ChannelAddress = ChannelAddress.From(options.HubId + "/" + conversationId.Value),
            Content = message, ChannelRequestId = command.Id,
            RoutingHints = new InboundMessageRoutingHints(agentId, null, conversationId)
        }, ct).ConfigureAwait(false);
        return status switch
        {
            InboundDispatchStatus.Accepted or InboundDispatchStatus.Steered => HubResult.Accepted(command.Id, conversationId.Value),
            InboundDispatchStatus.Busy => HubResult.Rejected(command.Id, "busy"),
            InboundDispatchStatus.NoRoute => HubResult.Rejected(command.Id, "no_route"),
            InboundDispatchStatus.Rejected => HubResult.Rejected(command.Id, "rejected"),
            _ => HubResult.Rejected(command.Id, "outcome_unknown")
        };
    }

    private async Task<HubResult> AnswerAsync(HubCommand command, Conversation conversation, CancellationToken ct)
    {
        var payload = command.Payload;
        if (resolver is null && checkpoints is null) return HubResult.Rejected(command.Id, "answer_unavailable");
        var durable = HubCheckpointProjection.Project(conversation.PendingAskUserJson, conversation);
        var liveId = resolver?.TryGetPendingRequestId(conversation.ConversationId, out var pendingId) == true ? pendingId : null;
        var currentId = liveId ?? durable?.RequestId;
        if (currentId is null) return HubResult.Rejected(command.Id, "no_pending_question");
        if (!string.Equals(payload.RequestId, currentId, StringComparison.Ordinal))
            return HubResult.Rejected(command.Id, "request_mismatch");

        var text = string.IsNullOrWhiteSpace(payload.Text) ? null : payload.Text.Trim();
        var values = payload.SelectedValues?.Where(value => !string.IsNullOrWhiteSpace(value)).Select(value => value.Trim()).ToArray();
        if (values is { Length: 0 }) values = null;
        if (!payload.Cancelled && text is null && values is null) return HubResult.Rejected(command.Id, "invalid_answer");
        if (resolver is not null)
        {
            var result = await resolver.ResolveAsync(new AskUserSubmission
            {
                ConversationId = conversation.ConversationId, RequestId = payload.RequestId,
                FreeFormText = payload.Text, SelectedValues = payload.SelectedValues,
                Cancelled = payload.Cancelled, OriginChannel = ChannelKey.From("hub")
            }, ct).ConfigureAwait(false);
            if (result.Succeeded) return HubResult.Accepted(command.Id, conversation.ConversationId.Value);
            if (result.Status == AskUserResolutionStatus.InvalidSubmission) return HubResult.Rejected(command.Id, "invalid_answer");
            if (result.Status != AskUserResolutionStatus.NoPendingPrompt) return HubResult.Rejected(command.Id, "no_pending_question");
        }
        if (checkpoints is null) return HubResult.Rejected(command.Id, "no_pending_question");
        if (durable is not null && durable.RequestId != payload.RequestId) return HubResult.Rejected(command.Id, "request_mismatch");
        var outcome = await checkpoints.ResolveAsync(conversation.ConversationId, payload.RequestId!, new AskUserResponse
        {
            RequestId = payload.RequestId!, FreeFormText = text, SelectedValues = values, WasCancelled = payload.Cancelled
        }, ct).ConfigureAwait(false);
        // Match GatewayHub.RespondToAskUser: an already-consumed checkpoint is an idempotent no-op, not another resume.
        return outcome switch
        {
            AskUserResolveOutcome.RequestIdMismatch => HubResult.Rejected(command.Id, "request_mismatch"),
            AskUserResolveOutcome.LiveCompleted or AskUserResolveOutcome.ResumedFromCheckpoint or AskUserResolveOutcome.NoPendingCheckpoint
                => HubResult.Accepted(command.Id, conversation.ConversationId.Value),
            _ => HubResult.Rejected(command.Id, "outcome_unknown")
        };
    }
}
