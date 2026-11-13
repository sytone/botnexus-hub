using BotNexus.Domain.Primitives;
using BotNexus.Gateway.Abstractions.Channels;
using BotNexus.Gateway.Abstractions.Models;

namespace BotNexus.Extensions.HubConnector;

/// <summary>Metadata-only channel. Full replies stay in the world; snapshots carry durable prompt metadata.</summary>
public sealed class HubChannelAdapter : IChannelAdapter
{
    public ChannelKey ChannelType => ChannelKey.From("hub");
    public string DisplayName => "BotNexusHub";
    public bool SupportsStreaming => false;
    public bool SupportsSteering => false;
    public bool SupportsFollowUp => true;
    public bool SupportsThinkingDisplay => false;
    public bool SupportsToolDisplay => false;
    public bool SupportsInboundImages => false;
    public bool SupportsInteractivePrompts => true;
    public bool IsRunning { get; private set; }
    public Task StartAsync(IChannelDispatcher dispatcher, CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        IsRunning = true;
        return Task.CompletedTask;
    }
    public Task StopAsync(CancellationToken cancellationToken = default)
    {
        IsRunning = false;
        return Task.CompletedTask;
    }
    public Task SendAsync(OutboundMessage message, CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        // Intentionally do not forward response/tool/transcript bytes. The Hub is a summary surface.
        return Task.CompletedTask;
    }
    public Task SendStreamDeltaAsync(ChannelStreamTarget target, string delta, CancellationToken cancellationToken = default)
        => throw new NotSupportedException("Hub does not export response streams.");
}
