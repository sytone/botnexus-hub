using System.Net.WebSockets;
using BotNexus.Gateway.Abstractions.Agents;
using BotNexus.Gateway.Abstractions.Configuration;
using BotNexus.Gateway.Abstractions.Conversations;
using BotNexus.Gateway.Abstractions.Services;
using BotNexus.Gateway.Dispatching;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace BotNexus.Extensions.HubConnector;

// The loader discovers this single IHostedService and applies its channel fault barrier.
// Do not also register it with AddHostedService or an IServiceContributor.
public sealed class HubConnectorService : BackgroundService
{
    private readonly IConversationStore _store;
    private readonly IAgentRegistry _agents;
    private readonly IInboundMessageOrchestrator _orchestrator;
    private readonly IWorldContext _world;
    private readonly ILogger<HubConnectorService> _logger;
    private readonly IConfiguration? _configuration;
    private readonly IAskUserPromptResolver? _resolver;
    private readonly IAskUserCheckpointService? _checkpoints;
    private readonly Func<string, string?> _environment;
    private readonly Func<HubConnectorOptions, CancellationToken, Task<WebSocket>> _connect;
    private readonly HttpMessageInvoker _http = new(new SocketsHttpHandler { AllowAutoRedirect = false, UseCookies = false });

    public HubConnectorService(IConversationStore store, IAgentRegistry agents, IInboundMessageOrchestrator orchestrator,
        IWorldContext world, ILogger<HubConnectorService> logger, IConfiguration? configuration = null,
        IAskUserPromptResolver? resolver = null, IAskUserCheckpointService? checkpoints = null)
        : this(store, agents, orchestrator, world, logger, configuration, resolver, checkpoints,
            Environment.GetEnvironmentVariable, null) { }

