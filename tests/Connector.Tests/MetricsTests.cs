using System.Text;
using System.Text.Json;
using BotNexus.Domain.Primitives;
using BotNexus.Domain.World;
using BotNexus.Extensions.HubConnector;
using BotNexus.Gateway.Abstractions.Conversations;
using BotNexus.Gateway.Abstractions.Models;
using BotNexus.Gateway.Abstractions.Sessions;
using BotNexus.Gateway.Diagnostics;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.Logging.Abstractions;

namespace Connector.Tests;

internal static class MetricsTests
{
    public static IEnumerable<(string Name, Func<Task> Run)> All()
    {
        yield return ("metrics default enabled and independent 30 second interval", ConfigDefaults);
        yield return ("metrics config has bounded interval and explicit precedence", ConfigBounds);
        yield return ("disabled metrics do not resolve services or collect", Disabled);
        yield return ("optional metrics preserve v1 and absence is not null JSON", OptionalWire);
        yield return ("metrics wire rejects nonnumeric ranges unknown ids and invalid scope", WireValidation);
        yield return ("metrics point and escaped UTF8 byte caps are enforced", WireBounds);
        yield return ("metrics cannot leave orphan dimensions after snapshot frame trimming", FrameVisibility);
        yield return ("resource gauges are numeric and retain genuine zero", ResourceGauges);
        yield return ("first resource sample has no CPU or IO rates", FirstRates);
        yield return ("process and machine rates use interval deltas and logical CPU", RateDeltas);
        yield return ("counter resets omit rates rather than negative or fake zero", RateReset);
        yield return ("gaps missing counters and nonmonotonic time break rate baselines", RateGaps);
        yield return ("Linux CPU parser excludes guest double counting and validates fields", LinuxCpu);
        yield return ("Linux memory and IO parsing use only exact supported counters", LinuxMemoryIo);
        yield return ("native probe reads only this standalone test process and application filesystem", NativeResources);
        yield return ("cgroup v2 paths resolve own membership and reject host root and traversal", CgroupPaths);
        yield return ("cgroup limits are distinct from host memory and unlimited is unavailable", CgroupLimits);
        yield return ("cgroup source changes cannot mix CPU baselines", CgroupRates);
        yield return ("active loops count only visible allowed top level conversations", ActiveVisibility);
        yield return ("missing active loop capability is unavailable not zero", ActiveUnavailable);
        yield return ("active tracker reflection reads only bounded details not aggregate counts", ActiveReflection);
        yield return ("SQLite reads only passive reflected numeric observations", SqlitePassive);
        yield return ("SQLite missing native provider and broken bindings are isolated", SqliteUnavailable);
        yield return ("run means carry exact weights including zero measured tools", RunWeights);
        yield return ("interval means combine by weights rather than by averaging averages", RunIntervalWeights);
        yield return ("run terminal counts include failed cancelled and unknown terminals", RunOutcomes);
        yield return ("in-place terminal update is not skipped by a forward cursor", RunUpdates);
        yield return ("run identity dedup survives repeated pages and visibility changes", RunDuplicates);
        yield return ("run history before collector start is not backfilled", RunHistory);
        yield return ("terminal arriving during a collection is revisited not lost", RunFuture);
        yield return ("run evidence queries use only visible canonical session bindings", RunBindings);
        yield return ("run evidence scan and state have hard bounds and fair rotation", RunBudget);
        yield return ("dedup and unfinished run caps expose dropped observations", RunCaps);
        yield return ("failed evidence read does not invent a zero", RunFailure);
        yield return ("partial evidence cancellation preserves already observed terminals", RunPartialCancellation);
        yield return ("evidence optional capability is reached through session store only", EvidenceReflection);
        yield return ("snapshots pass only canonical visible session mapping to metrics", SnapshotMapping);
        yield return ("metrics projection failure leaves snapshots and commands working", SnapshotIsolation);
        yield return ("collector keeps boot and sequence stable across snapshot intervals", CollectorIdentity);
        yield return ("collector sources fail independently and reset failed rate baselines", CollectorFailure);
        yield return ("collector rejects stale samples and rechecks current visibility", CollectorVisibility);
        yield return ("collector accounts for unsent completed runs without replay", CollectorDrops);
        yield return ("run diagnostic does not reveal completions removed by visibility", CollectorHiddenDrops);
        yield return ("pending run rechecks rotate fairly under their query budget", RunPendingRotation);
        yield return ("blocked resource collector never blocks snapshots or commands", CollectorNonblocking);
        yield return ("collector shutdown cancels and joins its evidence worker", CollectorLifecycle);
        yield return ("hosted service startup never waits for configuration or connection", ServiceStartup);
    }
    private static void Check(bool condition, string message = "metrics assertion failed")
    { if (!condition) throw new Exception(message); }
    private static Task Sync(Action action) { action(); return Task.CompletedTask; }
    private static HubConnectorOptions Options(bool enabled = true, int interval = 30) => new()
    {
        HubId = "synthetic", Name = "Synthetic", BaseUrl = "https://world.example", Token = "MUST_NOT_EXPORT_CREDENTIAL",
        ConnectUri = new("wss://hub.example/api/connect/synthetic"), AllowedAgents = new HashSet<string>(["helper", "idle"]),
        MetricsEnabled = enabled, MetricsInterval = TimeSpan.FromSeconds(interval)
    };
    private static Dictionary<string, string?> EnvironmentValues() => new()
    {
        ["HUB_URL"] = "https://hub.example", ["HUB_WORLD_ID"] = "synthetic", ["HUB_WORLD_NAME"] = "Synthetic",
        ["HUB_WORLD_BASE_URL"] = "https://world.example", ["HUB_WORLD_TOKEN"] = "MUST_NOT_EXPORT_CREDENTIAL", ["HUB_ALLOWED_AGENTS"] = "helper"
    };
    private static HubConnectorConfiguration ReadConfig(Dictionary<string, string?> env, IConfiguration? config = null)
        => HubConnectorConfigurationReader.Load(key => env.GetValueOrDefault(key), configuration: config);
    private static HubMetricsVisibility Visible(string session = "MUST_NOT_EXPORT_SESSION")
        => new(["helper", "idle"], [new("helper", "c_visible", session)], 0);
    private static HubMetricsEnvelope Envelope(params HubMetricPoint[] points)
        => new("00000000-0000-4000-8000-000000000001", 1, 1_794_931_200_000, 30, points);
    private static HubMetricPoint Point(IEnumerable<HubMetricPoint> points, string id, string? conversation = null)
        => points.Single(point => point.MetricId == id && point.ConversationId == conversation);
    private static void Equal(double expected, double actual)
        => Check(Math.Abs(expected - actual) < 0.00001, $"expected {expected}, got {actual}");
    private static HubResourceObservation Resources(double cpu = 10, double read = 100, double write = 200) => new()
    {
        CpuSeconds = cpu, LogicalProcessors = 2, RssBytes = 1024, PrivateBytes = 2048, HeapBytes = 512,
        GcPausePercent = 1.5, Threads = 8, UptimeSeconds = 120, ReadBytes = read, WriteBytes = write,
        MachineCpu = new(1000, 500), MachineTotalMemory = 10000, MachineAvailableMemory = 2500, DiskTotal = 20000, DiskFree = 5000
    };
    private static HubRunObservation Run(string id, long sequence, DateTimeOffset terminal, int? tools = 0,
        string outcome = "Completed", int durationMs = 1000)
        => new(id, sequence, terminal.AddMilliseconds(-durationMs), terminal, outcome, tools);
    private static AgentDescriptor Agent(string id = "helper") => new()
    {
        AgentId = AgentId.From(id), DisplayName = id, ModelId = "MUST_NOT_EXPORT_MODEL", ApiProvider = "MUST_NOT_EXPORT_PROVIDER"
    };
    private static Conversation Conversation(string id = "c_visible", string agent = "helper", string? session = "MUST_NOT_EXPORT_SESSION")
        => ConversationFactory.CreateForChannel(ConversationId.From(id), AgentId.From(agent), "Safe title", CitizenId.Of(UserId.From("synthetic")))
            with { ActiveSessionId = session is null ? null : SessionId.From(session) };
    private static ConversationSummary Summary(Conversation row) => new(row.ConversationId.Value, row.AgentId.Value,
        row.Title, false, row.Status.ToString(), null, 0, row.CreatedAt, row.UpdatedAt, null,
        row.Kind.ToString(), row.Source.ToString(), row.Visibility.ToString());

