namespace BotNexus.Extensions.HubConnector;

// A forward cursor alone is unsafe: the store updates Running rows in place without changing sequence.
// Keep bounded unfinished identities and re-read their exact one-row positions. Terminal identities are
// never evicted during this collector's lifetime, so reconnects and repeated terminal rows cannot double count.
internal sealed class HubRunMetrics(IHubRunEvidence evidence, DateTimeOffset startedAt,
    int maxQueries = 32, int pageSize = 128, int maxSeen = 8192, int maxPending = 512)
{
    private sealed class SessionState(string agentId, string conversationId)
    {
        public string AgentId { get; } = agentId;
        public string ConversationId { get; } = conversationId;
        public long Cursor;
        public Dictionary<long, string> Pending { get; } = [];
        public int PendingOffset;
        public bool Dropped;
    }
    private sealed class Totals
    {
        public int Completed;
        public int Durations;
        public double DurationSum;
        public int Tools;
        public double ToolSum;
        public bool CaughtUp;
        public bool Deferred;
    }
    private readonly Dictionary<string, SessionState> _sessions = new(StringComparer.Ordinal);
    private readonly HashSet<(string Session, string Run)> _seen = [];
    private int _sessionOffset;
    public long Dropped { get; private set; }
    internal int SeenCount => _seen.Count;
    internal int PendingCount => _sessions.Values.Sum(state => state.Pending.Count);
    internal int SessionCount => _sessions.Count;
    private void Drop(SessionState state)
    {
        Dropped = Math.Min(1_000_000_000_000_000_000L, Dropped + 1);
        state.Dropped = true;
    }
    public async Task<IReadOnlyList<HubMetricPoint>> CollectAsync(HubMetricsVisibility visibility,
        DateTimeOffset observedAt, CancellationToken ct)
    {
        // Current canonical ActiveSessionId only. Never infer IDs or enumerate sessions/transcripts.
        // Ambiguous bindings are excluded instead of attributing the same session to two conversations.
        var bindings = visibility.Conversations.Values.Where(row => HubProtocol.IsRoutingId(row.SessionId, 1024))
            .GroupBy(row => row.SessionId!, StringComparer.Ordinal).Where(group => group.Count() == 1)
            .Select(group => group.Single()).OrderBy(row => row.SessionId, StringComparer.Ordinal).ToArray();
        var current = bindings.Select(row => row.SessionId!).ToHashSet(StringComparer.Ordinal);
        foreach (var id in _sessions.Keys.Where(id => !current.Contains(id)).ToArray()) _sessions.Remove(id);
        foreach (var row in bindings)
        {
            var id = row.SessionId!;
            if (!_sessions.TryGetValue(id, out var state) || state.AgentId != row.AgentId || state.ConversationId != row.ConversationId)
                _sessions[id] = new(row.AgentId, row.ConversationId);
        }
        var result = new List<HubMetricPoint>();
        var budget = Math.Clamp(maxQueries, 1, 128);
        var limit = Math.Clamp(pageSize, 1, 1000);
        var totals = new Dictionary<string, Totals>(StringComparer.Ordinal);
        var visited = 0;
        var offset = bindings.Length == 0 ? 0 : _sessionOffset % bindings.Length;
        while (visited < bindings.Length && budget > 0 && !ct.IsCancellationRequested)
        {
            var binding = bindings[(offset + visited) % bindings.Length];
            visited++;
            var id = binding.SessionId!;
            var state = _sessions[id];
            var sum = new Totals();
            totals[id] = sum;
            try
            {
                // Share the budget between one forward page and unfinished runs. Recheck order rotates
                // so a long-running first row cannot hide terminal updates farther down the pending set.
                budget--;
                var page = await evidence.QueryAsync(id, limit, state.Cursor, ct).ConfigureAwait(false);
                var cursor = state.Cursor;
                foreach (var row in page.Runs.Take(limit))
                {
                    if (row.Sequence <= cursor) continue;
                    cursor = row.Sequence;
                    Observe(id, state, row, sum, observedAt);
                }
                state.Cursor = cursor;
                var pending = state.Pending.OrderBy(pair => pair.Key).ToArray();
                var rechecks = Math.Min(pending.Length, Math.Min(8, budget));
                var start = pending.Length == 0 ? 0 : state.PendingOffset % pending.Length;
                for (var n = 0; n < rechecks; n++)
                {
                    ct.ThrowIfCancellationRequested();
                    var entry = pending[(start + n) % pending.Length];
                    budget--;
                    var check = await evidence.QueryAsync(id, 1, entry.Key - 1, ct).ConfigureAwait(false);
                    // A deleted/changed row is not a completion. Do not assign a different run to this identity.
                    if (check.Runs.FirstOrDefault() is { } row && row.Sequence == entry.Key && row.Id == entry.Value)
                        Observe(id, state, row, sum, observedAt);
                }
                state.PendingOffset = start + rechecks;
                sum.CaughtUp = !page.HasMore && pending.Length == rechecks && !state.Dropped && !sum.Deferred;
            }
            // Return terminals already observed in this batch even when a later read times out.
            // Their identities have been deduplicated and must not silently disappear.
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { break; }
            catch (Exception) { } // Keep known observed terminals; never turn a failed read into a zero.
        }
        _sessionOffset = bindings.Length == 0 ? 0 : (offset + visited) % bindings.Length;
        foreach (var (id, sum) in totals)
        {
            var state = _sessions[id];
            if (sum.Completed > 0 || sum.CaughtUp)
                result.Add(new("agent.loops.completed", sum.Completed, AgentId: state.AgentId, ConversationId: state.ConversationId));
            if (sum.Durations > 0)
                result.Add(new("agent.loops.duration_ms", sum.DurationSum / sum.Durations, sum.Durations, state.AgentId, state.ConversationId));
            if (sum.Tools > 0)
                result.Add(new("agent.loops.tools", sum.ToolSum / sum.Tools, sum.Tools, state.AgentId, state.ConversationId));
        }
        return result;
    }
    private void Observe(string session, SessionState state, HubRunObservation row, Totals totals, DateTimeOffset observedAt)
    {
        if (row.Sequence < 1 || !HubProtocol.IsRoutingId(row.Id, 256)) { Drop(state); return; }
        if (_seen.Contains((session, row.Id))) { state.Pending.Remove(row.Sequence); return; }
        if (row.Outcome == "Running" || row.CompletedAt is null)
        {
            // Unknown terminal observations without a time are not measured completions. A Running row
            // is revisited; an immutable Unknown with no terminal time cannot provide numeric run metrics.
            if (row.Outcome != "Running") { state.Pending.Remove(row.Sequence); return; }
            if (state.Pending.ContainsKey(row.Sequence)) return;
            if (PendingCount >= Math.Clamp(maxPending, 1, 8192)) { Drop(state); return; }
            state.Pending[row.Sequence] = row.Id;
            return;
        }
        state.Pending.Remove(row.Sequence);
        if (row.Outcome is not ("Completed" or "Parked" or "Cancelled" or "Failed" or "Unknown")) return;
        var completed = row.CompletedAt.Value;
        // No historical backfill and no future or negative-duration evidence presented as an interval sample.
        if (completed < startedAt || completed < row.StartedAt) return;
        if (completed > observedAt)
        {
            totals.Deferred = true;
            // A terminal can arrive during this very collection. Revisit it on the next sample
            // rather than losing it behind the forward cursor or reporting a future observation.
            if (PendingCount >= Math.Clamp(maxPending, 1, 8192)) Drop(state);
            else state.Pending[row.Sequence] = row.Id;
            return;
        }
        if (_seen.Count >= Math.Clamp(maxSeen, 1, 1_000_000)) { Drop(state); return; }
        _seen.Add((session, row.Id));
        totals.Completed++;
        var duration = (completed - row.StartedAt).TotalMilliseconds;
        if (HubMetricCatalog.Number(duration)) { totals.DurationSum += duration; totals.Durations++; }
        if ((row.Outcome is "Completed" or "Parked") && row.CompletedResultCount is >= 0)
        {
            totals.ToolSum += row.CompletedResultCount.Value;
            totals.Tools++;
        }
    }
}
