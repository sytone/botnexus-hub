using System.Reflection;
using BotNexus.Domain.Primitives;
using BotNexus.Extensions.HubConnector;
using BotNexus.Gateway.Abstractions.Models;
using BotNexus.Gateway.Abstractions.Sessions;
using BotNexus.Gateway.Diagnostics;

namespace Connector.Tests;

internal sealed class MetricsClock : IHubMetricsClock
{
    public DateTimeOffset UtcNow { get; set; } = DateTimeOffset.FromUnixTimeMilliseconds(1_794_931_200_000);
    public double Seconds { get; set; } = 100;
    public void Advance(double seconds) { Seconds += seconds; UtcNow = UtcNow.AddSeconds(seconds); }
}
internal sealed class MetricsResources : IHubResourceProbe
{
    public HubResourceObservation Current { get; set; } = new();
    public Exception? Failure { get; set; }
    public int Calls { get; private set; }
    public HubResourceObservation Capture() { Calls++; return Failure is null ? Current : throw Failure; }
}
internal sealed class MetricsSqlite : IHubSqliteMetrics
{
    public IReadOnlyList<HubMetricPoint> Points { get; set; } = [];
    public Exception? Failure { get; set; }
    public IReadOnlyList<HubMetricPoint> Capture() => Failure is null ? Points : throw Failure;
}
internal sealed class MetricsActive : IHubActiveLoops
{
    public IReadOnlyList<HubActiveLoop> Loops { get; set; } = [];
    public Exception? Failure { get; set; }
    public IReadOnlyList<HubActiveLoop> Capture() => Failure is null ? Loops : throw Failure;
}
internal sealed class MetricsTracker : IActiveLoopTracker
{
    public IReadOnlyList<ActiveLoopDetail> Loops { get; set; } = [];
    public int ActiveCount => throw new InvalidOperationException("Do not read hidden aggregate counts");
    public int PeakCount => throw new InvalidOperationException("Do not read hidden aggregate counts");
    public long TotalCompleted => throw new InvalidOperationException("Do not read hidden aggregate counts");
    public ActiveLoopRegistration TrackStart(string? agentId = null, string? conversationId = null, string? sessionId = null)
        => throw new InvalidOperationException("Metrics never starts a loop");
    public void TrackEnd(ActiveLoopRegistration registration) => throw new InvalidOperationException("Metrics never ends a loop");
    public ActiveLoopSnapshot GetSnapshot() => new() { ActiveCount = 9999, PeakCount = 9999, TotalCompleted = 99999, ActiveLoops = Loops };
}
internal sealed class MetricsEvidence : IHubRunEvidence
{
    public Dictionary<string, List<HubRunObservation>> Rows { get; } = new(StringComparer.Ordinal);
    public List<(string Session, int Limit, long After)> Calls { get; } = [];
    public Action<string, int, long, CancellationToken>? BeforeQuery { get; set; }
    public Exception? Failure { get; set; }
    public Task<HubRunPage> QueryAsync(string sessionId, int limit, long afterSequence, CancellationToken ct)
    {
        Calls.Add((sessionId, limit, afterSequence));
        BeforeQuery?.Invoke(sessionId, limit, afterSequence, ct);
        ct.ThrowIfCancellationRequested();
        if (Failure is not null) throw Failure;
        var rows = Rows.GetValueOrDefault(sessionId, []).Where(row => row.Sequence > afterSequence)
            .OrderBy(row => row.Sequence).Take(limit + 1).ToArray();
        return Task.FromResult(new HubRunPage(rows.Take(limit).ToArray(), rows.Length > limit));
    }
}
internal sealed class MetricsSnapshot : IHubSnapshotMetrics
{
    public HubMetricsVisibility? Visibility { get; private set; }
    public HubMetricsEnvelope? Envelope { get; set; }
    public bool Throws { get; set; }
    public void UpdateVisibility(HubMetricsVisibility visibility)
    {
        if (Throws) throw new IOException("MUST_NOT_EXPORT_METRICS_ERROR");
        Visibility = visibility;
    }
    public HubMetricsEnvelope? Take(HubMetricsVisibility visibility)
        => Throws ? throw new IOException("MUST_NOT_EXPORT_METRICS_ERROR") : Envelope;
}
internal sealed class MetricsServices(Func<Type, object?> get) : IServiceProvider
{
    public List<Type> Calls { get; } = [];
    public object? GetService(Type serviceType) { Calls.Add(serviceType); return get(serviceType); }
}

