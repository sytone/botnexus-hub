namespace BotNexus.Extensions.HubConnector;

internal sealed record HubMetricPoint(string MetricId, double Value, int Weight = 1,
    string? AgentId = null, string? ConversationId = null);
internal sealed record HubMetricsEnvelope(string BootId, long Sequence, long ObservedAt, int IntervalSeconds,
    IReadOnlyList<HubMetricPoint> Points)
{
    public int SchemaVersion => 1;
}

// This is an explicit wire allowlist, not a general-purpose telemetry exporter.
internal static class HubMetricCatalog
{
    internal static readonly IReadOnlySet<string> World = new HashSet<string>(StringComparer.Ordinal)
    {
        "machine.cpu.percent", "machine.memory.total_bytes", "machine.memory.used_bytes", "machine.memory.percent",
        "machine.disk.total_bytes", "machine.disk.free_bytes", "machine.disk.percent",
        "container.cpu.percent", "container.memory.total_bytes", "container.memory.used_bytes", "container.memory.percent",
        "gateway.cpu.percent", "gateway.memory.rss_bytes", "gateway.memory.private_bytes", "gateway.gc.heap_bytes",
        "gateway.gc.pause_percent", "gateway.threads.count", "gateway.uptime.seconds",
        "gateway.io.read_bytes_per_second", "gateway.io.write_bytes_per_second", "gateway.loops.active",
        "gateway.questions.pending", "gateway.conversations.visible", "sqlite.connections.current",
        "sqlite.connections.peak", "sqlite.connections.opened", "sqlite.connections.closed", "sqlite.memory.current_bytes",
        "sqlite.memory.peak_bytes", "connector.collection.duration_ms", "connector.runs.dropped"
    };
    internal static readonly IReadOnlySet<string> Agent = new HashSet<string>(StringComparer.Ordinal)
    {
        "agent.loops.active", "agent.loops.completed", "agent.loops.duration_ms", "agent.loops.tools"
    };
    internal static bool Number(double value) => double.IsFinite(value) && value >= 0 && value <= 1e18;
    internal static bool IsPercent(string id) => id.EndsWith(".percent", StringComparison.Ordinal) || id == "gateway.gc.pause_percent";
    internal static bool IsRun(string id) => id is "agent.loops.completed" or "agent.loops.duration_ms" or "agent.loops.tools";
    internal static bool Valid(HubMetricPoint point, HubMetricsVisibility visibility)
    {
        if (!Number(point.Value) || point.Weight is < 1 or > 1_000_000 || IsPercent(point.MetricId) && point.Value > 100)
            return false;
        if (World.Contains(point.MetricId)) return point.AgentId is null && point.ConversationId is null;
        return Agent.Contains(point.MetricId) && visibility.Allows(point.AgentId, point.ConversationId);
    }
}

internal sealed record HubMetricsConversation(string AgentId, string ConversationId, string? SessionId);

internal sealed class HubMetricsVisibility
{
    internal static readonly HubMetricsVisibility Empty = new([], [], 0);
    public IReadOnlySet<string> Agents { get; }
    public IReadOnlyDictionary<string, HubMetricsConversation> Conversations { get; }
    public int PendingQuestions { get; }
    public HubMetricsVisibility(IEnumerable<string> agents, IEnumerable<HubMetricsConversation> conversations, int pendingQuestions)
    {
        Agents = agents.Where(id => HubProtocol.IsRoutingId(id, 128)).Take(100).ToHashSet(StringComparer.Ordinal);
        Conversations = conversations.Where(row => Agents.Contains(row.AgentId) && HubProtocol.IsRoutingId(row.ConversationId, 256))
            .Take(200).DistinctBy(row => row.ConversationId, StringComparer.Ordinal)
            .ToDictionary(row => row.ConversationId, StringComparer.Ordinal);
        PendingQuestions = Math.Clamp(pendingQuestions, 0, 100);
    }
    public bool Allows(string? agentId, string? conversationId)
        => agentId is not null && Agents.Contains(agentId) && (conversationId is null
            || Conversations.TryGetValue(conversationId, out var row) && row.AgentId == agentId);
    public static HubMetricsVisibility FromSnapshot(HubSnapshot snapshot)
        => new(snapshot.Agents.Select(row => row.Id),
            snapshot.Conversations.Select(row => new HubMetricsConversation(row.AgentId, row.Id, null))
                .Concat(snapshot.Questions.Select(row => new HubMetricsConversation(row.AgentId, row.ConversationId, null))),
            snapshot.Questions.Count);
}

