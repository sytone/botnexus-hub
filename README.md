# BotNexusHub

**Version 0.2.0** · [Source repository](https://github.com/sytone/botnexus-hub) · [Install into an existing world](#install-the-extension-into-an-existing-world)

Already running BotNexus? Follow the [existing-world install steps](#install-the-extension-into-an-existing-world) below. Install the connector on each world; run the Hub separately.

One view across your BotNexus worlds. The Hub is a separate Python/FastAPI app with SQLite storage. Each enrolled world runs a small .NET connector that opens an outbound WebSocket to the Hub. This is not a BotNexus core fork.

## What you can do

- See connected worlds, allowlisted agents, recent active conversations, open questions and recent activity.
- Start a conversation, send a message, or answer/cancel a pending question in a connected world.
- Open the world's own portal for the full conversation and the agent's work. Hub does not copy full transcripts, tool output, system prompts or world credentials.
- Use system, light or dark theme on desktop and mobile.

A successful command receipt means **accepted by the world**, not that the agent finished. If its outcome is uncertain, inspect the world before submitting a fresh command. Do not automatically replay it.

Run **one Hub instance with one backend process**. Connector routing and send queues are in memory. Multiple workers or replicas are not supported. All operators admitted by the external access policy can control all enrolled worlds; Hub has no per-user roles or world permissions.

## Access architecture

Version 0.2.0 uses **external preauthentication**. Your reverse proxy owns identity, sign-in, SSO, group policy, MFA, session expiry and rate limits. Hub has no native operator token, password, login form, session cookie or logout endpoint.

The example addresses below are placeholders, not hosted services:

```text
Browser -> https://hub.example.com -> external preauth proxy
        -> operator ingress :8080 -> private Hub 127.0.0.1:8785

World -> wss://connect.example.com/api/connect/{worldId}
      -> connector ingress :8081 -> the same private Hub
```

The nginx ingress shares the Hub container's network namespace. The two destination listeners have different rules; a supplied `Host` header does not select an authentication mode.

| Listener | Rule | Default Compose host mapping |
| --- | --- | --- |
| Operator `8080` | Requires the proxy-injected `X-Hub-Proxy-Secret` matching `HUB_PROXY_SECRET`. Only `GET /health` is public. Connector routes are blocked. | `127.0.0.1:8785` → `8080` |
| Connector `8081` | Only a GET WebSocket upgrade to `/api/connect/{worldId}`, without a query string. The backend checks that world's `Authorization: Bearer` token. No UI, health or operator API routes. | `127.0.0.1:8788` → `8081` |
| Private backend `127.0.0.1:8785` | Internal app, not an operator authentication boundary. Never publish or proxy to it directly. | None |

Both published ports reach guarded ingress listeners, **not the raw backend**. An unauthenticated request to `http://127.0.0.1:8785/` should be denied, not show a dashboard or a Hub login page. Health alone does not prove that the external access policy works.

The preauth proxy must overwrite any caller-supplied `X-Hub-Proxy-Secret` and inject its private value **only after access is approved**. Never send this shared secret to browsers or world connectors, put it in a URL, or log it. Protect the proxy's configuration and administration: anyone who can change the upstream header or access policy can bypass this boundary.

Use any preauth provider that can meet this contract. NetBird is an optional Compose profile, not a Hub core dependency. See [operations](docs/operations.md#external-preauthentication) for the full proxy contract.

## Start a Hub

Use Python 3.12 and Docker with Compose, or a compatible Podman/Compose setup. Deploy from a clean canonical `main` checkout at the reviewed release commit, separate from development worktrees and live world state. The default Compose project and container names are fixed; do not start a competing stack on an already managed engine.

From that checkout:

```text
python scripts/configure.py
git rev-parse HEAD
```

The configurator creates an untracked `.env`, generates **only the proxy shared secret**, and refuses to replace an existing file. It does not create a user password, operator token or browser session secret.

Privately edit `.env`:

- Set `HUB_PUBLIC_URL` to your operator HTTPS origin, for example `https://hub.example.com`.
- Set `HUB_CONNECTOR_PUBLIC_URL` to a separate connector HTTPS origin, for example `https://connect.example.com`.
- Use origin roots without a path, query, fragment or credentials. Public deployments require HTTPS.
- Set `HUB_COMMIT` to the reviewed commit printed by `git rev-parse HEAD`.
- Keep `HUB_PROXY_SECRET` private. Install the same value in the preauth proxy's protected upstream-header configuration.

Protect `.env` with owner-only permissions on Linux or a private Windows ACL. Do not print it into logs, chat or source control. The generated `example.com` addresses must be replaced before real use.

```text
docker compose up -d --build hub ingress
docker compose ps hub ingress
```

Check [http://127.0.0.1:8785/health](http://127.0.0.1:8785/health) for version and commit. Then configure TLS and both external routes:

1. Operator origin → guarded operator listener, after preauthentication.
2. Connector origin → guarded connector listener, with WebSocket upgrades and the per-world Authorization header preserved. Do not put an interactive browser login on this route.

A proxy on the same host can use the loopback mappings above. A proxy in another network namespace needs an explicitly reviewed route to the guarded listeners. It must never reach a published raw backend.

Open your operator HTTPS URL as a **top-level browser page** to sign in through the proxy. The proxy manages the session and sign-out. Hub does not offer a fake local sign-out. If access expires, **Reload secure access** returns through the external entry point without retrying a mutation.

Every operator API mutation requires both `X-Hub-Request: 1` and an `Origin` matching `HUB_PUBLIC_URL`, even when the caller has passed preauthentication. See [protocol](docs/protocol.md#operator-request-boundary).

For an existing installation, use the [upgrade procedure](docs/operations.md#upgrade-from-010). Do not reset enrollment or delete volumes to change authentication.

## Install the extension into an existing world

This installs **only the `botnexus-hub-connector` extension** into a world you already run. Do not initialize a new BotNexus home, replace the world's settings, rebuild BotNexus core, or run this repository's Hub/test stack on the world. The connector runs inside its gateway and opens an outbound connection; no inbound world port or gateway admin key is needed.

### 1. Check prerequisites and enroll the world

- Have a running Hub with both [external routes](#access-architecture) configured. Note its operator URL (for browser sign-in) and its **connector URL** (for the extension). Example addresses here are placeholders, not hosted services.
- On the target world's host, use its normal service account and BotNexus home. Have Git and the **.NET 10 SDK** available to the process that builds extensions. A world in a container needs these in its build environment, not just on the outer host.
- Check `botnexus extensions --help`. Your installed BotNexus must support the out-of-tree `extensions add` and `extensions sync` lifecycle described in the [platform guide](https://github.com/sytone/botnexus/blob/04052bbe283ac9106277a841b8d4c60a5c3ce095/docs/extension-development.md#out-of-tree-extension-repositories). If it does not, stop and check compatibility; this guide is not an instruction to update the running platform.
- Review the connector source, dependencies and selected ref before installing or updating. Extension builds can run MSBuild tasks, and loaded extensions have the gateway process's permissions. Registration is not a sandbox.

Sign in to your Hub's operator URL, choose **Add world**, and enter a stable world ID such as `workshop`, a display name, and the world's browser-reachable portal URL. Keep that ID for the configuration below.

Save the token shown by Hub directly into a private text file readable by the gateway service account. It is **shown once, but reusable** for reconnects until rotation or revocation. The file contains only the token, not JSON. Use owner-only permissions on Linux or a restricted ACL on Windows. Do not put the token in a shell command, Git, logs or chat. Do not use the Hub proxy shared secret.

### 2. Register and build the extension

Run on the **existing world's host**, using the same service account and BotNexus home as its gateway. Replace `<world-home>` in every command with that existing **home directory**, not a JSON or database filename; quote paths containing spaces. No separate clone of this repository is required.

**Latest source (default):** use `--ref main`. Review the changes before each sync; `main` moves as changes are published.

```text
botnexus --target <world-home> extensions add --id botnexushub --url https://github.com/sytone/botnexus-hub.git --ref main
botnexus --target <world-home> extensions list
botnexus --target <world-home> extensions sync
```

**Already registered, or updating an older commit pin to latest?** Use `update`, not another `add`:

```text
botnexus --target <world-home> extensions update --id botnexushub --ref main --updates
botnexus --target <world-home> extensions list
botnexus --target <world-home> extensions sync
```

**Tagged release (when available):** there are currently no release tags. The displayed app version `0.2.0` is not a Git tag. Once a tag is published on the [tags page](https://github.com/sytone/botnexus-hub/tags), replace `main` in the `add` or `update` command with that exact tag. For example, replace `<release-tag>` before running:

```text
botnexus --target <world-home> extensions update --id botnexushub --ref <release-tag> --updates
botnexus --target <world-home> extensions list
botnexus --target <world-home> extensions sync
```

`add` records the repository; `update` changes its registration; `sync` fetches the selected ref, builds the extension in Release, and deploys the output. **Sync processes all enabled repository registrations**, so inspect the list first. Check for a successful lifecycle result and record the resolved commit. Sync does not hot-load the running gateway. A later sync or gateway startup may resolve a newer `main`; choose a published tag or a reviewed commit if you need a fixed revision.

The registration ID `botnexushub` is not the config key. The extension's manifest ID and config key are **`botnexus-hub-connector`**. A build/reference error must be resolved against a compatible gateway; do not mix DLLs from different builds or bypass validation.

### 3. Add the connector settings to that world

Use the supported **`config get` / `config set` CLI**, which handles the world's active configuration backend. **Do not replace the whole configuration** or edit its JSON/database files. The scope is `gateway.extensions.botnexus-hub-connector`, not `gateway.extensions.defaults` or an agent's extensions bag.

The configuration shape is shown below; the CLI value is only the inner connector object, not this whole document.

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

Replace these example values:

- `url`: your Hub's **connector origin**, not its operator/sign-in URL and not the full `/api/connect/...` endpoint. Hub exposes it as `connectorUrl` from `/api/me`. The connector turns HTTPS into WSS and appends the world endpoint. The route must preserve WebSocket upgrades and Authorization, without a browser login challenge.
- `worldId`: exactly the ID enrolled in Hub. `worldName` is its display name; `worldBaseUrl` is the world's normal browser portal URL.
- `worldTokenFile`: the private token file's absolute path **as seen by the gateway process**. The example is a Linux/container path. On Windows, use a service-readable path such as `C:/BotNexus/secrets/hub-world-token.txt` with a private ACL. In a container, mount only that token file read-only at the configured path; a host-only path is not visible inside it.
- `allowedAgents`: actual enabled, visible, named-agent IDs from that world. The list is mandatory and case-sensitive; an empty list disables the connector and wildcards such as `*` are rejected.

#### CLI: add or replace the connector configuration

First check the installed command surface, then inspect this extension's current value in a private terminal. A previous inline `worldToken` may appear in `get` output; do not share that output or use shell tracing.

```text
botnexus config get --help
botnexus config set --help
botnexus --target <world-home> config get gateway.extensions.botnexus-hub-connector
```

Replace the example values in this **single-line** command before running it. The quoting below is for **PowerShell 7.3+ (standard native argument passing) or Bash**, not Windows PowerShell 5.1 or `cmd.exe`. On Windows, replace the Linux token-file path with your service-readable Windows path.

```sh
botnexus --target <world-home> config set gateway.extensions.botnexus-hub-connector '{"enabled":true,"url":"https://connect.example.com","worldId":"workshop","worldName":"Workshop","worldBaseUrl":"https://world.example.com","worldTokenFile":"/run/secrets/hub-world-token","allowedAgents":["helper"]}'
botnexus --target <world-home> config get gateway.extensions.botnexus-hub-connector
```

The same `config set` command **creates or replaces only this extension's object**. It does not merge that object's fields. For an update, read the current object, include any optional settings you want to keep, change the needed values, and set the complete object again. Do not set the entire `gateway` or `gateway.extensions` object. Some CLI versions cannot write dotted fields inside an extension's JSON object, so use the whole extension object above rather than appending `.url` or `.allowedAgents` to the key.

Check every command's exit status and compare the readback with your intended values before restarting. An unset extension may read as `null`. If the CLI reports an unsupported path or another configuration error, stop and check that version's support; do not edit the database or a stale JSON export as a workaround. See the platform's [config CLI reference](https://github.com/sytone/botnexus/blob/04052bbe283ac9106277a841b8d4c60a5c3ce095/docs/cli-reference.md#config-set).

If you copied Hub's generated snippet, replace `worldToken` with `worldTokenFile`; do not keep both. Existing `BOTNEXUSHUB_...` or `HUB_...` settings in the gateway process environment override these values. See the [full settings and environment reference](docs/connector.md#environment-alternative-and-settings) if you already use them. A Hub checkout's `.env` is not automatically a world configuration file.

### 4. Owner-run restart and connection checks

After sync succeeds and configuration is saved, **the world owner** checks active work and chooses a maintenance window. The owner then runs the normal restart for that world; for a CLI-managed gateway:

```text
botnexus --target <world-home> gateway restart
```

Restart interrupts active work. This is a user-operated installation step, **not permission for an assistant, Hub deployment or test to restart the running host world**. Use the world's normal service/container lifecycle if it is not CLI-managed.

After restart:

1. Check the world's extension view/logs for loaded ID `botnexus-hub-connector`. `extensions list` alone proves registration state, not a loaded service.
2. In Hub, confirm the world is online and has a fresh snapshot with only the intended agents. The connector normally sends an initial snapshot and updates every 15 seconds.
3. Choose **Open world** and check that it reaches the correct portal and its normal authentication. Test messages/questions only in a conversation approved for that test. An accepted receipt is not model completion; do not automatically replay an uncertain write.

If the world stays offline, check the selected BotNexus home, sync result, agent IDs, token-file access, environment overrides, TLS and the connector route to ingress `8081`. A login redirect usually means the operator URL was used. Do not print the token or disable TLS verification to diagnose it. A retained offline snapshot is not fresh data.

Repeat with a **separate enrollment and token per world**. See the [full connector guide](docs/connector.md) for all settings, build checks and recovery details.

Never mount a live world's configuration, state, credentials, workspace, tools or container socket into Hub or its tests. Hub needs no access to those files.

The connector exports at most 100 recent conversations and 100 pending questions by default, with a seven-day conversation lookback. The older gateway store contract still reads unbounded metadata before filtering. These output limits do not bound database work on a large world. See [data limits](docs/connector.md#data-limits-and-privacy).

## Development and verification

Use an isolated Python environment, Node.js with its built-in test runner, Git and the .NET 10 SDK. From the Hub checkout:

```text
python -m pip install -r requirements-dev.txt
python -m pytest -q
node --test tests/theme.test.cjs tests/ui-source.test.cjs
dotnet run --project tests/Connector.Tests/Connector.Tests.csproj -c Release -p:BotNexusBinaryDir=/matching/gateway
```

Replace `/matching/gateway` with a complete, isolated assembly directory from the matching gateway build. The connector suite is an executable (`dotnet run`), not a `dotnet test` project. Normal extension installation uses source references through `BotNexusRepoRoot`; [build details](docs/connector.md#build-and-reference-checks) explain both paths.

### Disposable container E2E

The optional `test` profile uses **real, unchanged BotNexus gateways** pinned to `04052bbe283ac9106277a841b8d4c60a5c3ce095`, including their portals and durable `ask_user` flow. The source archive has a checked SHA256 in `tests/world.Dockerfile`. The connected image builds this connector against those exact runtime assemblies.

Only the OpenAI-compatible provider is a deterministic local fixture. No external model or user credentials are needed. This tests gateway integration, not a real model provider's behavior. Test state is separate, and the worlds/provider use an internal test network. Plain WS is enabled only for those deliberately isolated world fixtures; real connectors should use WSS.

Use a dedicated disposable container engine or a fully reviewed isolated stack. Do not point mutating API, browser or recovery tests at a live Hub. The [verification guide](docs/verification.md#disposable-real-gateway-e2e) gives setup commands, file-based test authentication, browser checks and recovery gates. Unit tests, API E2E, browser E2E and recovery tests are separate requirements. This README does not claim that any gate has passed.

## Public release scope

Public releases are source-only. Do not publish gateway redistributions, DLLs, PDBs, build output, databases, real `.env` files, tokens, private proxy settings or test screenshots. Review the public tree and its history; do not mirror private repository history into the public repository. This guide does not select a license.

## Further details

- [Connector installation and configuration](docs/connector.md)
- [Protocol, authentication and command semantics](docs/protocol.md)
- [Deployment, backup and recovery](docs/operations.md)
- [Release verification checklist](docs/verification.md)
- [Changelog](CHANGELOG.md)

Back up the Hub data volume and private deployment settings. A normal redeploy keeps named volumes; never use `compose down -v` on a configured Hub.
