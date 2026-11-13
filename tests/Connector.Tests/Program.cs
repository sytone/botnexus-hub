using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using BotNexus.Domain.Primitives;
using BotNexus.Domain.World;
using BotNexus.Extensions.HubConnector;
using BotNexus.Gateway.Abstractions.Conversations;
using BotNexus.Gateway.Abstractions.Models;
using BotNexus.Gateway.Abstractions.Services;
using BotNexus.Gateway.Dispatching;
using Connector.Tests;
using Microsoft.Extensions.Configuration;

var tests = new (string Name, Func<Task> Run)[]
{
    ("config requires enrollment and explicit agents", ConfigRequired),
    ("config validates TLS and credential-free URLs", ConfigTls),
    ("config supports secret files without exposing secrets", ConfigFiles),
    ("config precedence and bounded settings", ConfigPrecedence),
    ("protocol only accepts three exact actions", ProtocolActions),
    ("protocol rejects malformed envelopes and extra capabilities", ProtocolMalformed),
    ("protocol validates fields and size", ProtocolLimits),
    ("snapshots filter agents, owners, lifecycle and nesting", SnapshotFilters),
    ("snapshots use safe metadata DTOs and configured hub id", SnapshotProjection),
    ("old pending questions are queried independently of activity", SnapshotOldQuestions),
    ("snapshots cap visible recent conversations and questions", SnapshotCaps),
    ("malformed and misrouted checkpoints fail closed", SnapshotBadCheckpoints),
    ("snapshot errors do not become empty successful sync", SnapshotFailure),
    ("outgoing snapshots fit a complete 1MiB frame", SnapshotBytes),
    ("start creates typed human conversation before queue admission", StartConversation),
    ("send rejects wrong owner and non-writable conversations", SendOwner),
    ("disabled, nested and disallowed agents cannot mutate", AgentPolicy),
    ("unknown actions have no store or queue effects", UnknownAction),
    ("answers require exact current request id", AnswerRequest),
    ("answers use shared resolver and hub origin", AnswerLive),
    ("answers use persisted checkpoint fallback", AnswerDurable),
    ("invalid answers are never retried through checkpoint service", AnswerInvalid),
    ("busy and unknown outcomes never report success or replay", AdmissionOutcomes),
    ("command ids deduplicate concurrently and reject changed bodies", Dedupe),
    ("bounded dedupe fails closed rather than evicting old commands", DedupeBound),
    ("existing deterministic conversations are not seeded twice", ExistingConversation),
    ("fragmented text frames are reassembled", FragmentedFrame),
    ("oversize fragmented frames are rejected", OversizeFrame),
    ("binary frames are rejected and close is handled", BinaryFrame),
    ("concurrent sends never interleave frames", SendLock),
    ("receive and send honor cancellation", TransportCancellation),
};
var failures = 0;
foreach (var test in tests)
{
    try { await test.Run(); Console.WriteLine("PASS " + test.Name); }
    catch (Exception ex) { failures++; Console.WriteLine("FAIL " + test.Name + ": " + ex.GetType().Name + " " + ex.Message); }
}
Console.WriteLine($"RESULT {tests.Length - failures}/{tests.Length} passed; {failures} failed");
return failures == 0 ? 0 : 1;

static void Check(bool condition, string message = "assertion failed") { if (!condition) throw new Exception(message); }
static Task Sync(Action action) { action(); return Task.CompletedTask; }
static byte[] Utf8(string value) => Encoding.UTF8.GetBytes(value);
static string Id(int n = 1) => $"00000000-0000-4000-8000-{n:D12}";
static Dictionary<string, string?> EnvironmentValues() => new()
{
    ["HUB_URL"] = "https://hub.example", ["HUB_WORLD_ID"] = "enrolled-a", ["HUB_WORLD_NAME"] = "Test A",
    ["HUB_WORLD_BASE_URL"] = "https://world.example", ["HUB_WORLD_TOKEN"] = "synthetic-token",
    ["HUB_ALLOWED_AGENTS"] = "helper"
};
static HubConnectorConfiguration ReadConfig(Dictionary<string, string?> values, Func<string, string>? files = null,
    IConfiguration? configuration = null) => HubConnectorConfigurationReader.Load(k => values.GetValueOrDefault(k), files, configuration);
