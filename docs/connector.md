# Install a world connector

The `botnexus-hub-connector` extension runs inside a world and opens an **outbound** WebSocket to the Hub's connector ingress. It needs a per-world connector token, not a gateway admin key or the proxy shared secret. Hub never needs access to the world's database, configuration, workspace or container socket.

These instructions are for the world owner to carry out in an explicitly chosen world. They do not authorize an assistant to change or restart the running host world. Plan installation and the gateway restart separately from Hub setup and tests.

## 1. Prepare and enroll

- Install Git and the .NET 10 SDK on the machine that builds extensions. Use the same BotNexus home and service account as the target gateway. Git must reach the public repository without an interactive credential prompt; never put credentials in the URL.
- Check that the installed BotNexus supports the out-of-tree `extensions add`/`sync` lifecycle. The commands below are documented at pinned platform commit [`04052bbe283ac9106277a841b8d4c60a5c3ce095`](https://github.com/sytone/botnexus/blob/04052bbe283ac9106277a841b8d4c60a5c3ce095/docs/extension-development.md#out-of-tree-extension-repositories). An older installed gateway may not support them. This is not an instruction to update it.
- Review the [public Hub repository](https://github.com/sytone/botnexus-hub), its dependencies and the exact **Hub repository commit** you intend to install. Extension builds can run MSBuild tasks, and the loaded extension has the gateway process's permissions. Registration is not a sandbox.

Open the operator URL, such as `https://hub.example.com`, as a top-level page. Complete the external proxy's sign-in and choose **Add world**. There is no separate Hub operator password or token.

| Enrollment field | Example / rule |
| --- | --- |
| World ID | `workshop`; 1–64 characters matching `[A-Za-z0-9][A-Za-z0-9_.-]{0,63}`. Lowercase kebab-case is easiest to read. Keep it stable. |
| Display name | `Workshop`; use at most 160 characters for connector compatibility. |
| World portal URL | The browser-reachable portal root, for example `https://world.example.com`, including a path prefix if needed. No credentials, query or fragment. |

The operation is preauthenticated `POST /api/worlds` with JSON `{"id":"workshop","name":"Workshop","baseUrl":"https://world.example.com"}`. Like every operator mutation, it needs `X-Hub-Request: 1` and an `Origin` matching `HUB_PUBLIC_URL`.

Enrollment returns the connector credential **once**. The `token` and `connectorToken` response fields are aliases for the same credential, not two credentials. Later world-list responses cannot recover it. The UI clears the enrollment token when its dialog closes.

**Shown once does not mean usable once.** The connector reuses this token for each WebSocket connection until explicit rotation or revocation. Ordinary snapshots, command frames, receipts and world-list responses do not echo it.

Save the token directly into a protected file readable by the gateway service account. Use owner-only permissions on Linux or a restricted ACL on Windows. Do not paste it into a command argument, Git URL, source control, logs or chat. If it is lost, explicitly rotate that world with `POST /api/worlds/{id}/rotate-token`; this disconnects the old connector. Do not delete all enrollment/state to fix one credential.

## 2. Register and sync the public source

Run these commands yourself on the target BotNexus host, using its normal BotNexus home. **Replace `<reviewed-hub-commit>` before running.** It is the reviewed commit in the Hub repository, not the platform commit above.

```text
botnexus extensions add --id botnexushub --url https://github.com/sytone/botnexus-hub.git --ref <reviewed-hub-commit>
botnexus extensions list
botnexus extensions sync
```

Use a pinned commit for reproducible installation. The public Git URL is a source reference, not a Hub service address. No private Git host, local repository path or credential-bearing URL is required.

`add` saves registration metadata only. `sync` fetches/resolves the pinned source, discovers projects beside a manifest, builds them in Release and deploys their outputs. It processes **all enabled repository registrations**, so review those before syncing. Check the exit status and persisted lifecycle result/resolved commit. A copied directory or resolved SHA alone does not prove a successful build/deployment. Sync does not hot-load the running gateway.

Keep these IDs separate:

| ID | Purpose |
| --- | --- |
| `botnexushub` | Source repository registration used by `extensions` commands. |
| `botnexus-hub-connector` | Manifest ID, deployed extension folder and gateway configuration key. |

Normal repository sync passes the matching BotNexus checkout as `BotNexusRepoRoot` for source references. Keep the extension and gateway contracts compatible. If that build fails, resolve the reported compatibility/reference problem. Do not bypass validation, patch the running core or combine DLLs from different builds.

## 3. Configure the connector

Merge this object into the target world's configuration; do not replace unrelated settings. The exact scope is **`gateway.extensions.botnexus-hub-connector`**, not `gateway.extensions.defaults` or an agent's extensions bag. Loader settings, if needed, belong separately under `gateway.extensionLoader`.

Use the separate **connector origin**, for example `https://connect.example.com`. Do not use `https://hub.example.com`: the operator listener blocks connector routes and its external sign-in is for browsers. `/api/me` exposes the configured connector root as `connectorUrl`, and the enrollment snippet uses that value.

```json
{
  "gateway": {
    "extensions": {
      "botnexus-hub-connector": {
        "enabled": true,
        "url": "https://connect.example.com",
        "worldId": "workshop",
        "worldName": "Workshop",
        "worldBaseUrl": "https://world.example.com",
        "worldTokenFile": "/run/secrets/hub-world-token",
        "allowedAgents": ["helper"]
      }
    }
  }
}
```

Replace the secret-file path with one readable by the gateway process or container. Windows services also need a service-readable path and a private ACL. The file contains only the reusable connector token, with optional surrounding whitespace. It is local to the gateway, not the Hub.

`allowedAgents` is **mandatory and explicit**. Replace `helper` with actual enabled, visible, named-agent IDs from that world. IDs are case-sensitive; patterns such as `*` are rejected. Missing/empty allowlists disable the connector. Allowlisting is checked again for every command and does not grant new agent tools.

The UI's generated snippet may contain `worldToken`. Prefer replacing that field with `worldTokenFile`; do not keep both. Give `url` the connector root, **not** the complete connection endpoint. The connector appends `/api/connect/{worldId}` and converts HTTPS to WSS.

Non-loopback connections require WSS by default, with normal certificate validation. The connector does not use browser cookies, follow login redirects or participate in interactive preauthentication. Its `Authorization: Bearer` token authenticates only its enrolled world. The proxy shared secret must never be installed in a connector.

For an isolated connector on the same host/network namespace as a development ingress, `http://127.0.0.1:8788` can be used. Inside the disposable Compose network, the fixtures use `ws://hub:8081` with an explicit test-only insecure setting. `localhost` inside a world container is the world itself.

The portal URL is only for browser handoff; users still sign in to that world normally. No inbound gateway port or gateway admin credential is needed.

### Environment alternative and settings

You may supply the same configuration in the **gateway process environment**, for example through its service/container definition. This is not a reason to edit a running world during tests. A Hub checkout's `.env` is not automatically a gateway environment file.

```dotenv
HUB_ENABLED=true
HUB_URL=https://connect.example.com
HUB_WORLD_ID=workshop
HUB_WORLD_NAME=Workshop
HUB_WORLD_BASE_URL=https://world.example.com
HUB_WORLD_TOKEN_FILE=/run/secrets/hub-world-token
HUB_ALLOWED_AGENTS=helper
```

For several agents, use a comma-separated value such as `helper,researcher`. Use your own confirmed connector URL and local secret-file path.

These settings come from [`HubConnectorOptions.cs`](../src/BotNexus.Extensions.HubConnector/HubConnectorOptions.cs):

| Config property | `HUB_` environment variable | Requirement / default |
| --- | --- | --- |
| `url` | `HUB_URL` | Required connector root; HTTPS/WSS for remote worlds. |
| `worldId` | `HUB_WORLD_ID` | Required; exactly the enrolled routing ID. |
| `worldName` | `HUB_WORLD_NAME` | Required display name, at most 160 characters. |
| `worldBaseUrl` | `HUB_WORLD_BASE_URL` | Required HTTP(S) portal root. |
| `worldToken` / `worldTokenFile` | `HUB_WORLD_TOKEN` / `HUB_WORLD_TOKEN_FILE` | Required; prefer a protected file. |
| `allowedAgents` | `HUB_ALLOWED_AGENTS` | Required explicit IDs; at most 100. JSON accepts an array or comma-separated string. |
| `enabled` | `HUB_ENABLED` | `true`. |
| `snapshotIntervalSeconds` | `HUB_SNAPSHOT_INTERVAL_SECONDS` | `15`; range 1–3600. |
| `activityLookbackDays` | `HUB_ACTIVITY_LOOKBACK_DAYS` | `7`; range 1–365, for recent conversations. |
| `maxConversations` | `HUB_MAX_CONVERSATIONS` | `100`; range 1–100. |
| `maxQuestions` | `HUB_MAX_QUESTIONS` | `100`; range 1–100, independent of the conversation lookback. |
| `includeWorldIdentity` | `HUB_INCLUDE_WORLD_IDENTITY` | `false`; optionally export the world's own ID/name as additional metadata. |
| `allowInsecure` | `HUB_ALLOW_INSECURE` | `false`; deliberate isolated development only, never a TLS workaround. |

For each setting, `BOTNEXUSHUB_...` takes precedence over `HUB_...`, then the gateway config property. Environment settings also accept a `_FILE` suffix, and config settings accept a `File` suffix. Set a value **or** its file form, not both. Remove stale higher-priority settings when switching configuration methods. Configuration and secret files are read when the connector starts; changes need a separately planned restart.

## 4. Plan the user-only restart and verify

After successful sync and configuration, **the world owner** chooses a maintenance window and checks that the target world's active work can be interrupted. Only then should the owner run:

```text
botnexus gateway restart
```

This is a user-operated installation step, never a Hub test/deployment action for an assistant to execute against the running host world. Gateway start/restart may reconcile enabled repositories again; a successful earlier sync is not an exemption from build/reference checks.

After the owner's restart:

1. Check the world's normal extension view/logs for loaded manifest ID `botnexus-hub-connector`. `extensions list` shows repository lifecycle state, not proof that the service is loaded.
2. Check Hub for an online world and a current-connection snapshot with only the intended agents. The connector sends an initial snapshot and normally updates every 15 seconds.
3. Open its portal link and a conversation handoff. Verify they reach the correct world and still use that world's normal authentication.
4. In a deliberately approved test conversation, try a harmless message/question. An accepted receipt is queue admission, not model completion. Inspect the world after an uncertain result; do not auto-retry a write.

If offline, check the selected BotNexus home, lifecycle result, exact IDs, explicit agent allowlist, secret-file readability, higher-priority environment values, TLS and routing to connector port `8081`. A login redirect usually means the connector was sent to an operator route. Invalid configuration disables only the connector and logs a fixed warning. Do not print secrets to diagnose it or enable insecure transport to hide a TLS problem. An offline world's retained snapshot is not fresh data.

## Build and reference checks

Normal installation uses the registered source lifecycle. For an isolated source-reference check, use a compatible BotNexus source checkout:

```text
dotnet build src/BotNexus.Extensions.HubConnector/BotNexus.Extensions.HubConnector.csproj -c Release -p:BotNexusRepoRoot=/isolated/botnexus
```

Replace `/isolated/botnexus` with your dedicated matching source tree, never the running world's codebase. This check is separate from tests against matching binary contracts.

### Binary build helper

For isolated packaging or contract checks, publish against a **complete matching gateway assembly directory**. From the Hub checkout:

```text
dotnet publish src/BotNexus.Extensions.HubConnector/BotNexus.Extensions.HubConnector.csproj -c Release -o artifacts/botnexus-hub-connector -p:BotNexusBinaryDir=/matching/gateway
```

Replace `/matching/gateway` with an isolated assembly directory from the matching build. Quote the full `-p:BotNexusBinaryDir=...` argument if its path has spaces. Setting this property selects binary references automatically; it does not rebuild the gateway.

The project requires `BotNexus.Domain.Wire.dll`, `BotNexus.Domain.dll`, `BotNexus.Gateway.Contracts.dll`, `BotNexus.Gateway.Dispatching.dll` and `Vogen.SharedTypes.dll` from the **same matching build**. Missing references are build errors. Never fill gaps with another revision's DLLs or disable the validation gate.

The local publish folder includes the manifest and dependency output; publishing alone does not install or load it. Keep generated binaries out of the public source-only release. Prefer registered source sync for normal installation, since unregistered manual output can be pruned by later lifecycle deployment.

## Data limits and privacy

Only allowlisted visible enabled named agents and active, user-facing, top-level conversations are exported. Defaults are a seven-day recent-activity window, at most 100 conversations and 100 pending questions. Pending questions are queried separately, so an older unresolved question can still appear. Snapshots contain titles, timestamps, routing IDs and explicit question text/choices; treat that content as sensitive.

**Scalability caveat:** the older shared store contract's `GetSummariesAsync` and `GetPendingAskUserCheckpointsAsync` have no limit/lookback parameter. Each snapshot reads those metadata sets before filtering/capping output, then checks relevant conversation metadata by ID. Lowering `maxConversations` does not bound that initial store work. The snapshot builder does not scan session history or full transcripts. Snapshots exclude transcripts, tool payloads, system instructions, authentication and configuration. Large worlds need performance validation before enrollment; output caps do not bound metadata retrieval.

Hub also stores submitted command payloads, including messages and answers. Its data volume and backups are private data, even though snapshots are metadata-only. Connector tokens are hashed in Hub storage and do not appear in ordinary frames or responses.

Hub offers cross-world overview and three restricted commands, not a full chat/transcript replacement. Continue detailed work in the world's portal. See [protocol](protocol.md) for wire and delivery semantics, [operations](operations.md) for token recovery/backups, and [verification](verification.md) for disposable real-gateway tests with a deterministic provider.
