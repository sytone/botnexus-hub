using System.Collections;
using System.Reflection;
using BotNexus.Domain.Primitives;

namespace BotNexus.Extensions.HubConnector;

internal sealed record HubActiveLoop(string Id, string? AgentId, string? ConversationId);
internal interface IHubActiveLoops { IReadOnlyList<HubActiveLoop> Capture(); }
internal sealed record HubRunObservation(string Id, long Sequence, DateTimeOffset StartedAt,
    DateTimeOffset? CompletedAt, string Outcome, int? CompletedResultCount);
internal sealed record HubRunPage(IReadOnlyList<HubRunObservation> Runs, bool HasMore);
internal interface IHubRunEvidence
{
    Task<HubRunPage> QueryAsync(string sessionId, int limit, long afterSequence, CancellationToken ct);
}
internal interface IHubSqliteMetrics { IReadOnlyList<HubMetricPoint> Capture(); }

// Optional capabilities are resolved from assemblies the host has already loaded. Never probe DLL paths,
// load a provider, open a DB, or put optional types in the hosted service's constructor signature.
internal static class HubGatewayMetrics
{
    internal static Type? LoadedType(string name) => System.Runtime.Loader.AssemblyLoadContext.Default.Assemblies
        .Select(assembly => assembly.GetType(name, throwOnError: false)).FirstOrDefault(type => type is not null);
    internal static IHubActiveLoops? ActiveLoops(IServiceProvider? services)
    {
        try
        {
            var type = LoadedType("BotNexus.Gateway.Diagnostics.IActiveLoopTracker");
            var service = type is null ? null : services?.GetService(type);
            var method = type?.GetMethod("GetSnapshot", Type.EmptyTypes);
            return service is null || method is null ? null : new ReflectedActiveLoops(service, method);
        }
        catch (Exception) { return null; }
    }
    internal static IHubRunEvidence? RunEvidence(IServiceProvider? services)
    {
        try
        {
            var storeType = LoadedType("BotNexus.Gateway.Abstractions.Sessions.ISessionStore");
            var evidenceType = LoadedType("BotNexus.Gateway.Abstractions.Sessions.IAgentRunEvidenceStore");
            var service = storeType is null ? null : services?.GetService(storeType);
            var query = evidenceType?.GetMethod("QueryAgentRunsAsync", [typeof(SessionId), typeof(int), typeof(long), typeof(CancellationToken)]);
            return service is null || evidenceType?.IsInstanceOfType(service) != true || query is null
                ? null : new ReflectedRunEvidence(service, query);
        }
        catch (Exception) { return null; }
    }
    internal static object? Property(object value, string name) => value.GetType().GetProperty(name)?.GetValue(value);
    internal static double? Number(object value, string name) => Property(value, name) switch
    {
        long n when n >= 0 => n, int n when n >= 0 => n, double n when HubMetricCatalog.Number(n) => n, _ => null
    };
    private sealed class ReflectedActiveLoops(object service, MethodInfo method) : IHubActiveLoops
    {
        public IReadOnlyList<HubActiveLoop> Capture()
        {
            var snapshot = method.Invoke(service, null) ?? throw new InvalidOperationException();
            if (Property(snapshot, "ActiveLoops") is not IEnumerable rows) throw new InvalidOperationException();
            var result = new List<HubActiveLoop>();
            var scanned = 0;
            foreach (var row in rows)
            {
                if (++scanned > 4096) throw new InvalidOperationException(); // Unknown excess must not become a false zero.
                if (row is null) continue;
                if (Property(row, "LoopId") is string id)
                    result.Add(new(id, Property(row, "AgentId") as string, Property(row, "ConversationId") as string));
            }
            return result;
        }
    }
    private sealed class ReflectedRunEvidence(object service, MethodInfo method) : IHubRunEvidence
    {
        public async Task<HubRunPage> QueryAsync(string sessionId, int limit, long afterSequence, CancellationToken ct)
        {
            if (method.Invoke(service, [SessionId.From(sessionId), limit, afterSequence, ct]) is not Task task)
                throw new InvalidOperationException();
            await task.ConfigureAwait(false);
            var page = Property(task, "Result") ?? throw new InvalidOperationException();
            if (Property(page, "Runs") is not IEnumerable rows || Property(page, "HasMore") is not bool hasMore)
                throw new InvalidOperationException();
            var result = new List<HubRunObservation>();
            foreach (var row in rows)
            {
                ct.ThrowIfCancellationRequested();
                if (row is null || result.Count == limit) throw new InvalidOperationException();
                var id = Property(row, "AgentRunId");
                if (id is null || Property(id, "Value") is not string runId
                    || Property(row, "Sequence") is not long sequence || Property(row, "StartedAt") is not DateTimeOffset started
                    || Property(row, "Outcome") is not string outcome) throw new InvalidOperationException();
                // Do not touch guard references, session metadata, page statistics, or legacy coverage.
                result.Add(new(runId, sequence, started, Property(row, "CompletedAt") as DateTimeOffset?, outcome,
                    Property(row, "CompletedResultCount") as int?));
            }
            return new(result, hasMore);
        }
    }
}