static HubConnectorOptions Options() => ReadConfig(EnvironmentValues()).Options ?? throw new Exception("valid config rejected");
static AgentDescriptor Agent(string id = "helper") => new()
{
    AgentId = AgentId.From(id), DisplayName = "Helper", Description = "Safe description", ModelId = "synthetic",
    ApiProvider = "synthetic", SystemPrompt = "MUST_NOT_EXPORT_SYSTEM", ToolIds = ["MUST_NOT_EXPORT_TOOL"]
};
static Conversation ConversationRow(string id = "c_one", string agent = "helper")
    => ConversationFactory.CreateForChannel(ConversationId.From(id), AgentId.From(agent), "Safe title",
        CitizenId.Of(UserId.From("synthetic-user")));
static ConversationSummary Summary(Conversation row) => new(row.ConversationId.Value, row.AgentId.Value,
    row.Title, false, row.Status.ToString(), null, 0, row.CreatedAt, row.UpdatedAt,
    "MUST_NOT_EXPORT_PURPOSE", row.Kind.ToString(), row.Source.ToString(), row.Visibility.ToString());
static string Checkpoint(Conversation row, string request = "req-1") => JsonSerializer.Serialize(new
{
    requestId = request, conversationId = row.ConversationId.Value, agentId = row.AgentId.Value,
    sessionId = "MUST_NOT_EXPORT_SESSION", prompt = "Which option?", inputType = "SingleChoice",
    choices = new[] { new { value = "yes", label = "Yes", description = "Continue" } },
    allowMultiple = false, allowFreeForm = true, toolPayload = "MUST_NOT_EXPORT_PAYLOAD"
});
static HubCommand Start(int n = 1) => new(Id(n), "start_conversation", new("helper", Title: "From Hub", Message: "Hello"));
static HubCommand Send(int n = 1, string agent = "helper") => new(Id(n), "send_message", new(agent, "c_one", Message: "Hello"));
static HubCommand Answer(int n = 1, string request = "req-1") => new(Id(n), "answer_question",
    new("helper", "c_one", RequestId: request, Text: "  Answer  ", SelectedValues: [" yes "]));