    private static Task ConfigDefaults() => Sync(() =>
    {
        var options = ReadConfig(EnvironmentValues()).Options!;
        Check(options.MetricsEnabled && options.MetricsInterval == TimeSpan.FromSeconds(30));
        Check(options.SnapshotInterval == TimeSpan.FromSeconds(15));
        Check(!options.ToString().Contains("MUST_NOT_EXPORT", StringComparison.Ordinal));
    });
    private static Task ConfigBounds() => Sync(() =>
    {
        foreach (var value in new[] { "14", "3601", "30.5", "NaN", "-1", "0", "999999999999999999999999" })
        {
            var env = EnvironmentValues(); env["HUB_METRICS_INTERVAL_SECONDS"] = value;
            Check(ReadConfig(env).Options is null, "accepted invalid interval " + value);
        }
        foreach (var value in new[] { 15, 30, 3600 })
        {
            var env = EnvironmentValues(); env["HUB_METRICS_INTERVAL_SECONDS"] = value.ToString();
            Check(ReadConfig(env).Options?.MetricsInterval.TotalSeconds == value);
        }
        var config = new ConfigurationBuilder().AddInMemoryCollection(new Dictionary<string, string?>
        {
            ["gateway:extensions:botnexus-hub-connector:metricsEnabled"] = "false",
            ["gateway:extensions:botnexus-hub-connector:metricsIntervalSeconds"] = "90"
        }).Build();
        var values = EnvironmentValues(); Check(ReadConfig(values, config).Options is { MetricsEnabled: false, MetricsInterval.TotalSeconds: 90 });
        values["HUB_METRICS_ENABLED"] = "true"; values["HUB_METRICS_INTERVAL_SECONDS"] = "60";
        Check(ReadConfig(values, config).Options is { MetricsEnabled: true, MetricsInterval.TotalSeconds: 60 });
        values["BOTNEXUSHUB_METRICS_ENABLED"] = "false"; values["BOTNEXUSHUB_METRICS_INTERVAL_SECONDS"] = "15";
        Check(ReadConfig(values, config).Options is { MetricsEnabled: false, MetricsInterval.TotalSeconds: 15 });
        values["BOTNEXUSHUB_METRICS_ENABLED"] = "yes"; Check(ReadConfig(values, config).Options is null);
    });
    private static async Task Disabled()
    {
        var services = new MetricsServices(_ => throw new Exception("must not resolve"));
        Check(HubMetricsCollector.Create(Options(false), services) is null && services.Calls.Count == 0);
        var resources = new MetricsResources();
        await using var collector = new HubMetricsCollector(Options(false), resources, new MetricsSqlite(), clock: new MetricsClock());
        collector.UpdateVisibility(Visible()); await collector.CollectOnceAsync();
        Check(collector.Take(Visible()) is null && resources.Calls == 0);
    }
    private static Task OptionalWire() => Sync(() =>
    {
        var snapshot = new HubSnapshot("world", "World", "https://world.example", "1", DateTimeOffset.UtcNow, [], [], []);
        using var old = JsonDocument.Parse(HubProtocol.EncodeSnapshot(snapshot));
        Check(!old.RootElement.TryGetProperty("metrics", out _) && old.RootElement.GetProperty("protocolVersion").GetInt32() == 1);
        var metrics = Envelope(new HubMetricPoint("gateway.memory.rss_bytes", 100));
        using var current = JsonDocument.Parse(HubProtocol.EncodeSnapshot(snapshot with { Metrics = metrics }));
        var root = current.RootElement.GetProperty("metrics");
        Check(root.GetProperty("schemaVersion").GetInt32() == 1 && root.GetProperty("observedAt").ValueKind == JsonValueKind.Number);
        Check(root.GetProperty("points")[0].GetProperty("value").ValueKind == JsonValueKind.Number);
        Check(!Encoding.UTF8.GetString(HubProtocol.Serialize(metrics)).Contains("MUST_NOT_EXPORT", StringComparison.Ordinal));
        using var malformed = JsonDocument.Parse(HubProtocol.EncodeSnapshot(snapshot with { Metrics = metrics with { Points = null! } }));
        Check(!malformed.RootElement.TryGetProperty("metrics", out _) && malformed.RootElement.GetProperty("type").GetString() == "snapshot");
    });
    private static Task WireValidation() => Sync(() =>
    {
        var visibility = Visible();
        var valid = new HubMetricPoint("gateway.memory.rss_bytes", 0);
        foreach (var value in new[] { double.NaN, double.NegativeInfinity, double.PositiveInfinity, -1, 1e19 })
            Check(HubMetricsWire.Bound(Envelope(valid with { Value = value }), visibility) is null);
        foreach (var point in new[] { valid with { MetricId = "arbitrary.sql.text" }, valid with { Weight = 0 }, valid with { Weight = 1_000_001 },
            valid with { AgentId = "helper" }, new("gateway.cpu.percent", 100.1),
            new("agent.loops.tools", 2, AgentId: "denied", ConversationId: "c_visible"),
            new("agent.loops.tools", 2, AgentId: "helper", ConversationId: "c_hidden"),
            new("agent.loops.tools", 2, ConversationId: "c_visible") })
            Check(HubMetricsWire.Bound(Envelope(point), visibility) is null, point.MetricId);
        Check(HubMetricsWire.Bound(Envelope(valid, valid), visibility)?.Points.Count == 1);
        Check(HubMetricsWire.Bound(Envelope(valid) with { IntervalSeconds = 14 }, visibility) is null);
        Check(HubMetricsWire.Bound(Envelope(valid) with { BootId = "bad" }, visibility) is null);
        Check(HubMetricsWire.Bound(Envelope(valid) with { Sequence = 0 }, visibility) is null);
        Check(HubMetricsWire.Bound(Envelope(valid) with { ObservedAt = -1 }, visibility) is null);
        Check(HubMetricsWire.Bound(Envelope(new("gateway.cpu.percent", 100), new("agent.loops.tools", 0, 1_000_000, "helper", "c_visible")), visibility)?.Points.Count == 2);
    });
    private static Task WireBounds() => Sync(() =>
    {
        foreach (var unicode in new[] { false, true })
        {
            var agent = unicode ? new string('\u4e00', 128) : "helper";
            var rows = Enumerable.Range(0, 200).Select(n => new HubMetricsConversation(agent,
                unicode ? "c_" + n + new string('\u4e01', 245) : "c_" + n, null)).ToArray();
            var visibility = new HubMetricsVisibility([agent], rows, 0);
            var points = new List<HubMetricPoint> { new("connector.runs.dropped", 0), new("gateway.loops.active", 200) };
            foreach (var row in rows) points.Add(new("agent.loops.active", 1, AgentId: agent, ConversationId: row.ConversationId));
            foreach (var row in rows)
            {
                points.Add(new("agent.loops.completed", 2, AgentId: agent, ConversationId: row.ConversationId));
                points.Add(new("agent.loops.tools", 2, 2, agent, row.ConversationId));
                points.Add(new("agent.loops.duration_ms", 1000, 2, agent, row.ConversationId));
            }
            var envelope = HubMetricsWire.Bound(Envelope(points.ToArray()), visibility)!;
            Check(envelope.Points.Count <= 512 && HubProtocol.Serialize(envelope).Length <= 64 * 1024);
            if (!unicode) Check(envelope.Points.Count >= 500, "point cap not exercised");
            else Check(envelope.Points.Count < 100, "escaped byte cap not exercised");
            foreach (var group in envelope.Points.Where(point => point.MetricId is "agent.loops.completed" or "agent.loops.tools" or "agent.loops.duration_ms")
                         .GroupBy(point => point.ConversationId))
                Check(group.Count() == 3, "partial run statistics after cap");
        }
    });
    private static Task FrameVisibility() => Sync(() =>
    {
        var choices = Enumerable.Range(0, 20).Select(n => new HubChoice("v" + n, new string('\u4e00', 160), new string('\u4e00', 256))).ToArray();
        var questions = Enumerable.Range(0, 100).Select(n => new HubQuestion("c_" + n, "helper", "Title", "request",
            new string('\u4e00', 4000), "single_choice", choices, false, true)).ToArray();
        var points = questions.Select(question => new HubMetricPoint("agent.loops.active", 1, AgentId: "helper", ConversationId: question.ConversationId)).ToList();
        points.Insert(0, new("gateway.loops.active", 100));
        points.Add(new("agent.loops.tools", 999, 1, "helper", "c_hidden"));
        var snapshot = new HubSnapshot("world", "World", "https://world.example", "1", DateTimeOffset.UtcNow,
            [new("helper", "Helper", null)], [], questions, Metrics: Envelope(points.ToArray()));
        var bytes = HubProtocol.EncodeSnapshot(snapshot);
        using var json = JsonDocument.Parse(bytes);
        Check(bytes.Length <= HubProtocol.MaxFrameBytes && json.RootElement.GetProperty("truncated").GetBoolean());
        var visible = json.RootElement.GetProperty("questions").EnumerateArray().Select(row => row.GetProperty("conversationId").GetString()).ToHashSet();
        var metrics = json.RootElement.GetProperty("metrics").GetProperty("points").EnumerateArray().ToArray();
        Check(metrics.All(point => point.GetProperty("conversationId").ValueKind == JsonValueKind.Null
            || visible.Contains(point.GetProperty("conversationId").GetString())));
        Check(!metrics.Any(point => point.GetProperty("metricId").GetString() == "gateway.loops.active"));
    });

