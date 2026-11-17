namespace BotNexus.Extensions.HubConnector;

// One collector per hosted-service lifetime, not per WebSocket. A slow probe never holds the snapshot
// or command path: snapshots only publish their visibility and take a completed, bounded sample.
internal sealed class HubMetricsCollector : IHubSnapshotMetrics, IAsyncDisposable
{
    private readonly HubConnectorOptions _options;
    private readonly IHubMetricsClock _clock;
    private readonly IHubResourceProbe _resources;
    private readonly IHubSqliteMetrics _sqlite;
    private readonly IHubActiveLoops? _active;
    private readonly HubRunMetrics? _runs;
    private readonly HubResourceRates _rates;
    private readonly object _gate = new();
    private readonly TaskCompletionSource _firstVisibility = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly CancellationTokenSource _stop = new();
    private readonly string _bootId = Guid.NewGuid().ToString("D");
    private readonly Task _worker;
    private HubMetricsVisibility _visibility = HubMetricsVisibility.Empty;
    private HubMetricsEnvelope? _ready;
    private double _readyAt;
    private double? _lastTakenAt;
    private long _sequence;
    private long _unsentRuns;

    internal HubMetricsCollector(HubConnectorOptions options, IHubResourceProbe? resources = null,
        IHubSqliteMetrics? sqlite = null, IHubActiveLoops? active = null, IHubRunEvidence? runs = null,
        IHubMetricsClock? clock = null, bool startWorker = true)
    {
        _options = options;
        _clock = clock ?? new HubMetricsClock();
        _resources = resources ?? new HubResourceProbe();
        _sqlite = sqlite ?? new HubSqliteMetrics();
        _active = active;
        _rates = new(options.MetricsInterval);
        _runs = runs is null ? null : new(runs, _clock.UtcNow);
        _worker = startWorker && options.MetricsEnabled ? Task.Run(RunAsync) : Task.CompletedTask;
    }
    internal static HubMetricsCollector? Create(HubConnectorOptions options, IServiceProvider? services)
    {
        if (!options.MetricsEnabled) return null;
        try { return new(options, active: HubGatewayMetrics.ActiveLoops(services), runs: HubGatewayMetrics.RunEvidence(services)); }
        catch (Exception) { return null; }
    }
    public void UpdateVisibility(HubMetricsVisibility visibility)
    {
        lock (_gate) _visibility = visibility;
        _firstVisibility.TrySetResult();
    }
    public HubMetricsEnvelope? Take(HubMetricsVisibility visibility)
    {
        if (!_options.MetricsEnabled) return null;
        lock (_gate)
        {
            var now = _clock.Seconds;
            if (_ready is null || _lastTakenAt is { } taken && now - taken < _options.MetricsInterval.TotalSeconds) return null;
            var ready = _ready;
            _ready = null;
            if (now - _readyAt < 0 || now - _readyAt > _options.MetricsInterval.TotalSeconds * 2.5)
            {
                DropUnsent(ready, visibility);
                return null;
            }
            var bounded = HubMetricsWire.Bound(ready, visibility);
            RecordTrimmedRuns(ready, bounded, visibility);
            if (bounded is not null) _lastTakenAt = now;
            return WithDropped(bounded);
        }
    }
    private async Task RunAsync()
    {
        var ct = _stop.Token;
        try
        {
            await _firstVisibility.Task.WaitAsync(ct).ConfigureAwait(false);
            while (!ct.IsCancellationRequested)
            {
                await CollectOnceAsync(ct).ConfigureAwait(false);
                await Task.Delay(_options.MetricsInterval, ct).ConfigureAwait(false);
            }
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested) { }
        catch (Exception) { } // Metrics must not fault the hosted service or its command loop.
    }
    internal async Task CollectOnceAsync(CancellationToken ct = default)
    {
        if (!_options.MetricsEnabled) return;
        HubMetricsVisibility visibility;
        lock (_gate) visibility = _visibility;
        var started = _clock.Seconds;
        var observedAt = _clock.UtcNow;
        var points = new List<HubMetricPoint>();
        try
        {
            try { points.AddRange(_rates.Observe(_resources.Capture(), started)); }
            catch (Exception) { _rates.Reset(); }
            try { points.AddRange(_sqlite.Capture()); }
            catch (Exception) { }
            if (_active is not null)
            {
                try { points.AddRange(HubLoopMetrics.Active(_active.Capture(), visibility)); }
                catch (Exception) { }
            }
            points.Add(new("gateway.questions.pending", visibility.PendingQuestions));
            points.Add(new("gateway.conversations.visible", visibility.Conversations.Count));
            if (_runs is not null)
            {
                // A cooperative bounded store read has a separate timeout and never delays snapshots.
                using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
                timeout.CancelAfter(TimeSpan.FromSeconds(2));
                try { points.AddRange(await _runs.CollectAsync(visibility, observedAt, timeout.Token).ConfigureAwait(false)); }
                catch (OperationCanceledException) when (!ct.IsCancellationRequested) { }
                catch (Exception) when (!ct.IsCancellationRequested) { }
            }
            ct.ThrowIfCancellationRequested();
            var duration = (_clock.Seconds - started) * 1000;
            if (HubMetricCatalog.Number(duration)) points.Insert(0, new("connector.collection.duration_ms", duration));
            lock (_gate)
            {
                if (_ready is not null) DropUnsent(_ready, visibility); // Offline/slow snapshots keep only the latest sample.
                if (_runs is not null || _unsentRuns > 0)
                    points.Insert(0, new("connector.runs.dropped", Math.Min(1e18, (_runs?.Dropped ?? 0) + (double)_unsentRuns)));
                var envelope = new HubMetricsEnvelope(_bootId, ++_sequence, observedAt.ToUnixTimeMilliseconds(),
                    (int)_options.MetricsInterval.TotalSeconds, points);
                _ready = HubMetricsWire.Bound(envelope, visibility);
                RecordTrimmedRuns(envelope, _ready, visibility);
                _ready = WithDropped(_ready);
                _readyAt = _clock.Seconds;
            }
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested) { throw; }
        catch (Exception)
        {
            _rates.Reset();
            lock (_gate) _ready = null; // No stale successful sample after a failed collection.
        }
    }
    private static double Completed(HubMetricsEnvelope? envelope, HubMetricsVisibility visibility) => envelope?.Points
        .Where(point => point.MetricId == "agent.loops.completed" && HubMetricCatalog.Valid(point, visibility))
        .Sum(point => point.Value) ?? 0;
    private void RecordTrimmedRuns(HubMetricsEnvelope before, HubMetricsEnvelope? after, HubMetricsVisibility visibility)
        => _unsentRuns = (long)Math.Min(1e18, _unsentRuns + Math.Max(0, Completed(before, visibility) - Completed(after, visibility)));
    private HubMetricsEnvelope? WithDropped(HubMetricsEnvelope? envelope)
        => envelope is null ? null : envelope with
        {
            Points = envelope.Points.Select(point => point.MetricId == "connector.runs.dropped"
                ? point with { Value = Math.Min(1e18, (_runs?.Dropped ?? 0) + (double)_unsentRuns) } : point).ToArray()
        };
    private void DropUnsent(HubMetricsEnvelope envelope, HubMetricsVisibility visibility)
        => _unsentRuns = (long)Math.Min(1e18, _unsentRuns + Completed(envelope, visibility));
    public async ValueTask DisposeAsync()
    {
        _stop.Cancel();
        try { await _worker.ConfigureAwait(false); }
        catch (OperationCanceledException) { }
        _stop.Dispose();
    }
}