static (HubCommandProcessor Processor, StoreProxy Store, FakeOrchestrator Queue) Processor(
    Conversation? row = null, FakeAgents? agents = null, FakeResolver? resolver = null,
    FakeCheckpoints? checkpoints = null, int capacity = 4096)
{
    var (store, fake) = StoreProxy.New();
    if (row is not null) fake.Rows[row.ConversationId.Value] = row;
    var queue = new FakeOrchestrator();
    return (new(Options(), store, agents ?? new FakeAgents(Agent()), queue, new FakeWorld(), resolver, checkpoints, capacity), fake, queue);
}
static async Task ExpectProtocol(Func<Task> action, string code)
{
    try { await action(); throw new Exception("expected " + code); }
    catch (HubProtocolException ex) { Check(ex.Code == code, ex.Code); }
}
static Task ConfigRequired() => Sync(() =>
{
    foreach (var key in new[] { "HUB_URL", "HUB_WORLD_ID", "HUB_WORLD_NAME", "HUB_WORLD_BASE_URL", "HUB_WORLD_TOKEN", "HUB_ALLOWED_AGENTS" })
    {
        var env = EnvironmentValues(); env.Remove(key);
        var result = ReadConfig(env); Check(result.Options is null && result.Code == "invalid_configuration", key);
    }
    var options = Options(); Check(options.HubId == "enrolled-a");
    Check(options.ConnectUri.ToString() == "wss://hub.example/api/connect/enrolled-a");
    Check(options.SnapshotInterval == TimeSpan.FromSeconds(15) && options.ActivityLookback == TimeSpan.FromDays(7));
});
static Task ConfigTls() => Sync(() =>
{
    foreach (var url in new[] { "http://remote.example", "ws://remote.example", "https://u:password@hub.example", "https://hub.example?q=secret", "https://hub.example/#x", "file:///tmp/x" })
    {
        var env = EnvironmentValues(); env["HUB_URL"] = url; Check(ReadConfig(env).Options is null, url);
    }
    foreach (var url in new[] { "http://localhost:8785", "ws://127.0.0.1:8785", "ws://[::1]:8785", "wss://hub.example" })
    {
        var env = EnvironmentValues(); env["HUB_URL"] = url; Check(ReadConfig(env).Options is not null, url);
    }
    var dev = EnvironmentValues(); dev["HUB_URL"] = "http://hub-container:8785"; dev["HUB_ALLOW_INSECURE"] = "true";
    Check(ReadConfig(dev).Options is not null);
    foreach (var url in new[] { "javascript:alert(1)", "https://u:p@world.example", "https://world.example?q=secret", "https://world.example/#fragment" })
    {
        dev = EnvironmentValues(); dev["HUB_WORLD_BASE_URL"] = url; Check(ReadConfig(dev).Options is null, url);
    }
});
static Task ConfigFiles() => Sync(() =>
{
    var env = EnvironmentValues(); env.Remove("HUB_WORLD_TOKEN"); env["HUB_WORLD_TOKEN_FILE"] = "/synthetic/token";
    Check(ReadConfig(env, path => path == "/synthetic/token" ? "synthetic-file-token\n" : throw new IOException()).Options?.Token == "synthetic-file-token");
    Check(ReadConfig(env, _ => throw new IOException("MUST_NOT_EXPORT_SECRET")).Code == "invalid_configuration");
    env["HUB_WORLD_TOKEN"] = "competing-token"; Check(ReadConfig(env, _ => "token").Options is null);
    env = EnvironmentValues(); env["HUB_WORLD_TOKEN"] = "token\r\nInjected: yes"; Check(ReadConfig(env).Options is null);
    Check(!Options().ToString().Contains("synthetic-token", StringComparison.Ordinal));
});
static Task ConfigPrecedence() => Sync(() =>
{
    var env = EnvironmentValues(); env["BOTNEXUSHUB_WORLD_ID"] = "preferred-id";
    var config = new ConfigurationBuilder().AddInMemoryCollection(new Dictionary<string, string?>
    {
        ["gateway:extensions:botnexus-hub-connector:worldId"] = "scope-id",
        ["gateway:extensions:botnexus-hub-connector:enabled"] = "true"
    }).Build();
    Check(ReadConfig(env, configuration: config).Options?.HubId == "preferred-id");
    env.Remove("BOTNEXUSHUB_WORLD_ID"); Check(ReadConfig(env, configuration: config).Options?.HubId == "enrolled-a");
    env.Remove("HUB_WORLD_ID"); Check(ReadConfig(env, configuration: config).Options?.HubId == "scope-id");
    env["HUB_SNAPSHOT_INTERVAL_SECONDS"] = "0"; Check(ReadConfig(env).Options is null);
    env["HUB_SNAPSHOT_INTERVAL_SECONDS"] = "1"; env["HUB_MAX_CONVERSATIONS"] = "101"; Check(ReadConfig(env).Options is null);
    env["HUB_MAX_CONVERSATIONS"] = "100"; env["HUB_ALLOWED_AGENTS"] = "helper, second,helper";
    Check(ReadConfig(env, configuration: config).Options?.AllowedAgents.Count == 2);
    env["HUB_ALLOWED_AGENTS"] = "*"; Check(ReadConfig(env, configuration: config).Options is null);
    env["HUB_ALLOWED_AGENTS"] = "helper"; env["HUB_ENABLED"] = "false"; Check(ReadConfig(env).Code == "disabled");
});
static Task ProtocolActions() => Sync(() =>
{
    foreach (var action in new[] { "start_conversation", "send_message", "answer_question" })
    {
        var command = action == "start_conversation" ? Start() : action == "send_message" ? Send() : Answer();
        var bytes = Utf8(JsonSerializer.Serialize(new { type = "command", id = command.Id, action, payload = new
        { agentId = "helper", conversationId = action == "start_conversation" ? null : "c_one", title = "Title", message = "Hello", requestId = "req-1", text = "answer", selectedValues = Array.Empty<string>(), cancelled = false } }));
        // Exact per-action payloads below avoid accidental extra capabilities.
        bytes = action switch
        {
            "start_conversation" => Utf8($"{{\"type\":\"command\",\"id\":\"{Id()}\",\"action\":\"{action}\",\"payload\":{{\"agentId\":\"helper\",\"title\":\"Title\",\"message\":\"Hello\"}}}}"),
            "send_message" => Utf8($"{{\"type\":\"command\",\"id\":\"{Id()}\",\"action\":\"{action}\",\"payload\":{{\"agentId\":\"helper\",\"conversationId\":\"c_one\",\"message\":\"Hello\"}}}}"),
            _ => Utf8($"{{\"type\":\"command\",\"id\":\"{Id()}\",\"action\":\"{action}\",\"payload\":{{\"agentId\":\"helper\",\"conversationId\":\"c_one\",\"requestId\":\"req-1\",\"text\":\"Answer\",\"selectedValues\":[],\"cancelled\":false}}}}")
        };
        Check(HubProtocol.ParseCommand(bytes).Command?.Action == action, action);
    }
    var unknown = HubProtocol.ParseCommand(Utf8($"{{\"type\":\"command\",\"id\":\"{Id()}\",\"action\":\"shell\",\"payload\":{{}}}}"));
    Check(unknown.Error?.Error == "unknown_action");
});
static Task ProtocolMalformed() => Sync(() =>
{
    foreach (var json in new[] { "null", "[]", "{", "{}", "{\"type\":\"snapshot\"}",
        $"{{\"type\":\"command\",\"id\":\"{Id()}\",\"id\":\"{Id(2)}\",\"action\":\"send_message\",\"payload\":{{}}}}",
        $"{{\"type\":\"command\",\"id\":\"{Id()}\",\"action\":\"send_message\",\"payload\":{{\"agentId\":\"helper\",\"conversationId\":\"c_one\",\"message\":\"Hello\",\"url\":\"https://evil.example\"}}}}",
        $"{{\"type\":\"command\",\"protocolVersion\":2,\"id\":\"{Id()}\",\"action\":\"send_message\",\"payload\":{{}}}}" })
        Check(HubProtocol.ParseCommand(Utf8(json)).Command is null, json);
});
static Task ProtocolLimits() => Sync(() =>
{
    Check(HubProtocol.ParseCommand(new byte[HubProtocol.MaxFrameBytes + 1]).Error?.Error == "frame_too_large");
    var large = new string('x', 32_769);
    var json = JsonSerializer.Serialize(new { type = "command", id = Id(), action = "send_message", payload = new { agentId = "helper", conversationId = "c_one", message = large } });
    Check(HubProtocol.ParseCommand(Utf8(json)).Error?.Error == "invalid_command");
    json = JsonSerializer.Serialize(new { type = "command", id = "not-a-uuid", action = "send_message", payload = new { agentId = "helper", conversationId = "c_one", message = "hello" } });
    Check(HubProtocol.ParseCommand(Utf8(json)).Command is null);
});
static async Task SnapshotFilters()
{
    var (store, fake) = StoreProxy.New();
    var good = ConversationRow();
    var rows = new[] { good, ConversationRow("c_archived") with { Status = ConversationStatus.Archived },
        ConversationRow("c_hidden") with { Visibility = ConversationVisibility.InternalHidden },
        ConversationRow("c_readonly") with { Visibility = ConversationVisibility.InspectableReadOnly },
        ConversationRow("c_nested") with { ParentConversationId = good.ConversationId },
        ConversationRow("c_wrong", "other"), ConversationRow("c_old") with { UpdatedAt = DateTimeOffset.UtcNow.AddDays(-8) } };
    foreach (var row in rows) { fake.Rows[row.ConversationId.Value] = row; fake.Summaries.Add(Summary(row)); }
    var agents = new FakeAgents(Agent(), Agent("disabled") with { Enabled = false }, Agent("nested") with { Kind = AgentKind.SubAgent }, Agent("other"));
    var snapshot = await new HubSnapshotBuilder(Options(), store, agents, new FakeWorld()).BuildAsync();
    Check(snapshot.Agents.Count == 1 && snapshot.Agents[0].Id == "helper");
    Check(snapshot.Conversations.Count == 1 && snapshot.Conversations[0].Id == "c_one");
    Check(!fake.Calls.Contains("ListAsync"));
}
static async Task SnapshotProjection()
{
    var (store, fake) = StoreProxy.New(); var row = ConversationRow();
    row.Instructions = "MUST_NOT_EXPORT_INSTRUCTIONS"; row.CanvasHtml = "MUST_NOT_EXPORT_CANVAS";
    fake.Rows[row.ConversationId.Value] = row; fake.Summaries.Add(Summary(row));
    fake.Pending.Add(new(row.ConversationId, Checkpoint(row)));
    var snapshot = await new HubSnapshotBuilder(Options(), store, new FakeAgents(Agent()), new FakeWorld()).BuildAsync();
    var json = Encoding.UTF8.GetString(HubProtocol.EncodeSnapshot(snapshot));
    Check(!json.Contains("MUST_NOT_EXPORT", StringComparison.Ordinal)); Check(!json.Contains("synthetic-token", StringComparison.Ordinal));
    Check(snapshot.WorldId == "enrolled-a" && snapshot.WorldIdentity is null && snapshot.Questions.Count == 1);
    Check(snapshot.Questions[0].InputType == "single_choice");
    Check(JsonDocument.Parse(json).RootElement.GetProperty("protocolVersion").GetInt32() == 1);
}
static async Task SnapshotOldQuestions()
{
    var (store, fake) = StoreProxy.New(); var row = ConversationRow("c_older") with { UpdatedAt = DateTimeOffset.UtcNow.AddDays(-30) };
    fake.Rows[row.ConversationId.Value] = row; fake.Pending.Add(new(row.ConversationId, Checkpoint(row)));
    var snapshot = await new HubSnapshotBuilder(Options(), store, new FakeAgents(Agent()), new FakeWorld()).BuildAsync();
    Check(snapshot.Conversations.Count == 0 && snapshot.Questions.Single().ConversationId == "c_older");
    Check(fake.Calls.Count(x => x == "GetPendingAskUserCheckpointsAsync") == 1);
}
static async Task SnapshotCaps()
{
    var (store, fake) = StoreProxy.New();
    for (var n = 0; n < 135; n++)
    {
        var row = ConversationRow("c_" + n) with { UpdatedAt = DateTimeOffset.UtcNow.AddMinutes(-n) };
        fake.Rows[row.ConversationId.Value] = row; fake.Summaries.Add(Summary(row)); fake.Pending.Add(new(row.ConversationId, Checkpoint(row)));
    }
    var snapshot = await new HubSnapshotBuilder(Options(), store, new FakeAgents(Agent()), new FakeWorld()).BuildAsync();
    Check(snapshot.Conversations.Count == 100 && snapshot.Questions.Count == 100);
    Check(snapshot.Conversations[0].Id == "c_0");
}
static async Task SnapshotBadCheckpoints()
{
    var (store, fake) = StoreProxy.New();
    foreach (var n in Enumerable.Range(1, 4)) fake.Rows["c_" + n] = ConversationRow("c_" + n);
    fake.Pending.Add(new(ConversationId.From("c_1"), "not json"));
    fake.Pending.Add(new(ConversationId.From("c_2"), Checkpoint(ConversationRow("c_other"))));
    fake.Pending.Add(new(ConversationId.From("c_3"), Checkpoint(ConversationRow("c_3", "other"))));
    fake.Pending.Add(new(ConversationId.From("c_4"), new string('x', HubProtocol.MaxFrameBytes + 1)));
    var snapshot = await new HubSnapshotBuilder(Options(), store, new FakeAgents(Agent()), new FakeWorld()).BuildAsync();
    Check(snapshot.Questions.Count == 0);
}
static async Task SnapshotFailure()
{
    var (store, fake) = StoreProxy.New(); fake.Failure = new IOException("synthetic store unavailable");
    try { await new HubSnapshotBuilder(Options(), store, new FakeAgents(Agent()), new FakeWorld()).BuildAsync(); throw new Exception("failure hidden"); }
    catch (IOException) { }
}
static Task SnapshotBytes() => Sync(() =>
{
    var choices = Enumerable.Range(0, 20).Select(n => new HubChoice("v" + n, new string('\u4e00', 160), new string('\u4e00', 256))).ToArray();
    var questions = Enumerable.Range(0, 100).Select(n => new HubQuestion("c_" + n, "helper", "Title", "req-" + n,
        new string('\u4e00', 4000), "single_choice", choices, false, true)).ToArray();
    var bytes = HubProtocol.EncodeSnapshot(new("enrolled-a", "Test", "https://world.example", "1", DateTimeOffset.UtcNow, [new HubAgent("helper", "Helper", null)], [], questions));
    Check(bytes.Length <= HubProtocol.MaxFrameBytes);
    var root = JsonDocument.Parse(bytes).RootElement;
    Check(root.GetProperty("truncated").GetBoolean());
    var agents = root.GetProperty("agents").EnumerateArray().Select(agent => agent.GetProperty("id").GetString()).ToHashSet();
    Check(root.GetProperty("questions").EnumerateArray().Any());
    Check(root.GetProperty("questions").EnumerateArray().All(question => agents.Contains(question.GetProperty("agentId").GetString())));
    var orphanBytes = HubProtocol.EncodeSnapshot(new("enrolled-a", "Test", "https://world.example", "1", DateTimeOffset.UtcNow, [], [], questions));
    Check(JsonDocument.Parse(orphanBytes).RootElement.GetProperty("questions").GetArrayLength() == 0);
});
static async Task StartConversation()
{
    var (processor, store, queue) = Processor(); var result = await processor.ProcessAsync(Start());
    Check(result.Ok && result.Data?.Status == "accepted" && store.Creates == 1 && queue.Messages.Count == 1);
    var row = store.Rows.Values.Single(); var message = queue.Messages.Single();
    Check(row.ConversationId.Value == result.Data?.ConversationId && row.ConversationId.Value.StartsWith("c_", StringComparison.Ordinal));
    Check(row.Source == ConversationSource.Channel && row.Kind == ConversationKind.HumanAgent && row.Visibility == ConversationVisibility.UserFacing);
    Check(row.Initiator == CitizenId.Of(UserId.From("hub-operator")) && row.WorldId == "actual-local-world");
    Check(message.ChannelType.Value == "hub" && message.Sender == CitizenId.Of(UserId.From("hub-operator")));
    Check(message.RoutingHints?.RequestedConversationId == row.ConversationId && message.RoutingHints.RequestedAgentId == row.AgentId);
    Check(message.Content == "Hello" && message.ContentParts is null && message.Metadata.Count == 0);
    Check(store.Calls.IndexOf("CreateAsync") < store.Calls.Count);
}
static async Task SendOwner()
{
    foreach (var row in new[] { ConversationRow(agent: "other"), ConversationRow() with { Status = ConversationStatus.Archived },
        ConversationRow() with { Visibility = ConversationVisibility.InternalHidden },
        ConversationRow() with { Visibility = ConversationVisibility.InspectableReadOnly },
        ConversationRow() with { ParentConversationId = ConversationId.From("c_parent") } })
    {
        var (processor, _, queue) = Processor(row); var result = await processor.ProcessAsync(Send());
        Check(!result.Ok && queue.Messages.Count == 0, "owner or visibility bypass");
    }
    var valid = Processor(ConversationRow()); Check((await valid.Processor.ProcessAsync(Send())).Ok && valid.Queue.Messages.Count == 1);
}
static async Task AgentPolicy()
{
    foreach (var agent in new[] { Agent() with { Enabled = false }, Agent() with { Kind = AgentKind.SubAgent },
        Agent() with { Metadata = new Dictionary<string, object?> { ["hidden"] = true } } })
    {
        var (processor, store, queue) = Processor(agents: new FakeAgents(agent));
        Check(!(await processor.ProcessAsync(Start())).Ok && store.Creates == 0 && queue.Messages.Count == 0);
    }
    var denied = Processor(); Check(!(await denied.Processor.ProcessAsync(Start() with { Payload = new("not-allowed", Message: "Hello") })).Ok);
}
static async Task UnknownAction()
{
    var (processor, store, queue) = Processor();
    var result = await processor.ProcessAsync(Start() with { Action = "shell" });
    Check(result.Error == "unknown_action" && store.Calls.Count == 0 && queue.Messages.Count == 0);
}
static async Task AnswerRequest()
{
    var row = ConversationRow(); row.PendingAskUserJson = Checkpoint(row);
    var resolver = new FakeResolver(); var checkpoints = new FakeCheckpoints();
    var (processor, _, queue) = Processor(row, resolver: resolver, checkpoints: checkpoints);
    var result = await processor.ProcessAsync(Answer(request: "stale"));
    Check(result.Error == "request_mismatch" && resolver.Submissions.Count == 0 && checkpoints.Responses.Count == 0 && queue.Messages.Count == 0);
}
static async Task AnswerLive()
{
    var resolver = new FakeResolver(); var (processor, _, queue) = Processor(ConversationRow(), resolver: resolver);
    var result = await processor.ProcessAsync(Answer());
    Check(result.Ok && resolver.Submissions.Single().OriginChannel?.Value == "hub" && queue.Messages.Count == 0);
    Check(resolver.Submissions[0].RequestId == "req-1");
}
static async Task AnswerDurable()
{
    var row = ConversationRow(); row.PendingAskUserJson = Checkpoint(row);
    var resolver = new FakeResolver { PendingId = null, Result = AskUserResolutionResult.NoPendingPrompt("MUST_NOT_EXPORT_REASON") };
    var checkpoints = new FakeCheckpoints(); var (processor, _, _) = Processor(row, resolver: resolver, checkpoints: checkpoints);
    Check((await processor.ProcessAsync(Answer())).Ok && checkpoints.Responses.Count == 1);
    Check(checkpoints.Responses[0].FreeFormText == "Answer" && checkpoints.Responses[0].SelectedValues?.Single() == "yes");
    checkpoints.Result = AskUserResolveOutcome.RequestIdMismatch;
    Check((await processor.ProcessAsync(Answer(2))).Error == "request_mismatch");
}
static async Task AnswerInvalid()
{
    var row = ConversationRow(); row.PendingAskUserJson = Checkpoint(row);
    var resolver = new FakeResolver { Result = AskUserResolutionResult.InvalidSubmission("MUST_NOT_EXPORT_REASON") };
    var checkpoints = new FakeCheckpoints(); var (processor, _, _) = Processor(row, resolver: resolver, checkpoints: checkpoints);
    var result = await processor.ProcessAsync(Answer()); Check(result.Error == "invalid_answer" && checkpoints.Responses.Count == 0);
}
static async Task AdmissionOutcomes()
{
    var (processor, _, queue) = Processor(ConversationRow()); queue.Status = InboundDispatchStatus.Busy;
    Check((await processor.ProcessAsync(Send())).Error == "busy");
    queue.Failure = new IOException("MUST_NOT_EXPORT_TOKEN");
    var result = await processor.ProcessAsync(Send(2)); Check(result.Error == "outcome_unknown" && !result.Ok);
    Check((await processor.ProcessAsync(Send(2))).Error == "outcome_unknown" && queue.Messages.Count == 2);
    Check(!Encoding.UTF8.GetString(HubProtocol.Serialize(result)).Contains("MUST_NOT_EXPORT", StringComparison.Ordinal));
}
static async Task Dedupe()
{
    var (processor, store, queue) = Processor();
    var results = await Task.WhenAll(processor.ProcessAsync(Start()), processor.ProcessAsync(Start()));
    Check(results.All(x => x.Ok) && store.Creates == 1 && queue.Messages.Count == 1);
    Check((await processor.ProcessAsync(Start() with { Payload = new("helper", Message: "changed") })).Error == "command_conflict");
}
static async Task DedupeBound()
{
    var (processor, _, queue) = Processor(ConversationRow(), capacity: 2);
    Check((await processor.ProcessAsync(Send(1))).Ok && (await processor.ProcessAsync(Send(2))).Ok);
    Check((await processor.ProcessAsync(Send(3))).Error == "command_capacity");
    Check((await processor.ProcessAsync(Send(1))).Ok && queue.Messages.Count == 2);
}
static async Task ExistingConversation()
{
    var first = Processor(); var result = await first.Processor.ProcessAsync(Start());
    var second = Processor(first.Store.Rows[result.Data!.ConversationId]);
    Check((await second.Processor.ProcessAsync(Start())).Error == "outcome_unknown" && second.Queue.Messages.Count == 0 && second.Store.Creates == 0);
}
static async Task FragmentedFrame()
{
    var socket = new FakeSocket(); socket.Incoming.Enqueue((Utf8("{\"type\":"), WebSocketMessageType.Text, false));
    socket.Incoming.Enqueue((Utf8("\"command\"}"), WebSocketMessageType.Text, true));
    using var transport = new HubWebSocketTransport(socket);
    Check(Encoding.UTF8.GetString((await transport.ReceiveAsync())!) == "{\"type\":\"command\"}");
}
static async Task OversizeFrame()
{
    var socket = new FakeSocket();
    for (var n = 0; n < 65; n++) socket.Incoming.Enqueue((new byte[16_384], WebSocketMessageType.Text, n == 64));
    using var transport = new HubWebSocketTransport(socket);
    await ExpectProtocol(async () => { await transport.ReceiveAsync(); }, "frame_too_large");
}
static async Task BinaryFrame()
{
    var socket = new FakeSocket(); socket.Incoming.Enqueue((Utf8("{}"), WebSocketMessageType.Binary, true));
    using var transport = new HubWebSocketTransport(socket);
    await ExpectProtocol(async () => { await transport.ReceiveAsync(); }, "invalid_frame");
    socket.Incoming.Enqueue(([], WebSocketMessageType.Close, true)); Check(await transport.ReceiveAsync() is null);
}
static async Task SendLock()
{
    var socket = new FakeSocket(); using var transport = new HubWebSocketTransport(socket);
    await Task.WhenAll(Enumerable.Range(0, 20).Select(n => transport.SendAsync(HubResult.Accepted(Id(n), "c_one"))));
    Check(socket.MaxConcurrentSends == 1 && socket.Sent.Count == 20);
    foreach (var bytes in socket.Sent) Check(JsonDocument.Parse(bytes).RootElement.GetProperty("type").GetString() == "result");
}
static async Task TransportCancellation()
{
    var socket = new FakeSocket(); using var transport = new HubWebSocketTransport(socket);
    using var cancellation = new CancellationTokenSource(); cancellation.Cancel();
    try { await transport.ReceiveAsync(cancellation.Token); throw new Exception("receive ignored cancellation"); } catch (OperationCanceledException) { }
    try { await transport.SendAsync(HubResult.Accepted(Id(), "c_one"), cancellation.Token); throw new Exception("send ignored cancellation"); } catch (OperationCanceledException) { }
}
