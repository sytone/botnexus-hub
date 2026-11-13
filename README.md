# BotNexusHub

**Version 0.2.0** · [Source repository](https://github.com/sytone/botnexus-hub) · [Install a world connector](docs/connector.md)

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

## Connect a world

1. Enter Hub through your external sign-in service, choose **Add world**, and use a stable ID such as `workshop`.
2. Save the connector token privately when it is shown. It is **shown once, but reusable** for reconnects until explicit rotation or revocation. It is not a one-time-use token.
3. Follow the [connector guide](docs/connector.md) to register `https://github.com/sytone/botnexus-hub.git` at a reviewed pinned ref, sync it and configure `gateway.extensions.botnexus-hub-connector` with an explicit `allowedAgents` list.
4. Use the **connector origin**, not the operator origin, for the extension's `url`. Hub supplies it as `connectorUrl` from `/api/me`.
5. The world owner schedules and performs any gateway restart. Hub setup and tests must never restart the running host world.
6. Check that the world is online with a current snapshot. Use **Open world** to verify its portal URL and normal world authentication. Test writes only in a world approved for that test.

Never mount a live world's configuration, state, credentials, workspace, tools or container socket into Hub or its tests. Hub needs no gateway admin key and opens no inbound port on a world.

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