internal static class HubMetricsWire
{
    internal const int MaxPoints = 512;
    internal const int MaxBytes = 64 * 1024;

    public static HubMetricsEnvelope? Bound(HubMetricsEnvelope envelope, HubMetricsVisibility visibility)
    {
        if (!Guid.TryParseExact(envelope.BootId, "D", out var boot) || boot == Guid.Empty || envelope.Sequence < 1
            || envelope.IntervalSeconds is < 15 or > 3600 || envelope.ObservedAt < 0 || envelope.Points.Count > 2048) return null;
        var points = new List<HubMetricPoint>();
        var seen = new HashSet<(string, string?, string?)>();
        var valid = envelope.Points.Where(point => HubMetricCatalog.Valid(point, visibility)
                && seen.Add((point.MetricId, point.AgentId, point.ConversationId))).ToArray();
        var removedActive = valid.Count(point => point.MetricId == "agent.loops.active")
            != envelope.Points.Count(point => point.MetricId == "agent.loops.active");
        // Account for escaped UTF-8 bytes, not character count. Never serialize a candidate unbounded list.
        var bytes = HubProtocol.Serialize(envelope with { Points = [] }).Length;
        void Add(IReadOnlyList<HubMetricPoint> group)
        {
            // Reserve room to increase the lifetime dropped counter after trimming this batch.
            var size = group.Sum(point => point.MetricId == "connector.runs.dropped"
                ? HubProtocol.Serialize(point with { Value = 0 }).Length + 33 : HubProtocol.Serialize(point).Length + 1);
            if (points.Count + group.Count > MaxPoints || bytes + size > MaxBytes)
            {
                removedActive |= group.Any(point => point.MetricId == "agent.loops.active");
                return;
            }
            points.AddRange(group);
            bytes += size;
        }
        // Prefer world gauges and active loops. Keep one conversation's run count and weighted means
        // together so a cap cannot leave misleading partial run statistics.
        foreach (var point in valid.Where(point => !HubMetricCatalog.IsRun(point.MetricId))) Add([point]);
        foreach (var group in valid.Where(point => HubMetricCatalog.IsRun(point.MetricId))
                     .GroupBy(point => (point.AgentId, point.ConversationId))) Add(group.ToArray());
        if (removedActive) points.RemoveAll(point => point.MetricId == "gateway.loops.active");
        points.RemoveAll(point => point.MetricId == "gateway.conversations.visible" && point.Value != visibility.Conversations.Count
            || point.MetricId == "gateway.questions.pending" && point.Value != visibility.PendingQuestions);
        return points.Count == 0 ? null : envelope with { Points = points };
    }
}

internal interface IHubMetricsClock
{
    DateTimeOffset UtcNow { get; }
    double Seconds { get; }
}
internal sealed class HubMetricsClock : IHubMetricsClock
{
    public DateTimeOffset UtcNow => DateTimeOffset.UtcNow;
    public double Seconds => (double)System.Diagnostics.Stopwatch.GetTimestamp() / System.Diagnostics.Stopwatch.Frequency;
}

internal interface IHubSnapshotMetrics
{
    void UpdateVisibility(HubMetricsVisibility visibility);
    HubMetricsEnvelope? Take(HubMetricsVisibility visibility);
}