    private static Task ResourceGauges() => Sync(() =>
    {
        var rates = new HubResourceRates(TimeSpan.FromSeconds(30));
        var points = rates.Observe(Resources() with { RssBytes = 0, PrivateBytes = 0, HeapBytes = 0, GcPausePercent = 0 }, 100);
        Equal(0, Point(points, "gateway.memory.rss_bytes").Value);
        Equal(0, Point(points, "gateway.memory.private_bytes").Value);
        Equal(0, Point(points, "gateway.gc.heap_bytes").Value);
        Equal(0, Point(points, "gateway.gc.pause_percent").Value);
        Equal(7500, Point(points, "machine.memory.used_bytes").Value);
        Equal(75, Point(points, "machine.memory.percent").Value);
        Equal(75, Point(points, "machine.disk.percent").Value);
        Check(points.All(point => double.IsFinite(point.Value) && point.Value >= 0 && point.Weight == 1));
        points = rates.Observe(new() { RssBytes = double.NaN, DiskTotal = 10, DiskFree = 20, MachineTotalMemory = 10, MachineAvailableMemory = -1 }, 130);
        Check(points.Count == 0);
    });
    private static Task FirstRates() => Sync(() =>
    {
        var points = new HubResourceRates(TimeSpan.FromSeconds(30)).Observe(Resources(), 100);
        Check(!points.Any(point => point.MetricId is "gateway.cpu.percent" or "machine.cpu.percent"
            or "gateway.io.read_bytes_per_second" or "gateway.io.write_bytes_per_second"));
    });
    private static Task RateDeltas() => Sync(() =>
    {
        var rates = new HubResourceRates(TimeSpan.FromSeconds(30)); var first = Resources(); rates.Observe(first, 100);
        var second = Resources(cpu: 40, read: 400, write: 800) with { MachineCpu = new(2000, 750) };
        var points = rates.Observe(second, 130);
        Equal(50, Point(points, "gateway.cpu.percent").Value);
        Equal(10, Point(points, "gateway.io.read_bytes_per_second").Value);
        Equal(20, Point(points, "gateway.io.write_bytes_per_second").Value);
        Equal(75, Point(points, "machine.cpu.percent").Value);
        points = rates.Observe(second, 160);
        Equal(0, Point(points, "gateway.cpu.percent").Value);
        Equal(0, Point(points, "gateway.io.read_bytes_per_second").Value);
    });
    private static Task RateReset() => Sync(() =>
    {
        var rates = new HubResourceRates(TimeSpan.FromSeconds(30)); rates.Observe(Resources(), 100);
        var points = rates.Observe(Resources(1, 1, 1) with { MachineCpu = new(1, 1) }, 130);
        Check(!points.Any(point => point.MetricId is "gateway.cpu.percent" or "machine.cpu.percent"
            or "gateway.io.read_bytes_per_second" or "gateway.io.write_bytes_per_second"));
        points = rates.Observe(Resources(31, 301, 601) with { MachineCpu = new(1001, 251) }, 160);
        Equal(50, Point(points, "gateway.cpu.percent").Value); Equal(10, Point(points, "gateway.io.read_bytes_per_second").Value);
        rates.Reset(); Check(!rates.Observe(Resources(), 190).Any(point => point.MetricId == "gateway.cpu.percent"));
    });
    private static Task RateGaps() => Sync(() =>
    {
        var rates = new HubResourceRates(TimeSpan.FromSeconds(30)); rates.Observe(Resources(), 100);
        Check(!rates.Observe(Resources(20, 200, 400), 200).Any(point => point.MetricId == "gateway.cpu.percent"));
        Check(rates.Observe(Resources(50, 500, 1000), 230).Any(point => point.MetricId == "gateway.cpu.percent"));
        Check(!rates.Observe(Resources(60), 229).Any(point => point.MetricId == "gateway.cpu.percent"));
        rates.Observe(Resources() with { CpuSeconds = null, ReadBytes = null }, 260);
        var points = rates.Observe(Resources(), 290);
        Check(!points.Any(point => point.MetricId is "gateway.cpu.percent" or "gateway.io.read_bytes_per_second"));
        points = rates.Observe(Resources() with { LogicalProcessors = 4 }, 320);
        Check(!points.Any(point => point.MetricId == "gateway.cpu.percent"));
    });
    private static Task LinuxCpu() => Sync(() =>
    {
        var cpu = HubLinuxMetrics.Cpu("cpu 100 20 30 400 50 6 7 8 80 10\ncpu0 1 1 1 1");
        Check(cpu is not null); Equal(621, cpu!.Value.Total); Equal(450, cpu.Value.Idle);
        foreach (var text in new[] { "", "cpu0 1 2 3 4", "cpu 1 2 3", "cpu -1 2 3 4", "cpu NaN 2 3 4", "cpu 1 bad 3 4" })
            Check(HubLinuxMetrics.Cpu(text) is null, text);
    });
    private static Task LinuxMemoryIo() => Sync(() =>
    {
        var (total, available) = HubLinuxMetrics.Memory("MemTotal: 1024 kB\nMemFree: 1 kB\nMemAvailable: 256 kB\nSwapTotal: 9999 kB");
        Equal(1048576, total!.Value); Equal(262144, available!.Value);
        Check(HubLinuxMetrics.Memory("MemTotal: 1 MB\nMemFree: 2 kB").Total is null);
        Check(HubLinuxMetrics.Memory("MemTotal: 1 kB\nMemFree: 2 kB").Available is null);
        var io = HubLinuxMetrics.Io("rchar: 999\nwchar: 999\nread_bytes: 0\nwrite_bytes: 100\ncancelled_write_bytes: 50");
        Check(io.Read == 0 && io.Write == 100);
        Check(HubLinuxMetrics.Io("read_bytes: -1\nwrite_bytes: nan").Read is null);
    });
    private static Task NativeResources() => Sync(() =>
    {
        // This is the disposable test executable, never the running BotNexus process.
        var sample = new HubResourceProbe().Capture();
        Check(sample.CpuSeconds is >= 0 && sample.RssBytes is > 0 && sample.HeapBytes is >= 0 && sample.Threads is > 0);
        Check(sample.UptimeSeconds is >= 0 && sample.LogicalProcessors > 0 && sample.GcPausePercent is >= 0 and <= 100);
        if (OperatingSystem.IsWindows() || OperatingSystem.IsLinux())
        {
            Check(sample.PrivateBytes is >= 0 && sample.ReadBytes is >= 0 && sample.WriteBytes is >= 0);
            Check(sample.MachineTotalMemory is > 0 && sample.MachineAvailableMemory is >= 0);
            Check(sample.DiskTotal is > 0 && sample.DiskFree is >= 0 && sample.DiskFree <= sample.DiskTotal);
        }
        var points = new HubResourceRates(TimeSpan.FromSeconds(30)).Observe(sample, 100);
        Check(points.All(point => HubMetricCatalog.Valid(point, HubMetricsVisibility.Empty)));
    });
    private static Task CgroupPaths() => Sync(() =>
    {
        const string mount = "29 23 0:26 / /sys/fs/cgroup rw - cgroup2 cgroup rw";
        Check(HubLinuxMetrics.CgroupDirectory("0::/docker/abc", mount)?.Directory == "/sys/fs/cgroup/docker/abc");
        Check(HubLinuxMetrics.CgroupDirectory("0::/", "29 23 0:26 /docker/abc /sys/fs/cgroup ro - cgroup2 cgroup rw")?.Directory == "/sys/fs/cgroup");
        Check(HubLinuxMetrics.CgroupDirectory("0::/docker/abc/child", "29 23 0:26 /docker/abc /cg ro - cgroup2 cgroup rw")?.Directory == "/cg/child");
        Check(HubLinuxMetrics.CgroupDirectory("0::/", mount) is null);
        Check(HubLinuxMetrics.CgroupDirectory("1:memory:/docker/abc", mount) is null);
        Check(HubLinuxMetrics.CgroupDirectory("0::/docker/../secret", mount) is null);
        Check(HubLinuxMetrics.CgroupDirectory("0::/abc", "29 23 0:26 /different /cg ro - cgroup2 cgroup rw") is null);
        Check(HubLinuxMetrics.CgroupDirectory("0::/abc", "29 23 0:26 / /cg rw - cgroup cgroup rw") is null);
    });
    private static Task CgroupLimits() => Sync(() =>
    {
        var files = new Dictionary<string, string>
        {
            ["/proc/self/cgroup"] = "0::/docker/abc", ["/proc/self/mountinfo"] = "29 23 0:26 / /sys/fs/cgroup rw - cgroup2 cgroup rw",
            ["/sys/fs/cgroup/docker/abc/memory.current"] = "256", ["/sys/fs/cgroup/docker/abc/memory.max"] = "1024",
            ["/sys/fs/cgroup/docker/abc/cpu.max"] = "50000 100000", ["/sys/fs/cgroup/docker/abc/cpu.stat"] = "usage_usec 2000000\nuser_usec 1"
        };
        var calls = new List<string>();
        string? Read(string path) { calls.Add(path); return files.GetValueOrDefault(path); }
        var container = HubLinuxMetrics.Container(Read, 8192)!;
        Check(container.MemoryUsed == 256 && container.MemoryLimit == 1024 && container.CpuCapacity == .5 && container.CpuSeconds == 2);
        var points = new HubResourceRates(TimeSpan.FromSeconds(30)).Observe(Resources() with { Container = container }, 100);
        Equal(1024, Point(points, "container.memory.total_bytes").Value);
        Equal(10000, Point(points, "machine.memory.total_bytes").Value);
        files["/sys/fs/cgroup/docker/abc/memory.max"] = "max";
        files["/sys/fs/cgroup/docker/abc/cpu.max"] = "max 100000";
        container = HubLinuxMetrics.Container(Read, 8192)!;
        Check(container.MemoryLimit is null && container.CpuCapacity is null && container.MemoryUsed == 256);
        points = new HubResourceRates(TimeSpan.FromSeconds(30)).Observe(Resources() with { Container = container }, 100);
        Check(!points.Any(point => point.MetricId is "container.memory.total_bytes" or "container.memory.percent"));
        files["/sys/fs/cgroup/docker/abc/memory.max"] = "999999999999999999";
        Check(HubLinuxMetrics.Container(Read, 8192)?.MemoryLimit is null);
        Check(calls.All(path => path.StartsWith("/proc/self/", StringComparison.Ordinal) || path.StartsWith("/sys/fs/cgroup/docker/abc/", StringComparison.Ordinal)));
    });
    private static Task CgroupRates() => Sync(() =>
    {
        var rates = new HubResourceRates(TimeSpan.FromSeconds(30));
        var first = new HubResourceObservation { Container = new("A", 10, .5, 1000, 0) };
        var points = rates.Observe(first, 100);
        Equal(0, Point(points, "container.memory.used_bytes").Value);
        Check(!points.Any(point => point.MetricId == "container.cpu.percent"));
        points = rates.Observe(first with { Container = new("A", 17.5, .5, 1000, 250) }, 130);
        Equal(50, Point(points, "container.cpu.percent").Value);
        points = rates.Observe(first with { Container = new("B", 100, .5, 1000, 250) }, 160);
        Check(!points.Any(point => point.MetricId == "container.cpu.percent"));
        points = rates.Observe(first with { Container = new("B", 1, .5, 1000, 250) }, 190);
        Check(!points.Any(point => point.MetricId == "container.cpu.percent"));
    });