public interface IMetricsEvidenceSession : ISessionStore, IAgentRunEvidenceStore;
// The only allowed session operation is the explicit evidence query. Any accidental transcript/aggregate
// fallback fails immediately, including ISessionStore.GetAsync and ListAsync.
public class MetricsSessionProxy : DispatchProxy
{
    public List<AgentRunEvidenceRow> Runs { get; } = [];
    public List<(string Session, int Limit, long After)> Calls { get; } = [];
    public static (ISessionStore Store, MetricsSessionProxy Fake) New(bool supportsEvidence = true)
    {
        object store = supportsEvidence ? Create<IMetricsEvidenceSession, MetricsSessionProxy>() : Create<ISessionStore, MetricsSessionProxy>();
        return ((ISessionStore)store, (MetricsSessionProxy)store);
    }
    protected override object? Invoke(MethodInfo? method, object?[]? args)
    {
        if (method!.Name != "QueryAgentRunsAsync") throw new InvalidOperationException("Forbidden session access: " + method.Name);
        var id = ((SessionId)args![0]!).Value;
        var limit = (int)args[1]!;
        var after = (long)args[2]!;
        Calls.Add((id, limit, after));
        return Task.FromResult(new AgentRunEvidencePage(Runs.Where(row => row.Sequence > after).Take(limit).ToArray(),
            0, 0, 0, 0, null, null, null, 0, 0, false, false, 999999));
    }
}

internal static class MetricsConnectionFactory
{
    public static object GetConnectionObservation() => new
    {
        CurrentObservedOpenConnections = 0L, PeakObservedOpenConnections = 9L, OpenTransitions = 12L, CloseTransitions = 12L,
        ConnectionString = "MUST_NOT_EXPORT_DB_PATH", Sql = "MUST_NOT_EXPORT_SQL"
    };
}
internal static class MetricsAllocator
{
    public static bool Available { get; set; } = true;
    public static object Capture() => new { IsAvailable = Available, CurrentBytes = (long?)0, PeakBytes = (long?)456 };
}
internal static class MetricsBrokenSqlite
{
    public static object Capture() => throw new IOException("MUST_NOT_EXPORT_NATIVE_BINDING");
    public static object GetConnectionObservation() => throw new IOException("MUST_NOT_EXPORT_DB_PATH");
}
internal sealed class MetricsBlockedResources : IHubResourceProbe, IDisposable
{
    public TaskCompletionSource Entered { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
    public ManualResetEventSlim Release { get; } = new(false);
    public HubResourceObservation Capture()
    {
        Entered.TrySetResult();
        if (!Release.Wait(TimeSpan.FromSeconds(10))) throw new TimeoutException("synthetic test guard");
        return new() { RssBytes = 123 };
    }
    public void Dispose() => Release.Dispose();
}
internal sealed class MetricsBlockedEvidence : IHubRunEvidence
{
    public TaskCompletionSource Entered { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
    public bool Cancelled { get; private set; }
    public async Task<HubRunPage> QueryAsync(string sessionId, int limit, long afterSequence, CancellationToken ct)
    {
        Entered.TrySetResult();
        try { await Task.Delay(Timeout.InfiniteTimeSpan, ct); }
        catch (OperationCanceledException) { Cancelled = true; throw; }
        return new([], false);
    }
}