internal sealed class HubSqliteMetrics(Func<string, Type?>? resolve = null) : IHubSqliteMetrics
{
    public IReadOnlyList<HubMetricPoint> Capture()
    {
        var points = new List<HubMetricPoint>();
        void Read(string typeName, string methodName, Action<object> project)
        {
            try
            {
                var type = (resolve ?? HubGatewayMetrics.LoadedType)(typeName);
                var method = type?.GetMethod(methodName, BindingFlags.Public | BindingFlags.Static, Type.EmptyTypes);
                if (method?.Invoke(null, null) is { } snapshot) project(snapshot);
            }
            catch (Exception) { } // Provider/type absence affects only these numbers; never logs raw exceptions.
        }
        void Add(object snapshot, string property, string id)
        {
            if (HubGatewayMetrics.Number(snapshot, property) is { } value && HubMetricCatalog.Number(value))
                points.Add(new(id, value));
        }
        Read("BotNexus.Persistence.Sqlite.SqliteConnectionFactory", "GetConnectionObservation", snapshot =>
        {
            Add(snapshot, "CurrentObservedOpenConnections", "sqlite.connections.current");
            Add(snapshot, "PeakObservedOpenConnections", "sqlite.connections.peak");
            Add(snapshot, "OpenTransitions", "sqlite.connections.opened");
            Add(snapshot, "CloseTransitions", "sqlite.connections.closed");
        });
        Read("BotNexus.Persistence.Sqlite.SqliteAllocatorDiagnostics", "Capture", snapshot =>
        {
            if (HubGatewayMetrics.Property(snapshot, "IsAvailable") is not true) return;
            Add(snapshot, "CurrentBytes", "sqlite.memory.current_bytes");
            Add(snapshot, "PeakBytes", "sqlite.memory.peak_bytes");
        });
        return points;
    }
}

internal static class HubLoopMetrics
{
    public static IReadOnlyList<HubMetricPoint> Active(IReadOnlyList<HubActiveLoop> loops, HubMetricsVisibility visibility)
    {
        if (loops.Count > 4096) throw new InvalidOperationException();
        var counts = loops.Where(loop => HubProtocol.IsRoutingId(loop.Id, 256)
                && loop.ConversationId is not null && visibility.Allows(loop.AgentId, loop.ConversationId))
            .DistinctBy(loop => loop.Id, StringComparer.Ordinal).GroupBy(loop => loop.ConversationId!, StringComparer.Ordinal)
            .ToDictionary(group => group.Key, group => group.Count(), StringComparer.Ordinal);
        var result = new List<HubMetricPoint> { new("gateway.loops.active", counts.Values.Sum()) };
        foreach (var row in visibility.Conversations.Values)
            result.Add(new("agent.loops.active", counts.GetValueOrDefault(row.ConversationId), AgentId: row.AgentId, ConversationId: row.ConversationId));
        // Do not emit both agent totals and per-conversation values (that would double count).
        foreach (var agent in visibility.Agents.Where(agent => !visibility.Conversations.Values.Any(row => row.AgentId == agent)))
            result.Add(new("agent.loops.active", 0, AgentId: agent));
        return result;
    }
}
