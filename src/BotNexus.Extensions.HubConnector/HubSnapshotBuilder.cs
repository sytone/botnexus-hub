using BotNexus.Domain.Primitives;
using BotNexus.Gateway.Abstractions.Agents;
using BotNexus.Gateway.Abstractions.Configuration;
using BotNexus.Gateway.Abstractions.Conversations;
using BotNexus.Gateway.Abstractions.Models;

namespace BotNexus.Extensions.HubConnector;

internal sealed class HubSnapshotBuilder(HubConnectorOptions options, IConversationStore store, IAgentRegistry agents,
    IWorldContext world)
{
    public async Task<HubSnapshot> BuildAsync(CancellationToken ct = default)
    {
        ct.ThrowIfCancellationRequested();
        var observedAt = DateTimeOffset.UtcNow;
        var allowed = agents.GetAll().Where(agent => HubVisibilityPolicy.AllowsAgent(options, agent))
            .DistinctBy(agent => agent.AgentId.Value, StringComparer.Ordinal)
            .OrderBy(agent => agent.AgentId.Value, StringComparer.Ordinal).Take(100).ToArray();
        var ids = allowed.Select(agent => agent.AgentId.Value).ToHashSet(StringComparer.Ordinal);
        var agentSnapshots = allowed.Select(agent => new HubAgent(agent.AgentId.Value,
            HubProtocol.Clamp(agent.DisplayName, 160), agent.Description is null ? null : HubProtocol.Clamp(agent.Description, 1024))).ToArray();

        // The shared older contract has no limit or lookback parameter. This returns metadata, never session history.
        // Do not replace either query with ListAsync, ListForCitizenAsync or a session/transcript scan.
        var summaries = await store.GetSummariesAsync(ct).ConfigureAwait(false);
        var checkpoints = await store.GetPendingAskUserCheckpointsAsync(ct).ConfigureAwait(false);
        var rows = new Dictionary<string, Conversation?>(StringComparer.Ordinal);
        async Task<Conversation?> Row(string id)
        {
            if (!rows.TryGetValue(id, out var row))
            {
                row = await store.GetAsync(ConversationId.From(id), ct).ConfigureAwait(false);
                rows.Add(id, row);
            }
            return row;
        }
        var conversations = new List<HubConversation>();
        foreach (var summary in summaries.Where(summary => ids.Contains(summary.AgentId)
                     && HubProtocol.IsRoutingId(summary.ConversationId, 256)
                     && summary.Status == "Active" && summary.Visibility == "UserFacing"
                     && summary.UpdatedAt >= observedAt - options.ActivityLookback)
                 .OrderByDescending(summary => summary.UpdatedAt).ThenBy(summary => summary.ConversationId, StringComparer.Ordinal)
                 .DistinctBy(summary => summary.ConversationId, StringComparer.Ordinal))
        {
            ct.ThrowIfCancellationRequested();
            var row = await Row(summary.ConversationId).ConfigureAwait(false);
            // Summary has no parent field. Recheck canonical owner, visibility and parentage by id before exporting.
            if (row is null || !HubVisibilityPolicy.AllowsConversation(row, summary.AgentId, summary.ConversationId)) continue;
            conversations.Add(new(summary.ConversationId, summary.AgentId, HubProtocol.Clamp(row.Title, 200),
                summary.UpdatedAt.ToUniversalTime(), "active"));
            if (conversations.Count == options.MaxConversations) break;
        }

        var questions = new List<HubQuestion>();
        // Pending checkpoints are intentionally independent of recent activity, including after a gateway restart.
        foreach (var checkpoint in checkpoints.OrderBy(checkpoint => checkpoint.ConversationId.Value, StringComparer.Ordinal)
                     .DistinctBy(checkpoint => checkpoint.ConversationId.Value, StringComparer.Ordinal))
        {
            ct.ThrowIfCancellationRequested();
            if (!HubProtocol.IsRoutingId(checkpoint.ConversationId.Value, 256)) continue;
            var row = await Row(checkpoint.ConversationId.Value).ConfigureAwait(false);
            if (row is null || !ids.Contains(row.AgentId.Value)
                || !HubVisibilityPolicy.AllowsConversation(row, row.AgentId.Value, checkpoint.ConversationId.Value)) continue;
            var question = HubCheckpointProjection.Project(checkpoint.PendingAskUserJson, row);
            if (question is null) continue;
            questions.Add(question);
            if (questions.Count == options.MaxQuestions) break;
        }
        var identity = options.IncludeWorldIdentity
            ? new HubWorldIdentity(HubProtocol.Clamp(world.Current.Id, 128), HubProtocol.Clamp(world.Current.Name, 160)) : null;
        var version = typeof(HubSnapshotBuilder).Assembly.GetName().Version?.ToString() ?? "unknown";
        return new(options.HubId, options.Name, options.BaseUrl, version, observedAt, agentSnapshots, conversations, questions, identity);
    }
}