    private static Task ActiveVisibility() => Sync(() =>
    {
        var loops = new HubActiveLoop[] { new("one", "helper", "c_visible"), new("two", "helper", "c_visible"),
            new("two", "helper", "c_visible"), new("hidden", "helper", "c_hidden"), new("denied", "denied", "c_visible"),
            new("unknown", "helper", null), new("no-owner", null, "c_visible") };
        var points = HubLoopMetrics.Active(loops, Visible());
        Equal(2, Point(points, "gateway.loops.active").Value);
        Equal(2, Point(points, "agent.loops.active", "c_visible").Value);
        Check(points.Single(point => point.AgentId == "idle").Value == 0);
        Check(!points.Any(point => point.AgentId == "helper" && point.ConversationId is null));
        var json = Encoding.UTF8.GetString(HubProtocol.Serialize(points));
        Check(!json.Contains("c_hidden", StringComparison.Ordinal) && !json.Contains("denied", StringComparison.Ordinal) && !json.Contains("LoopId", StringComparison.Ordinal));
    });
    private static async Task ActiveUnavailable()
    {
        Check(HubGatewayMetrics.ActiveLoops(null) is null);
        var clock = new MetricsClock();
        await using var collector = new HubMetricsCollector(Options(), new MetricsResources(), new MetricsSqlite(), clock: clock, startWorker: false);
        collector.UpdateVisibility(Visible()); await collector.CollectOnceAsync();
        Check(!collector.Take(Visible())!.Points.Any(point => point.MetricId is "gateway.loops.active" or "agent.loops.active"));
        await using var zero = new HubMetricsCollector(Options(), new MetricsResources(), new MetricsSqlite(), new MetricsActive(), clock: clock, startWorker: false);
        zero.UpdateVisibility(Visible()); await zero.CollectOnceAsync();
        Equal(0, Point(zero.Take(Visible())!.Points, "gateway.loops.active").Value);
    }
    private static Task ActiveReflection() => Sync(() =>
    {
        var tracker = new MetricsTracker
        {
            Loops = [new() { LoopId = "one", AgentId = "helper", ConversationId = "c_visible", SessionId = "MUST_NOT_EXPORT_SESSION", StartedAtUtc = DateTimeOffset.UtcNow },
                new() { LoopId = "hidden", AgentId = "denied", ConversationId = "c_hidden", SessionId = "MUST_NOT_EXPORT_HIDDEN_SESSION", StartedAtUtc = DateTimeOffset.UtcNow }]
        };
        var services = new MetricsServices(type => type == typeof(IActiveLoopTracker) ? tracker : null);
        var reader = HubGatewayMetrics.ActiveLoops(services)!;
        var points = HubLoopMetrics.Active(reader.Capture(), Visible());
        Equal(1, Point(points, "gateway.loops.active").Value);
        Check(services.Calls.SequenceEqual([typeof(IActiveLoopTracker)]));
        Check(!Encoding.UTF8.GetString(HubProtocol.Serialize(points)).Contains("MUST_NOT_EXPORT", StringComparison.Ordinal));
        tracker.Loops = Enumerable.Range(0, 4097).Select(n => new ActiveLoopDetail
        { LoopId = "loop" + n, AgentId = "helper", ConversationId = "c_visible", StartedAtUtc = DateTimeOffset.UtcNow }).ToArray();
        try { reader.Capture(); throw new Exception("unbounded active loops accepted"); } catch (InvalidOperationException) { }
        Check(HubGatewayMetrics.ActiveLoops(new MetricsServices(_ => throw new TypeLoadException())) is null);
    });
    private static Task SqlitePassive() => Sync(() =>
    {
        MetricsAllocator.Available = true;
        var types = new List<string>();
        var probe = new HubSqliteMetrics(name => { types.Add(name); return name.EndsWith("SqliteConnectionFactory", StringComparison.Ordinal) ? typeof(MetricsConnectionFactory) : typeof(MetricsAllocator); });
        var points = probe.Capture();
        Check(points.Count == 6 && types.Count == 2);
        Equal(0, Point(points, "sqlite.connections.current").Value);
        Equal(12, Point(points, "sqlite.connections.opened").Value);
        Equal(0, Point(points, "sqlite.memory.current_bytes").Value);
        Check(!Encoding.UTF8.GetString(HubProtocol.Serialize(points)).Contains("MUST_NOT_EXPORT", StringComparison.Ordinal));
    });
    private static Task SqliteUnavailable() => Sync(() =>
    {
        Check(new HubSqliteMetrics(_ => null).Capture().Count == 0);
        Check(new HubSqliteMetrics(_ => typeof(MetricsBrokenSqlite)).Capture().Count == 0);
        MetricsAllocator.Available = false;
        Check(new HubSqliteMetrics(_ => typeof(MetricsAllocator)).Capture().Count == 0);
        MetricsAllocator.Available = true;
        Check(HubGatewayMetrics.RunEvidence(new MetricsServices(_ => throw new TypeLoadException())) is null);
    });

