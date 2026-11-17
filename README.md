# BotNexusHub

**One place to see and work across your BotNexus worlds.** Run the Hub once, install the connector in each world, then choose which agents it can access.

**Version 0.3.0** · [Source](https://github.com/sytone/botnexus-hub) · [Report an issue](https://github.com/sytone/botnexus-hub/issues)

## Contents

1. [What this is](#what-this-is)
2. [Run the Hub in a container](#run-the-hub-in-a-container)
3. [Install the extension into an existing world](#install-the-extension-into-an-existing-world)
4. [Register and connect a world](#register-and-connect-a-world)
5. [Development and issues](#development-and-issues)

**Already have a running Hub?** Start with [extension installation](#install-the-extension-into-an-existing-world), then [register the world](#register-and-connect-a-world). Installing the extension and enrolling a world are separate steps.

<a id="what-you-can-do"></a>

## What this is

BotNexusHub is a separate Python/FastAPI app with SQLite storage. It is not a BotNexus core fork and does not replace a world's own portal.

- **Hub:** a dashboard for connected worlds, allowlisted agents, recent conversations, open questions, activity and numeric metrics history.
- **World connector:** a small .NET extension loaded by each world's gateway. It opens an **outbound** WebSocket to Hub; no inbound world port or gateway admin key is needed.
- **World portal:** where you open the full conversation and continue detailed work using that world's normal sign-in.

From Hub, you can start a conversation, send a message, answer/cancel a pending question, or open the world's portal. The UI supports desktop/mobile and system, light and dark themes. Each world card can show one to three compact metric graphs. Open **Metrics** to compare worlds over time and filter by agent, conversation and date/time. [Metrics, collection and retention](docs/metrics.md).

Metrics are stored separately from commands in `metrics.sqlite`, with **seven days of history by default**. You can change retention in the Metrics page. Resource counters and IDs are small, but SQLite indexes and per-sample rows still have a cost; a row cap is an additional safety bound, not a promise of unlimited storage.

**This is not read-only.** Hub can send commands to allowlisted agents and receives their visible conversation metadata and explicit question text/choices. It does not copy full transcripts, tool output, system prompts or world credentials. Hub also stores the messages and answers submitted through it; protect its data volume and backups.

Run **one Hub instance with one backend process**. Multiple workers/replicas are not supported. Everyone admitted by your external access policy can control all enrolled worlds; Hub has no per-user roles or world permissions.

A successful command receipt means **accepted by the world**, not that the agent finished. If a write is uncertain, inspect the world before submitting a fresh command. Do not automatically replay it.

<a id="start-a-hub"></a>

## Run the Hub in a container

Yes—the Hub runs separately in containers. The supplied Compose file starts the app and its guarded nginx ingress. You do **not** need to run the demo worlds or install BotNexus on the Hub host.

Already have a configured Hub? Use [operations and upgrades](docs/operations.md#deploy) instead of rerunning first-time setup. Keep existing settings, enrollments and volumes.

### 1. Prepare the Hub host

You need:

- Git, **Python 3.12** for the setup script, and Docker with Compose, or a compatible Podman/Compose setup.
- Two HTTPS origins you control: one for operator/browser access and one for world connectors.
- An external reverse proxy that handles sign-in/access policy and can inject a protected upstream header. Hub has **no built-in login**.

All service addresses below use `example.com` placeholders. Substitute your own addresses. NetBird is [optional](docs/operations.md#optional-netbird-profile), not a Hub requirement.

Clone the public source into a dedicated deployment directory, separate from world state and development worktrees:

```text
git clone https://github.com/sytone/botnexus-hub.git
cd botnexus-hub
git switch main
git rev-parse HEAD
python scripts/configure.py
```

Review the selected source revision before deployment. `main` moves; record the reviewed commit. The Compose project/container names are fixed, so do not start a second copy on an engine that already runs this stack.

The configurator creates an **untracked `.env`**, generates only the proxy shared secret, and refuses to overwrite an existing file. Privately edit it:

| Setting | Set it to |
| --- | --- |
| `HUB_PUBLIC_URL` | Your operator origin, for example `https://hub.example.com`. |
| `HUB_CONNECTOR_PUBLIC_URL` | Your separate connector origin, for example `https://connect.example.com`. |
| `HUB_COMMIT` | The reviewed commit printed by `git rev-parse HEAD`. |
| `HUB_PROXY_SECRET` | Keep the generated value private; install the same value in the trusted proxy's protected upstream-header configuration. |

Use HTTPS origin roots without a path, query, fragment or credentials. Protect `.env` with owner-only permissions on Linux or a private Windows ACL. Do not print it into logs, chat or source control. The proxy secret is **not** a world token and must never reach a browser or connector.

### 2. Start the Hub and ingress

From the reviewed deployment checkout:

```text
docker compose up -d --build hub ingress
docker compose ps hub ingress
```

Check [http://127.0.0.1:8785/health](http://127.0.0.1:8785/health) for the expected version and commit. The local dashboard root should be denied without proxy admission; a health response alone does not prove that sign-in or access policy works.

<a id="access-architecture"></a>

### 3. Configure secure access

Keep these **two Hub addresses** separate from each world's portal address:

| Address | Used by | Route to the guarded listener |
| --- | --- | --- |
| `https://hub.example.com` | Your browser; external sign-in required. | Operator `8080`, published by default at host `127.0.0.1:8785`. |
| `https://connect.example.com` | World extensions; per-world Bearer token, no interactive sign-in. | Connector `8081`, published by default at host `127.0.0.1:8788`. |
| Your world's portal URL | Your browser when opening that world. | That world's existing portal, **not** either Hub listener. |

The external proxy must:

1. Enforce sign-in and the intended access/group policy **before** forwarding operator requests.
2. Strip/overwrite caller-supplied `X-Hub-Proxy-Secret`, then inject your private value only after access is approved. Never return or log it. Protect proxy administration and configuration.
3. Route the connector origin to `8081`, preserving WebSocket upgrades and `Authorization: Bearer`. Do not put an interactive browser login on this route.
4. Preserve the browser's real `Origin`. Operator API mutations also require `X-Hub-Request: 1` and an Origin matching `HUB_PUBLIC_URL`; the Hub UI sends these.

The backend binds to **container loopback `127.0.0.1:8785`** and is not an operator authentication boundary. Never publish or proxy straight to it. The similarly numbered **host** port reaches guarded ingress, not the backend. A proxy in a different network namespace needs an explicitly reviewed route to the guarded listeners; its `localhost` is not this host.

Open your operator HTTPS URL as a **top-level browser page** and sign in through the proxy. Verify both denied and authorized access plus the connector route before enrolling real worlds. The proxy owns sessions and sign-out; Hub's **Reload secure access** does not retry writes. See the [full proxy contract](docs/operations.md#external-preauthentication) and [access-policy checks](docs/verification.md#ingress-and-access-policy-gates).

**Keep your data:** the default database volume is `botnexushub_hub-data`; include it and private deployment settings in backups. Optional NetBird identity uses `botnexushub_netbird-state`; preserve it and back it up privately or keep an intentional re-enrollment plan. A normal redeploy keeps volumes. **Never use `compose down -v` on a configured Hub.** See [backup and recovery](docs/operations.md#persistent-data-and-backups).

## Install the extension into an existing world

Do this **on each world's host**, using the account and existing BotNexus home that run its gateway. No separate Hub clone is needed there. This section registers/builds the extension source; the [next section](#register-and-connect-a-world) enrolls the world, saves its token and configures it.

Do not initialize a new BotNexus home, replace world settings, rebuild BotNexus core, or start this repository's Hub/test stack on the world. Installing/syncing an extension is not a gateway restart.

### 1. Check prerequisites and select the existing home

- Use **PowerShell 7.3+** with standard native argument passing for the commands below, not Windows PowerShell 5.1 or `cmd.exe`.
- Have Git and the **.NET 10 SDK** available in the extension build environment. For a containerized world, that means the environment where sync builds the extension, not just its outer host.
- Check that the installed BotNexus supports `extensions add`, `update`, `sync` and `config get`/`set`. See the [platform extension guide](https://github.com/sytone/botnexus/blob/04052bbe283ac9106277a841b8d4c60a5c3ce095/docs/extension-development.md#out-of-tree-extension-repositories). If unsupported, stop and check compatibility; this is not an instruction to update the running platform.
- Review the connector source, dependencies and selected ref. Extension builds can run MSBuild tasks, and loaded extensions have the gateway process's permissions. Registration is not a sandbox.

```powershell
$WorldHome = if ($env:BOTNEXUS_HOME) {
    $env:BOTNEXUS_HOME
} else {
    Join-Path $HOME '.botnexus'
}
```

**Check `$WorldHome` matches the existing gateway's home directory.** Override it if that gateway uses another location. It is a directory, not a JSON/database filename. Keep this PowerShell session open for the registration steps below.

First check existing registrations and agent IDs:

```powershell
botnexus --target $WorldHome extensions list
botnexus --target $WorldHome agent list
botnexus --target $WorldHome config get gateway.extensionLoader.enabled
botnexus --target $WorldHome config get gateway.extensionLoader.path
```

Confirm dynamic extension loading is enabled and any custom loader path matches the extension deployment directory. If loading is disabled or the path is unexpected, stop for owner review rather than changing loader settings automatically.

### 2. Register and build the connector source

If `botnexushub` is **not registered**:

```powershell
botnexus --target $WorldHome extensions add --id botnexushub --url https://github.com/sytone/botnexus-hub.git --ref main
```

If it **is already registered**, use this instead:

```powershell
botnexus --target $WorldHome extensions update --id botnexushub --ref main --updates
```

If `extensions list` shows this repository is **disabled**, enable it only if you intend to use it:

```powershell
botnexus --target $WorldHome extensions enable --id botnexushub
```

`--updates` permits source updates; it does not enable a disabled registration. The repository's enabled state is also separate from the connector's `enabled` config field.

Then sync:

```powershell
botnexus --target $WorldHome extensions sync
```

**Sync processes all enabled extension repositories, not just this one.** Review the list first, check every command's exit status, and stop if sync reports a failure. Record the resolved commit. Sync fetches/builds/deploys the selected source; it does not hot-load the running gateway.

`--ref main` means latest source, so review changes before each update. A later sync or gateway startup may resolve a newer `main`. For a fixed revision, use a reviewed commit or an exact published tag from the [tags page](https://github.com/sytone/botnexus-hub/tags). The displayed app version is not automatically a Git tag; use `--ref <release-tag>` only after replacing the placeholder with a real tag.

Keep the two IDs separate: **`botnexushub`** is the repository registration used by `extensions` commands; **`botnexus-hub-connector`** is the extension manifest ID and configuration key. Resolve build/reference errors against a compatible gateway; do not combine DLLs from different builds or bypass validation. [Build details](docs/connector.md#build-and-reference-checks).

## Register and connect a world

With the Hub running and the extension synced, enroll the world and give the connector its own protected token. The example uses a world called **Home**. Repeat with a **separate ID, enrollment and token per world**.

### 1. Set the addresses and token-file path

Continue in the PowerShell session on the world's host. If you opened a new session, [set and check `$WorldHome` again](#1-check-prerequisites-and-select-the-existing-home).

```powershell
# Replace these three URLs before continuing.
$HubUrl = 'https://hub.example.com'
$HubConnectorUrl = 'https://connect.example.com'
$WorldPortalUrl = 'https://YOUR-HOME-WORLD-PORTAL'
$TokenFile = Join-Path $WorldHome 'secrets/hub-home-token.txt'
```

`$WorldPortalUrl` is the URL you normally use to open **that world's portal**, not Hub. `$HubConnectorUrl` is the separate connector origin, **not** the operator/sign-in URL or the full `/api/connect/...` endpoint. Hub exposes the configured connector root as `connectorUrl` from `/api/me`; the extension converts HTTPS to WSS and appends the world endpoint.

### 2. Add the world in Hub and save its token

Open `$HubUrl`, sign in, choose **Add world**, and enter:

- **ID:** `home`
- **Name:** `Home`
- **Portal URL:** the value of `$WorldPortalUrl`

Create the parent secrets directory if needed. Save the displayed token **directly into `$TokenFile` as plain text**, using a private editor. The file contains only the token, not JSON. Restrict its permissions to the gateway's service account: owner-only permissions on Linux or a private ACL on Windows. Do not paste the token into chat, a shell command, Git or logs. Do not use the proxy shared secret.

The token is **shown once, but reusable** for reconnects until rotation or revocation. Hub cannot show it again. The enrollment dialog clears it when closed; save it securely before closing. If it is lost, use [per-world token recovery](docs/operations.md#recovery), not a database reset.

`worldTokenFile` must be an absolute path **as seen by the gateway process**. A Windows service needs a service-readable Windows path. For a containerized world, mount only that token file read-only and use its container path; the outer host's path is not automatically visible inside the world. Do not mount world state or credentials into Hub.

### 3. Configure the connector with the CLI

<a id="cli-add-or-replace-the-connector-configuration"></a>

Inspect the current connector settings **in a private terminal**:

```powershell
botnexus --target $WorldHome config get gateway.extensions.botnexus-hub-connector
```

A previous inline `worldToken` may appear in that output. Do not share it or use shell tracing. An unset extension may read as `null`.

For a **new installation**, use:

```powershell
$Connector = @{
    enabled        = $true
    url            = $HubConnectorUrl
    worldId        = 'home'
    worldName      = 'Home'
    worldBaseUrl   = $WorldPortalUrl
    worldTokenFile = $TokenFile
    allowedAgents  = @('tinker')
} | ConvertTo-Json -Compress

botnexus --target $WorldHome config set gateway.extensions.botnexus-hub-connector $Connector
botnexus --target $WorldHome config get gateway.extensions.botnexus-hub-connector
```

Use `tinker` **only if it appeared in `agent list`**; otherwise substitute a confirmed enabled, visible, named-agent ID. `allowedAgents` is mandatory and case-sensitive. An empty list disables the connector; wildcards such as `*` are rejected. This grants Hub command access and exposes the allowed metadata/question text—it is not a read-only monitor.

This creates or replaces **only the connector's settings object**. It does **not merge fields**. If settings already exist, retain any optional fields you need in the complete object before setting it again. Never set the entire `gateway` or `gateway.extensions` object, and do not write dotted child keys such as `...botnexus-hub-connector.url`. Do not edit the world's JSON/database files. See the [config CLI reference](https://github.com/sytone/botnexus/blob/04052bbe283ac9106277a841b8d4c60a5c3ce095/docs/cli-reference.md#config-set).

Check the exit status and readback against your intended values. If Hub's generated snippet uses `worldToken`, replace that field with `worldTokenFile`; do not keep both. Existing `BOTNEXUSHUB_...` or `HUB_...` values in the **gateway process environment** override config settings. Check for stale overrides before restarting. A Hub checkout's `.env` is not a world configuration file. [All settings and environment rules](docs/connector.md#environment-alternative-and-settings).

### 4. Restart when you can interrupt active work

After sync succeeds and configuration is saved, **the world owner** checks active work and chooses a maintenance window, then runs the world's normal restart. For a CLI-managed gateway:

```powershell
botnexus --target $WorldHome gateway restart
```

Restart interrupts active work. This is a user-operated installation step, **not permission for an assistant, Hub deployment or test to restart the running host world**. Use the normal service/container lifecycle for a world that is not CLI-managed.

Then:

1. Check the gateway's extension view/logs for loaded ID `botnexus-hub-connector`. `extensions list` alone proves registration, not a loaded service.
2. In Hub, **Home** should become online with a fresh snapshot and only the intended agents, normally within about 15 seconds.
3. Choose **Open world**, then a conversation handoff. Confirm the correct portal and its normal sign-in. Test writes only in a conversation approved for that test; acceptance is not model completion.
4. With connector 0.3.0 or later, metrics start collecting from this point. CPU and I/O rates need a second sample; older connectors remain online but show no new metrics. The default metric interval is 30 seconds. To update an existing world, keep its enrollment/token and use the extension update/sync steps above, followed by the owner’s planned gateway restart. No history is invented or backfilled from transcripts.

If it stays offline, check the selected home, sync result, agent IDs, token-file access, environment overrides, TLS and routing to connector listener `8081`. A login redirect usually means the operator URL was used. Do not print the token or disable TLS verification. A retained offline snapshot is not fresh data.

<a id="correct-a-portal-url-or-remove-a-world"></a>

### Correct a portal URL, disconnect, or remove a world

- **Wrong portal URL:** set the correct `worldBaseUrl` in the complete connector object, preserving its ID, token-file path, connector `url`, allowlist and optional fields. After the owner's gateway restart, Hub uses the next authenticated snapshot's address for world/conversation/question links. The 0.2.0 connector already supports this; no rebuild, new enrollment or token is needed. The enrollment address is the fallback before a snapshot; the last snapshot address is retained offline.
- **Temporary outage/restart:** keep the enrollment and token. The connector can reconnect; offline worlds do not expire automatically.
- **Retire a world:** choose **Remove world** and type its exact ID. This revokes enrollment/token and disconnects the socket. It does not uninstall the extension or delete that world's conversations/files. A still-running connector may retry, but **cannot re-register itself**. Its owner can disable it and plan a gateway restart to stop retries.
- **Reconnect after removal:** explicitly add the world again, replace its protected token file with the **new** token, and have the owner restart its gateway. A failed/uncertain removal is not automatically retried; refresh the world list before deciding on another action.

See the [full connector guide](docs/connector.md) for all settings, limits and recovery details.

### Metrics and history

- On a world card, choose its metric settings to select **one to three** resource charts. Click a graph to open that world’s full history.
- Choose **Metrics** in the header to compare all worlds. Add or remove metrics and filter by world, agent, conversation and date/time. Different units use separate charts; different worlds stay separate series.
- History starts when a metrics-capable connector connects. Blank means unavailable, not zero. Agent/conversation filters apply only to metrics with those IDs; machine counters are not attributed to an agent.
- Start with **7 days** of retention. Changing the retention setting is an explicit shared Hub change; reducing it expires older metric samples. It does not prune command receipts or world conversations.
- See [the metrics guide](docs/metrics.md) for counter definitions, container limits, storage caps, SQLite coverage and disabling collection.


<a id="development-and-verification"></a>

## Development and issues

### Local development and tests

Work in an isolated development checkout with Python 3.12, Node.js with its built-in test runner, Git and the .NET 10 SDK. Create/activate a virtual environment using your platform's normal steps, then from the repository root:

```text
python -m pip install -r requirements-dev.txt
python -m pytest -q
node --test tests/*.test.cjs
dotnet run --project tests/Connector.Tests/Connector.Tests.csproj -c Release -p:BotNexusBinaryDir=/matching/gateway
git diff --check
```

Replace `/matching/gateway` with a **complete, isolated assembly directory from the matching gateway build**, never the running world's codebase. The connector suite is an executable (`dotnet run`), not a `dotnet test` project. Normal extension sync uses source references through `BotNexusRepoRoot`; see [build/reference checks](docs/connector.md#build-and-reference-checks).

All application changes require Python tests, Node UI/theme tests, connector build/tests and isolated container/browser E2E. Unit, ingress/API, browser and recovery checks are separate gates; see the [verification checklist](docs/verification.md). A documentation change is not evidence that an unrun app gate passed.

### Disposable integration worlds

The optional `test` profile uses **real, unchanged BotNexus gateways** pinned to `04052bbe283ac9106277a841b8d4c60a5c3ce095`, including portals and durable `ask_user`. Only the OpenAI-compatible provider is a deterministic local fixture. No external model or user credentials are needed; this does not validate real provider behavior.

These synthetic worlds have separate volumes and an internal test network. Plain WS is enabled only for those deliberately isolated fixtures; real connectors should use WSS. Use a dedicated disposable engine or a fully reviewed isolated stack—**never launch the fixed-name profile beside a managed live deployment or point mutating tests at a live Hub**. Follow [disposable E2E setup](docs/verification.md#disposable-real-gateway-e2e).

Never mount a live world's configuration, state, credentials, workspace, tools or container socket into Hub or its tests. The default synthetic volumes `botnexushub_world-a-state` and `botnexushub_world-b-state` are disposable test data; exclude them from backups with an explicit reason. This rule never applies to real worlds.

### Report an issue

Use [GitHub Issues](https://github.com/sytone/botnexus-hub/issues). Include:

- Hub version/commit from `/health`, connector version/ref, and the relevant BotNexus version.
- Host/container engine, which setup step failed, expected behavior, and steps to reproduce.
- Sanitized error text and whether the problem is operator sign-in, connector transport, configuration, or a world handoff.

**Never include** tokens, `.env`, private configuration, proxy headers, database copies, full conversation/question text, or screenshots with private data. For an uncertain write, check the world and existing receipt before trying again. For a possible security issue, do not post exploit details or secrets publicly; use a private reporting channel if available, or ask for one without disclosing sensitive details.

<a id="public-release-scope"></a>

### Current limits and release scope

The default connector exports at most 100 recent conversations and 100 pending questions, with a seven-day conversation lookback. The older gateway store contract reads unbounded metadata before filtering; these output caps do not bound database work on a large world. See [data limits and privacy](docs/connector.md#data-limits-and-privacy). Hub command payloads/receipts have no automatic pruning in this version.

Public releases are **source-only**. Do not publish gateway redistributions, DLLs, PDBs, build output, databases, real `.env` files, tokens, private proxy settings or test screenshots. Review the public tree/history; do not mirror private repository history. **No open-source license has been granted for this project; maintainers have not selected a license.**

<a id="further-details"></a>

### Further reading

- [Connector settings, compatibility and privacy](docs/connector.md)
- [Deployment, access policy, backups and recovery](docs/operations.md)
- [Protocol, authentication and command semantics](docs/protocol.md)
- [Metrics, history and retention](docs/metrics.md)
- [Release verification checklist](docs/verification.md)
- [Changelog](CHANGELOG.md)
