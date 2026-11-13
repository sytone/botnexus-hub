using System.Net.WebSockets;

namespace BotNexus.Extensions.HubConnector;

internal sealed class HubWebSocketTransport(WebSocket socket) : IDisposable
{
    private readonly SemaphoreSlim _sendGate = new(1, 1);

    public async Task<byte[]?> ReceiveAsync(CancellationToken ct = default)
    {
        ct.ThrowIfCancellationRequested();
        var buffer = new byte[16_384];
        using var message = new MemoryStream();
        while (true)
        {
            var frame = await socket.ReceiveAsync(new ArraySegment<byte>(buffer), ct).ConfigureAwait(false);
            if (frame.MessageType == WebSocketMessageType.Close) return null;
            if (frame.MessageType != WebSocketMessageType.Text) throw new HubProtocolException("invalid_frame");
            if (message.Length + frame.Count > HubProtocol.MaxFrameBytes) throw new HubProtocolException("frame_too_large");
            message.Write(buffer, 0, frame.Count);
            if (frame.EndOfMessage) return message.ToArray();
        }
    }

    public Task SendAsync<T>(T frame, CancellationToken ct = default)
        => SendBytesAsync(frame is HubSnapshot snapshot ? HubProtocol.EncodeSnapshot(snapshot) : HubProtocol.Serialize(frame), ct);

    private async Task SendBytesAsync(byte[] bytes, CancellationToken ct)
    {
        if (bytes.Length > HubProtocol.MaxFrameBytes) throw new HubProtocolException("frame_too_large");
        await _sendGate.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
            timeout.CancelAfter(TimeSpan.FromSeconds(15));
            await socket.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Text, true, timeout.Token).ConfigureAwait(false);
        }
        finally { _sendGate.Release(); }
    }

    public void Dispose()
    {
        _sendGate.Dispose();
        socket.Dispose();
    }
}