    private static async Task RunWeights()
    {
        var clock = new MetricsClock(); var evidence = new MetricsEvidence(); var start = clock.UtcNow;
        evidence.Rows["s"] = [Run("zero", 1, start.AddSeconds(1), 0, durationMs: 1000), Run("eight", 2, start.AddSeconds(2), 8, durationMs: 3000),
            Run("unknown", 3, start.AddSeconds(3), null, durationMs: 5000)];
        var runs = new HubRunMetrics(evidence, start); var points = await runs.CollectAsync(Visible("s"), start.AddSeconds(30), default);
        var tools = Point(points, "agent.loops.tools", "c_visible"); Equal(4, tools.Value); Check(tools.Weight == 2);
        var duration = Point(points, "agent.loops.duration_ms", "c_visible"); Equal(3000, duration.Value); Check(duration.Weight == 3);
        Equal(3, Point(points, "agent.loops.completed", "c_visible").Value);
        Check(points.All(point => point.AgentId == "helper" && point.ConversationId == "c_visible"));
    }
    private static async Task RunIntervalWeights()
    {
        var now = new MetricsClock().UtcNow; var evidence = new MetricsEvidence();
        evidence.Rows["s"] = [Run("one", 1, now.AddSeconds(1), 10, durationMs: 10000)];
        var runs = new HubRunMetrics(evidence, now);
        var first = await runs.CollectAsync(Visible("s"), now.AddSeconds(30), default);
        evidence.Rows["s"].AddRange([Run("two", 2, now.AddSeconds(31), 0), Run("three", 3, now.AddSeconds(32), 0), Run("four", 4, now.AddSeconds(33), 0)]);
        var second = await runs.CollectAsync(Visible("s"), now.AddSeconds(60), default);
        foreach (var id in new[] { "agent.loops.tools", "agent.loops.duration_ms" })
        {
            var a = Point(first, id, "c_visible"); var b = Point(second, id, "c_visible");
            Check(a.Weight == 1 && b.Weight == 3);
            Equal(id == "agent.loops.tools" ? 2.5 : 3250, (a.Value * a.Weight + b.Value * b.Weight) / (a.Weight + b.Weight));
        }
        Equal(4, Point(first, "agent.loops.completed", "c_visible").Value + Point(second, "agent.loops.completed", "c_visible").Value);
    }
    private static async Task RunOutcomes()
    {
        var start = new MetricsClock().UtcNow; var evidence = new MetricsEvidence();
        evidence.Rows["s"] = [Run("ok", 1, start.AddSeconds(1), 0), Run("parked", 2, start.AddSeconds(2), 4, "Parked"),
            Run("failed", 3, start.AddSeconds(3), null, "Failed"), Run("cancel", 4, start.AddSeconds(4), null, "Cancelled"),
            Run("unknown", 5, start.AddSeconds(5), 999, "Unknown")];
        var points = await new HubRunMetrics(evidence, start).CollectAsync(Visible("s"), start.AddSeconds(30), default);
        Equal(5, Point(points, "agent.loops.completed", "c_visible").Value);
        Equal(2, Point(points, "agent.loops.tools", "c_visible").Value);
        Check(Point(points, "agent.loops.tools", "c_visible").Weight == 2 && Point(points, "agent.loops.duration_ms", "c_visible").Weight == 5);
    }
    private static async Task RunUpdates()
    {
        var start = new MetricsClock().UtcNow; var evidence = new MetricsEvidence();
        evidence.Rows["s"] = [new("pending", 1, start, null, "Running", null), Run("fast", 2, start.AddSeconds(1), 2)];
        var runs = new HubRunMetrics(evidence, start);
        var first = await runs.CollectAsync(Visible("s"), start.AddSeconds(30), default);
        Equal(1, Point(first, "agent.loops.completed", "c_visible").Value); Check(runs.PendingCount == 1);
        evidence.Rows["s"][0] = Run("pending", 1, start.AddSeconds(40), 0, durationMs: 40000);
        var second = await runs.CollectAsync(Visible("s"), start.AddSeconds(60), default);
        Equal(1, Point(second, "agent.loops.completed", "c_visible").Value);
        Equal(0, Point(second, "agent.loops.tools", "c_visible").Value);
        Check(runs.PendingCount == 0 && evidence.Calls.Any(call => call.After == 2) && evidence.Calls.Any(call => call.Limit == 1 && call.After == 0));
        var third = await runs.CollectAsync(Visible("s"), start.AddSeconds(90), default);
        Equal(0, Point(third, "agent.loops.completed", "c_visible").Value);
        Check(!third.Any(point => point.MetricId is "agent.loops.tools" or "agent.loops.duration_ms"));
    }
    private static async Task RunDuplicates()
    {
        var start = new MetricsClock().UtcNow; var evidence = new MetricsEvidence();
        evidence.Rows["s"] = [Run("same", 1, start.AddSeconds(1), 4), Run("same", 2, start.AddSeconds(1), 4)];
        var runs = new HubRunMetrics(evidence, start);
        Equal(1, Point(await runs.CollectAsync(Visible("s"), start.AddSeconds(30), default), "agent.loops.completed", "c_visible").Value);
        await runs.CollectAsync(HubMetricsVisibility.Empty, start.AddSeconds(60), default);
        var again = await runs.CollectAsync(Visible("s"), start.AddSeconds(90), default);
        Equal(0, Point(again, "agent.loops.completed", "c_visible").Value); Check(runs.SeenCount == 1);
    }
    private static async Task RunHistory()
    {
        var start = new MetricsClock().UtcNow; var evidence = new MetricsEvidence();
        evidence.Rows["s"] = [Run("historic", 1, start.AddSeconds(-1), 100), Run("new", 2, start.AddSeconds(1), 0)];
        var points = await new HubRunMetrics(evidence, start).CollectAsync(Visible("s"), start.AddSeconds(30), default);
        Equal(1, Point(points, "agent.loops.completed", "c_visible").Value); Equal(0, Point(points, "agent.loops.tools", "c_visible").Value);
    }
    private static async Task RunFuture()
    {
        var start = new MetricsClock().UtcNow; var evidence = new MetricsEvidence();
        evidence.Rows["s"] = [Run("during-query", 1, start.AddSeconds(31), 0)];
        var runs = new HubRunMetrics(evidence, start);
        var first = await runs.CollectAsync(Visible("s"), start.AddSeconds(30), default);
        Check(first.All(point => point.MetricId is not ("agent.loops.tools" or "agent.loops.completed"))); Check(runs.PendingCount == 1);
        var next = await runs.CollectAsync(Visible("s"), start.AddSeconds(60), default);
        Equal(1, Point(next, "agent.loops.completed", "c_visible").Value); Check(runs.PendingCount == 0);
    }
    private static async Task RunBindings()
    {
        var evidence = new MetricsEvidence(); var now = new MetricsClock().UtcNow;
        var visibility = new HubMetricsVisibility(["helper"], [new("helper", "c_known", "s_known"), new("helper", "c_none", null),
            new("denied", "c_denied", "s_denied"), new("helper", "c_ambiguous1", "s_ambiguous"), new("helper", "c_ambiguous2", "s_ambiguous")], 0);
        await new HubRunMetrics(evidence, now).CollectAsync(visibility, now.AddSeconds(30), default);
        Check(evidence.Calls.Count == 1 && evidence.Calls[0].Session == "s_known");
    }
    private static async Task RunBudget()
    {
        var evidence = new MetricsEvidence(); var now = new MetricsClock().UtcNow;
        var rows = Enumerable.Range(0, 100).Select(n => new HubMetricsConversation("helper", "c_" + n, "s_" + n)).ToArray();
        var visibility = new HubMetricsVisibility(["helper"], rows, 0);
        foreach (var row in rows) evidence.Rows[row.SessionId!] = Enumerable.Range(1, 200).Select(n => Run("r_" + n, n, now.AddSeconds(-n))).ToList();
        var runs = new HubRunMetrics(evidence, now, maxQueries: 8, pageSize: 16);
        for (var n = 0; n < 13; n++)
        {
            var count = evidence.Calls.Count;
            await runs.CollectAsync(visibility, now.AddSeconds(30 + n), default);
            Check(evidence.Calls.Count - count <= 8 && runs.SessionCount <= 200 && runs.PendingCount <= 512 && runs.SeenCount <= 8192);
        }
        Check(evidence.Calls.All(call => call.Limit is > 0 and <= 16));
        Check(evidence.Calls.Select(call => call.Session).Distinct().Count() == 100);
        await runs.CollectAsync(HubMetricsVisibility.Empty, now.AddSeconds(60), default); Check(runs.SessionCount == 0);
    }
    private static async Task RunCaps()
    {
        var now = new MetricsClock().UtcNow; var evidence = new MetricsEvidence();
        evidence.Rows["s"] = [Run("one", 1, now.AddSeconds(1)), Run("two", 2, now.AddSeconds(2)), Run("three", 3, now.AddSeconds(3))];
        var runs = new HubRunMetrics(evidence, now, maxSeen: 2);
        var first = await runs.CollectAsync(Visible("s"), now.AddSeconds(30), default);
        Equal(2, Point(first, "agent.loops.completed", "c_visible").Value); Check(runs.Dropped == 1 && runs.SeenCount == 2);
        Check((await runs.CollectAsync(Visible("s"), now.AddSeconds(60), default)).Count == 0, "cap cannot invent a complete zero interval");
        evidence = new MetricsEvidence(); evidence.Rows["s"] = [new("one", 1, now, null, "Running", null), new("two", 2, now, null, "Running", null)];
        runs = new HubRunMetrics(evidence, now, maxPending: 1);
        await runs.CollectAsync(Visible("s"), now.AddSeconds(30), default); Check(runs.Dropped == 1 && runs.PendingCount == 1);
        evidence.Rows["s"][0] = Run("one", 1, now.AddSeconds(40));
        Equal(1, Point(await runs.CollectAsync(Visible("s"), now.AddSeconds(60), default), "agent.loops.completed", "c_visible").Value);
    }
    private static async Task RunFailure()
    {
        var now = new MetricsClock().UtcNow; var evidence = new MetricsEvidence { Failure = new IOException("MUST_NOT_EXPORT_QUERY") };
        var runs = new HubRunMetrics(evidence, now);
        Check((await runs.CollectAsync(Visible("s"), now.AddSeconds(30), default)).Count == 0);
        evidence.Failure = null;
        Equal(0, Point(await runs.CollectAsync(Visible("s"), now.AddSeconds(60), default), "agent.loops.completed", "c_visible").Value);
    }
    private static async Task RunPartialCancellation()
    {
        var now = new MetricsClock().UtcNow; var evidence = new MetricsEvidence();
        evidence.Rows["s"] = [Run("done", 1, now.AddSeconds(1)), new("pending", 2, now, null, "Running", null)];
        using var cancel = new CancellationTokenSource();
        evidence.BeforeQuery = (_, limit, _, _) => { if (limit == 1) cancel.Cancel(); };
        var runs = new HubRunMetrics(evidence, now);
        var points = await runs.CollectAsync(Visible("s"), now.AddSeconds(30), cancel.Token);
        Equal(1, Point(points, "agent.loops.completed", "c_visible").Value); Check(runs.SeenCount == 1);
        evidence.BeforeQuery = null;
        var next = await runs.CollectAsync(Visible("s"), now.AddSeconds(60), default);
        Equal(0, Point(next, "agent.loops.completed", "c_visible").Value);
    }
    private static async Task EvidenceReflection()
    {
        // Force only the synthetic SDK's already referenced contracts into this test process, not a host DLL path.
        _ = typeof(ISessionStore); _ = typeof(IAgentRunEvidenceStore);
        var (store, fake) = MetricsSessionProxy.New(); var now = new MetricsClock().UtcNow;
        fake.Runs.Add(new(AgentRunId.From("opaque-run"), now, now.AddSeconds(1), "Completed", 0, [], 7));
        var services = new MetricsServices(type => type == typeof(ISessionStore) ? store : throw new Exception("unexpected resolution"));
        var reader = HubGatewayMetrics.RunEvidence(services); Check(reader is not null && services.Calls.SequenceEqual([typeof(ISessionStore)]));
        var page = await reader!.QueryAsync("MUST_NOT_EXPORT_SESSION", 12, 0, default);
        Check(page.Runs.Single().CompletedResultCount == 0 && page.Runs.Single().Sequence == 7);
        Check(fake.Calls.Single() == ("MUST_NOT_EXPORT_SESSION", 12, 0));
        var (legacy, _) = MetricsSessionProxy.New(false);
        Check(HubGatewayMetrics.RunEvidence(new MetricsServices(type => type == typeof(ISessionStore) ? legacy : null)) is null);
        Check(HubGatewayMetrics.RunEvidence(null) is null);
    }

