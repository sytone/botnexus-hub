using System.Globalization;
using System.Net;
using System.Text;
using Microsoft.Extensions.Configuration;

namespace BotNexus.Extensions.HubConnector;

internal sealed class HubConnectorOptions
{
    public required string HubId { get; init; }
    public required Uri ConnectUri { get; init; }
    public required string Name { get; init; }
    public required string BaseUrl { get; init; }
    public required string Token { get; init; }
    public required IReadOnlySet<string> AllowedAgents { get; init; }
    public TimeSpan SnapshotInterval { get; init; } = TimeSpan.FromSeconds(15);
    public TimeSpan ActivityLookback { get; init; } = TimeSpan.FromDays(7);
    public int MaxConversations { get; init; } = 100;
    public int MaxQuestions { get; init; } = 100;
    public bool IncludeWorldIdentity { get; init; }
    public override string ToString() => "HubConnectorOptions [redacted]";
}

internal sealed record HubConnectorConfiguration(HubConnectorOptions? Options, string Code);

internal static class HubConnectorConfigurationReader
{
    internal const string ExtensionId = "botnexus-hub-connector";
    private const int MaxSettingLength = 16_384;

    // No configuration or file error escapes to gateway startup, and no diagnostic contains a value.
    public static HubConnectorConfiguration Load(Func<string, string?> environment,
        Func<string, string>? readFile = null, IConfiguration? configuration = null)
    {
        try
        {
            var section = configuration?.GetSection("gateway:extensions:" + ExtensionId);
            string? Read(string key, string property)
            {
                foreach (var prefix in new[] { "BOTNEXUSHUB_", "HUB_" })
                {
                    var value = environment(prefix + key);
                    var file = environment(prefix + key + "_FILE");
                    if (value is not null || file is not null)
                        return Resolve(value, file, readFile);
                }
                var configured = section?[property];
                if (property == "allowedAgents" && configured is null && section is not null)
                {
                    var children = section.GetSection(property).GetChildren().ToArray();
                    if (children.Length > 0) configured = string.Join(',', children.Select(c => c.Value));
                }
                return Resolve(configured, section?[property + "File"], readFile);
            }
            bool Flag(string key, string property, bool fallback)
            {
                var value = Read(key, property);
                return value is null ? fallback : bool.TryParse(value, out var flag) ? flag : throw new FormatException();
            }
            int Number(string key, string property, int fallback, int min, int max)
            {
                var value = Read(key, property);
                var number = value is null ? fallback : int.Parse(value, NumberStyles.None, CultureInfo.InvariantCulture);
                return number >= min && number <= max ? number : throw new FormatException();
            }
            if (!Flag("ENABLED", "enabled", true)) return new(null, "disabled");

            var hubId = Read("WORLD_ID", "worldId");
            var name = Read("WORLD_NAME", "worldName");
            var token = Read("WORLD_TOKEN", "worldToken");
            var allowed = Read("ALLOWED_AGENTS", "allowedAgents");
            if (!HubProtocol.IsRoutingId(hubId, 64) || !char.IsAsciiLetterOrDigit(hubId![0]) || hubId.Any(c => !char.IsAsciiLetterOrDigit(c) && c is not '-' and not '_' and not '.')
                || string.IsNullOrWhiteSpace(name) || name.Length > 160 || name.Any(char.IsControl)
                || string.IsNullOrEmpty(token) || token.Length > 4096 || token.Any(c => c < 0x21 || c > 0x7e)
                || string.IsNullOrWhiteSpace(allowed))
                return new(null, "invalid_configuration");

            var agents = allowed.Split(',', StringSplitOptions.TrimEntries);
            if (agents.Length > 100 || agents.Any(id => !HubProtocol.IsRoutingId(id, 128) || id.IndexOfAny(['*', '?', '[', ']']) >= 0))
                return new(null, "invalid_configuration");

            var allowInsecure = Flag("ALLOW_INSECURE", "allowInsecure", false);
            var hub = ParseUrl(Read("URL", "url"), websocket: true);
            var baseUrl = ParseUrl(Read("WORLD_BASE_URL", "worldBaseUrl"), websocket: false);
            var scheme = hub.Scheme switch { "https" => "wss", "http" => "ws", _ => hub.Scheme };
            if (scheme != "wss" && !IsLoopback(hub) && !allowInsecure)
                return new(null, "invalid_configuration");
            var connect = new UriBuilder(hub)
            {
                Scheme = scheme,
                Path = hub.AbsolutePath.TrimEnd('/') + "/api/connect/" + Uri.EscapeDataString(hubId!),
                Query = "", Fragment = ""
            }.Uri;
            return new(new HubConnectorOptions
            {
                HubId = hubId!, Name = name, BaseUrl = baseUrl.AbsoluteUri.TrimEnd('/'), Token = token,
                ConnectUri = connect, AllowedAgents = new HashSet<string>(agents, StringComparer.Ordinal),
                SnapshotInterval = TimeSpan.FromSeconds(Number("SNAPSHOT_INTERVAL_SECONDS", "snapshotIntervalSeconds", 15, 1, 3600)),
                ActivityLookback = TimeSpan.FromDays(Number("ACTIVITY_LOOKBACK_DAYS", "activityLookbackDays", 7, 1, 365)),
                MaxConversations = Number("MAX_CONVERSATIONS", "maxConversations", 100, 1, 100),
                MaxQuestions = Number("MAX_QUESTIONS", "maxQuestions", 100, 1, 100),
                IncludeWorldIdentity = Flag("INCLUDE_WORLD_IDENTITY", "includeWorldIdentity", false)
            }, "enabled");
        }
        catch (Exception)
        {
            return new(null, "invalid_configuration");
        }
    }

    private static string? Resolve(string? value, string? file, Func<string, string>? readFile)
    {
        if (value is not null && file is not null) throw new FormatException();
        if (file is not null)
        {
            if (string.IsNullOrWhiteSpace(file) || file.Length > 4096 || file.Any(char.IsControl)) throw new FormatException();
            value = readFile is not null ? readFile(file) : ReadBoundedFile(file);
        }
        if (value is null) return null;
        if (value.Length > MaxSettingLength) throw new FormatException();
        return value.Trim();
    }

    private static string ReadBoundedFile(string path)
    {
        // Bounded even when a file changes after opening. Only local operator configuration selects this path.
        using var reader = new StreamReader(path, Encoding.UTF8, detectEncodingFromByteOrderMarks: true);
        var characters = new char[MaxSettingLength + 1];
        var count = reader.ReadBlock(characters, 0, characters.Length);
        if (count > MaxSettingLength) throw new FormatException();
        return new string(characters, 0, count);
    }

    private static Uri ParseUrl(string? value, bool websocket)
    {
        if (value is null || value.Length > 2048 || value.Any(char.IsControl)
            || !Uri.TryCreate(value, UriKind.Absolute, out var uri)
            || string.IsNullOrWhiteSpace(uri.Host) || !string.IsNullOrEmpty(uri.UserInfo)
            || !string.IsNullOrEmpty(uri.Query) || !string.IsNullOrEmpty(uri.Fragment)
            || (uri.Scheme != "http" && uri.Scheme != "https" && (!websocket || (uri.Scheme != "ws" && uri.Scheme != "wss"))))
            throw new FormatException();
        return uri;
    }

    private static bool IsLoopback(Uri uri)
        => string.Equals(uri.DnsSafeHost, "localhost", StringComparison.OrdinalIgnoreCase)
           || (IPAddress.TryParse(uri.DnsSafeHost.Trim('[', ']'), out var address) && IPAddress.IsLoopback(address));
}