    internal HubConnectorService(IConversationStore store, IAgentRegistry agents, IInboundMessageOrchestrator orchestrator,
        IWorldContext world, ILogger<HubConnectorService> logger, IConfiguration? configuration,
        IAskUserPromptResolver? resolver, IAskUserCheckpointService? checkpoints,
        Func<string, string?> environment, Func<HubConnectorOptions, CancellationToken, Task<WebSocket>>? connect)
    {
        _store = store; _agents = agents; _orchestrator = orchestrator; _world = world; _logger = logger;
        _configuration = configuration; _resolver = resolver; _checkpoints = checkpoints;
        _environment = environment; _connect = connect ?? ConnectAsync;
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        // No network, store read, secret-file read or configuration work participates in gateway startup.
        await Task.Yield();
        try
        {
            var configuration = HubConnectorConfigurationReader.Load(_environment, configuration: _configuration);
            if (configuration.Options is not { } options)
            {
                if (configuration.Code == "disabled") _logger.LogInformation("Hub connector disabled.");
                else _logger.LogWarning("Hub connector disabled: invalid configuration.");
                return;
            }
            var snapshots = new HubSnapshotBuilder(options, _store, _agents, _world);
            // One processor/cache per service lifetime, not per connection. Never replay a command after reconnect.
            var commands = new HubCommandProcessor(options, _store, _agents, _orchestrator, _world, _resolver, _checkpoints);
            var failures = 0;
            while (!stoppingToken.IsCancellationRequested)
            {
                var startedAt = System.Diagnostics.Stopwatch.GetTimestamp();
                try
                {
                    using var timeout = CancellationTokenSource.CreateLinkedTokenSource(stoppingToken);
                    timeout.CancelAfter(TimeSpan.FromSeconds(10));
                    using var socket = await _connect(options, timeout.Token).ConfigureAwait(false);
                    await RunSessionAsync(socket, snapshots, commands, options, stoppingToken).ConfigureAwait(false);
                }
                catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested) { break; }
                catch (Exception)
                {
                    // Fixed text only: WebSocket/HTTP/store exceptions can include endpoints, tokens or user content.
                    _logger.LogWarning("Hub connector disconnected; retrying.");
                }
                if (System.Diagnostics.Stopwatch.GetElapsedTime(startedAt) >= TimeSpan.FromMinutes(1)) failures = 0;
                else failures = Math.Min(failures + 1, 6);
                await Task.Delay(RetryDelay(failures, Random.Shared.NextDouble()), stoppingToken).ConfigureAwait(false);
            }
        }
        catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested) { }
        catch (Exception)
        {
            _logger.LogWarning("Hub connector disabled after an internal error.");
        }
    }

    private async Task<WebSocket> ConnectAsync(HubConnectorOptions options, CancellationToken ct)
    {
        var socket = new ClientWebSocket();
        try
        {
            socket.Options.SetRequestHeader("Authorization", "Bearer " + options.Token);
            socket.Options.KeepAliveInterval = TimeSpan.FromSeconds(20);
            // Cookies and redirects are disabled on the dedicated handshake handler. Normal TLS validation stays enabled.
            await socket.ConnectAsync(options.ConnectUri, _http, ct).ConfigureAwait(false);
            return socket;
        }
        catch { socket.Dispose(); throw; }
    }

    internal static TimeSpan RetryDelay(int failures, double jitter)
        => TimeSpan.FromMilliseconds(Math.Min(30_000, 1000 * Math.Pow(2, Math.Clamp(failures - 1, 0, 5))
            * (0.8 + 0.4 * Math.Clamp(jitter, 0, 1))));

    private async Task RunSessionAsync(WebSocket socket, HubSnapshotBuilder snapshots, HubCommandProcessor commands,
        HubConnectorOptions options, CancellationToken stoppingToken)
    {
        using var transport = new HubWebSocketTransport(socket);
        using var session = CancellationTokenSource.CreateLinkedTokenSource(stoppingToken);
        await SendSnapshotAsync(transport, snapshots, session.Token).ConfigureAwait(false);
        var snapshotLoop = SendSnapshotsAsync(transport, snapshots, options.SnapshotInterval, session.Token);
        var commandLoop = ReceiveCommandsAsync(transport, commands, session.Token);
        try
        {
            var finished = await Task.WhenAny(snapshotLoop, commandLoop).ConfigureAwait(false);
            await finished.ConfigureAwait(false);
        }
        finally
        {
            session.Cancel();
            socket.Abort();
            // Both tasks are joined before disposing the send lock or socket. No detached receive, send or mutation tasks.
            try { await Task.WhenAll(snapshotLoop, commandLoop).ConfigureAwait(false); }
            catch (Exception) when (session.IsCancellationRequested) { }
        }
    }

    private static async Task SendSnapshotAsync(HubWebSocketTransport transport, HubSnapshotBuilder snapshots, CancellationToken ct)
    {
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
        timeout.CancelAfter(TimeSpan.FromSeconds(15));
        var snapshot = await snapshots.BuildAsync(timeout.Token).ConfigureAwait(false);
        await transport.SendAsync(snapshot, timeout.Token).ConfigureAwait(false);
    }

    private async Task SendSnapshotsAsync(HubWebSocketTransport transport, HubSnapshotBuilder snapshots, TimeSpan interval, CancellationToken ct)
    {
        while (true)
        {
            await Task.Delay(interval, ct).ConfigureAwait(false);
            try { await SendSnapshotAsync(transport, snapshots, ct).ConfigureAwait(false); }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { throw; }
            catch (WebSocketException) { throw; }
            catch (Exception)
            {
                // Do not turn a failed store read into an empty snapshot that clears valid Hub data.
                _logger.LogWarning("Hub snapshot unavailable; no successful snapshot reported.");
            }
        }
    }

    private static async Task ReceiveCommandsAsync(HubWebSocketTransport transport, HubCommandProcessor commands, CancellationToken ct)
    {
        while (await transport.ReceiveAsync(ct).ConfigureAwait(false) is { } bytes)
        {
            var parsed = HubProtocol.ParseCommand(bytes);
            var result = parsed.Command is { } command
                ? await commands.ProcessAsync(command, ct).ConfigureAwait(false) : parsed.Error!;
            await transport.SendAsync(result, ct).ConfigureAwait(false);
        }
    }

    public override void Dispose()
    {
        base.Dispose();
        _http.Dispose();
    }
}