    private static async Task SnapshotMapping()
    {
        var (store, fake) = StoreProxy.New(); var good = Conversation();
        var rows = new[] { good, Conversation("c_hidden") with { Visibility = ConversationVisibility.InternalHidden },
            Conversation("c_nested") with { ParentConversationId = good.ConversationId }, Conversation("c_archived") with { Status = ConversationStatus.Archived },
            Conversation("c_denied", "denied") };
        foreach (var row in rows) { fake.Rows[row.ConversationId.Value] = row; fake.Summaries.Add(Summary(row)); }
        var metrics = new MetricsSnapshot();
        var snapshot = await new HubSnapshotBuilder(Options(), store, new FakeAgents(Agent(), Agent("denied")), new FakeWorld(), metrics).BuildAsync();
        Check(snapshot.Conversations.Count == 1 && metrics.Visibility?.Conversations.Count == 1);
        Check(metrics.Visibility!.Conversations["c_visible"].SessionId == "MUST_NOT_EXPORT_SESSION");
        Check(!Encoding.UTF8.GetString(HubProtocol.EncodeSnapshot(snapshot)).Contains("MUST_NOT_EXPORT", StringComparison.Ordinal));
        Check(fake.Calls.All(call => call is "GetAsync" or "GetSummariesAsync" or "GetPendingAskUserCheckpointsAsync"));
    }
    private static async Task SnapshotIsolation()
    {
        var (store, fake) = StoreProxy.New(); var row = Conversation(); fake.Rows[row.ConversationId.Value] = row; fake.Summaries.Add(Summary(row));
        var snapshot = await new HubSnapshotBuilder(Options(), store, new FakeAgents(Agent()), new FakeWorld(), new MetricsSnapshot { Throws = true }).BuildAsync();
        Check(snapshot.Conversations.Count == 1 && snapshot.Metrics is null);
        var queue = new FakeOrchestrator(); var commands = new HubCommandProcessor(Options(), store, new FakeAgents(Agent()), queue, new FakeWorld());
        var result = await commands.ProcessAsync(new("00000000-0000-4000-8000-000000000001", "send_message", new("helper", "c_visible", Message: "Hello")));
        Check(result.Ok && queue.Messages.Count == 1);
    }
    private static async Task CollectorIdentity()
    {
        var clock = new MetricsClock(); var visibility = Visible();
        await using var collector = new HubMetricsCollector(Options(), new MetricsResources { Current = Resources() }, new MetricsSqlite(), clock: clock, startWorker: false);
        collector.UpdateVisibility(visibility); Check(collector.Take(visibility) is null);
        await collector.CollectOnceAsync(); var first = collector.Take(visibility)!;
        Check(first.Sequence == 1 && Guid.TryParseExact(first.BootId, "D", out _) && first.ObservedAt == clock.UtcNow.ToUnixTimeMilliseconds());
        Check(collector.Take(visibility) is null); clock.Advance(15); await collector.CollectOnceAsync();
        Check(collector.Take(visibility) is null, "metrics sent more often than interval");
        clock.Advance(15); var second = collector.Take(visibility)!;
        Check(first.BootId == second.BootId && second.Sequence > first.Sequence && second.IntervalSeconds == 30);
        await using var fresh = new HubMetricsCollector(Options(), new MetricsResources(), new MetricsSqlite(), clock: clock, startWorker: false);
        fresh.UpdateVisibility(visibility); await fresh.CollectOnceAsync(); Check(fresh.Take(visibility)!.BootId != first.BootId);
    }
    private static async Task CollectorFailure()
    {
        var clock = new MetricsClock(); var resources = new MetricsResources { Current = Resources() }; var active = new MetricsActive();
        var sqlite = new MetricsSqlite { Failure = new IOException("MUST_NOT_EXPORT_PROVIDER") };
        await using var collector = new HubMetricsCollector(Options(), resources, sqlite, active, clock: clock, startWorker: false);
        collector.UpdateVisibility(Visible()); await collector.CollectOnceAsync();
        Check(collector.Take(Visible())!.Points.Any(point => point.MetricId == "gateway.memory.rss_bytes"));
        clock.Advance(30); resources.Failure = new IOException("MUST_NOT_EXPORT_PROCESS"); active.Failure = new IOException("MUST_NOT_EXPORT_LOOPS");
        await collector.CollectOnceAsync(); var failed = collector.Take(Visible())!;
        Check(!failed.Points.Any(point => point.MetricId is "gateway.memory.rss_bytes" or "gateway.loops.active" or "gateway.cpu.percent"));
        Check(!Encoding.UTF8.GetString(HubProtocol.Serialize(failed)).Contains("MUST_NOT_EXPORT", StringComparison.Ordinal));
        clock.Advance(30); resources.Failure = null; resources.Current = Resources(100, 10000, 20000);
        await collector.CollectOnceAsync(); Check(!collector.Take(Visible())!.Points.Any(point => point.MetricId == "gateway.cpu.percent"));
    }
    private static async Task CollectorVisibility()
    {
        var clock = new MetricsClock(); var active = new MetricsActive { Loops = [new("one", "helper", "c_visible")] };
        await using var collector = new HubMetricsCollector(Options(), new MetricsResources(), new MetricsSqlite(), active, clock: clock, startWorker: false);
        collector.UpdateVisibility(Visible()); await collector.CollectOnceAsync();
        var hiddenNow = new HubMetricsVisibility(["helper", "idle"], [], 0); var sample = collector.Take(hiddenNow)!;
        Check(!sample.Points.Any(point => point.ConversationId is not null || point.MetricId == "gateway.loops.active"));
        clock.Advance(30); await collector.CollectOnceAsync(); clock.Advance(100);
        Check(collector.Take(Visible()) is null);
    }
    private static async Task CollectorDrops()
    {
        var clock = new MetricsClock(); var evidence = new MetricsEvidence();
        await using var collector = new HubMetricsCollector(Options(), new MetricsResources(), new MetricsSqlite(), runs: evidence, clock: clock, startWorker: false);
        evidence.Rows["s"] = [Run("one", 1, clock.UtcNow.AddSeconds(1), 0)];
        collector.UpdateVisibility(Visible("s")); clock.Advance(30); await collector.CollectOnceAsync();
        clock.Advance(30); await collector.CollectOnceAsync(); // The first complete sample was never sent.
        var sample = collector.Take(Visible("s"))!;
        Equal(1, Point(sample.Points, "connector.runs.dropped").Value);
        Equal(0, Point(sample.Points, "agent.loops.completed", "c_visible").Value);
    }
    private static async Task CollectorHiddenDrops()
    {
        var clock = new MetricsClock(); var evidence = new MetricsEvidence();
        await using var collector = new HubMetricsCollector(Options(), new MetricsResources(), new MetricsSqlite(), runs: evidence, clock: clock, startWorker: false);
        evidence.Rows["s"] = [Run("one", 1, clock.UtcNow.AddSeconds(1), 0)];
        collector.UpdateVisibility(Visible("s")); clock.Advance(30); await collector.CollectOnceAsync();
        var hiddenNow = new HubMetricsVisibility(["helper", "idle"], [], 0);
        var sample = collector.Take(hiddenNow)!;
        Equal(0, Point(sample.Points, "connector.runs.dropped").Value);
        Check(sample.Points.All(point => point.ConversationId is null));
        clock.Advance(30); collector.UpdateVisibility(hiddenNow); await collector.CollectOnceAsync();
        Equal(0, Point(collector.Take(hiddenNow)!.Points, "connector.runs.dropped").Value);
    }
    private static async Task RunPendingRotation()
    {
        var now = new MetricsClock().UtcNow; var evidence = new MetricsEvidence();
        evidence.Rows["s"] = Enumerable.Range(1, 20).Select(n => new HubRunObservation("run" + n, n, now, null, "Running", null)).ToList();
        var runs = new HubRunMetrics(evidence, now, maxQueries: 4, pageSize: 64);
        await runs.CollectAsync(Visible("s"), now.AddSeconds(30), default);
        evidence.Rows["s"][19] = Run("run20", 20, now.AddSeconds(40), 0);
        var completed = 0d;
        for (var n = 0; n < 8; n++)
        {
            var calls = evidence.Calls.Count;
            var points = await runs.CollectAsync(Visible("s"), now.AddSeconds(60 + n * 30), default);
            completed += points.Where(point => point.MetricId == "agent.loops.completed").Sum(point => point.Value);
            Check(evidence.Calls.Count - calls <= 4);
        }
        Equal(1, completed);
        Check(runs.PendingCount == 19 && evidence.Calls.Any(call => call.After == 19 && call.Limit == 1));
    }
    private static async Task CollectorNonblocking()
    {
        using var resources = new MetricsBlockedResources(); var clock = new MetricsClock();
        var collector = new HubMetricsCollector(Options(), resources, new MetricsSqlite(), clock: clock);
        try
        {
            Check(!resources.Entered.Task.IsCompleted, "constructor collected before visibility");
            collector.UpdateVisibility(Visible()); await resources.Entered.Task.WaitAsync(TimeSpan.FromSeconds(3));
            var (store, fake) = StoreProxy.New(); var row = Conversation(); fake.Rows[row.ConversationId.Value] = row; fake.Summaries.Add(Summary(row));
            var snapshot = await new HubSnapshotBuilder(Options(), store, new FakeAgents(Agent()), new FakeWorld(), collector).BuildAsync().WaitAsync(TimeSpan.FromSeconds(1));
            Check(snapshot.Metrics is null && snapshot.Conversations.Count == 1);
            var queue = new FakeOrchestrator(); var commands = new HubCommandProcessor(Options(), store, new FakeAgents(Agent()), queue, new FakeWorld());
            var command = new HubCommand("00000000-0000-4000-8000-000000000001", "send_message", new("helper", "c_visible", Message: "Hello"));
            Check((await commands.ProcessAsync(command).WaitAsync(TimeSpan.FromSeconds(1))).Ok);
        }
        finally { resources.Release.Set(); await collector.DisposeAsync(); }
    }
    private static async Task ServiceStartup()
    {
        var (store, fake) = StoreProxy.New(); var env = EnvironmentValues(); env["HUB_METRICS_ENABLED"] = "false";
        using var release = new ManualResetEventSlim(false);
        var reading = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var connecting = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var connectCancelled = false;
        string? Read(string key)
        {
            reading.TrySetResult();
            if (!release.Wait(TimeSpan.FromSeconds(5))) throw new TimeoutException("synthetic test guard");
            return env.GetValueOrDefault(key);
        }
        async Task<System.Net.WebSockets.WebSocket> Connect(HubConnectorOptions _, CancellationToken ct)
        {
            connecting.TrySetResult();
            try { await Task.Delay(Timeout.InfiniteTimeSpan, ct); }
            catch (OperationCanceledException) { connectCancelled = true; throw; }
            return new FakeSocket();
        }
        using var service = new HubConnectorService(store, new FakeAgents(Agent()), new FakeOrchestrator(), new FakeWorld(),
            NullLogger<HubConnectorService>.Instance, null, null, null, Read, Connect);
        try
        {
            await service.StartAsync(default).WaitAsync(TimeSpan.FromSeconds(1));
            await reading.Task.WaitAsync(TimeSpan.FromSeconds(3));
            Check(!connecting.Task.IsCompleted && fake.Calls.Count == 0);
            release.Set();
            await connecting.Task.WaitAsync(TimeSpan.FromSeconds(3));
        }
        finally
        {
            release.Set();
            await service.StopAsync(default).WaitAsync(TimeSpan.FromSeconds(3));
        }
        Check(connectCancelled && fake.Calls.Count == 0);
    }
    private static async Task CollectorLifecycle()
    {
        var evidence = new MetricsBlockedEvidence();
        var collector = new HubMetricsCollector(Options(), new MetricsResources(), new MetricsSqlite(), runs: evidence, clock: new MetricsClock());
        collector.UpdateVisibility(Visible("s"));
        try { await evidence.Entered.Task.WaitAsync(TimeSpan.FromSeconds(3)); }
        finally { await collector.DisposeAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(3)); }
        Check(evidence.Cancelled);
    }
}
