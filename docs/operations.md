# Operations and recovery

## Isolation boundary

Hub is independent software. It never mounts a world's database, configuration, tokens, workspace, container socket or host root. World connectors run inside explicitly enrolled worlds using supported gateway services. Installing in a real world is a separate owner-operated action with a planned restart. Do not change or restart the running host world during Hub setup, deployment or tests.

Use one Hub instance with one backend process. Connections and queues are in memory; multiple workers or replicas are not supported. All operators admitted by the external policy have the same Hub access to all enrolled worlds. Hub does not implement per-user roles, world permissions or a per-human audit identity.

## External preauthentication

Version 0.2.0 has no native operator password/token, login API, session cookie or logout API. The external reverse proxy owns identity, SSO, group restrictions, MFA, session lifetime and rate limits. A private shared header connects that policy to the operator ingress; it is not a user credential.

### Listener contract

The backend binds to **container loopback `127.0.0.1:8785`**. nginx shares that network namespace and provides two separate destination listeners:

| Listener | Accepted traffic | Default host mapping |
| --- | --- | --- |
| Operator `8080` | UI/static/operator API requests with `X-Hub-Proxy-Secret` exactly matching `.env`'s `HUB_PROXY_SECRET`. `GET /health` is public. `/api/connect` routes and removed login/logout routes are blocked. | `127.0.0.1:8785:8080` |
| Connector `8081` | GET WebSocket upgrade to `/api/connect/{worldId}` only, with no query string. The backend requires a valid per-world Bearer token. Other HTTP, UI, health and operator routes are not proxied. | `127.0.0.1:8788:8081` |
| Private backend `127.0.0.1:8785` | Internal app traffic from the ingress only. The app does not authenticate operators itself. | Not published |

The host's port `8785` is the guarded operator ingress, not the backend's port with the same number. Do not publish the backend, change it to a public bind address, or route an external proxy straight to it. Do not use a caller-controlled `Host` header to choose between these authentication boundaries.

### Required proxy behavior

Use separate HTTPS origins, for example:

- `HUB_PUBLIC_URL=https://hub.example.com` for browsers/operators.
- `HUB_CONNECTOR_PUBLIC_URL=https://connect.example.com` for world connectors.

These are example addresses. Replace them with origins you control. Origins have no path, query, fragment or credentials. HTTPS is required outside loopback tests.

Configure the proxy as follows:

1. Require external sign-in and the intended access/group policy on the operator origin. Apply MFA and rate limits there as required by your deployment.
2. Reject unauthorized requests **before forwarding them**. Do not forward a caller's private header while authentication is pending or denied.
3. Strip/overwrite every caller-supplied `X-Hub-Proxy-Secret`. Only after access is approved, inject the shared value on the upstream request to operator port `8080`.
4. Never include that secret in a response, browser script, cookie, URL, redirect, access log or error page. Do not give it to world connectors. It is held only by the ingress and the trusted proxy configuration.
5. Protect the proxy's configuration, administration, header rules and upstream network path. An administrator who can change these can bypass the operator access policy. Keep direct access to guarded listeners restricted as well.
6. Route the connector origin only to port `8081`. Forward WebSocket upgrades and preserve its `Authorization: Bearer` header. Do not put an interactive sign-in redirect or browser-cookie requirement on this route. Apply appropriate network/rate limits without removing per-world authentication.
7. Preserve the browser's real `Origin` on operator mutations. Do not invent a trusted Origin to make failed requests pass. Every API POST/PUT/PATCH/DELETE also requires `X-Hub-Request: 1` and the configured operator origin, independently of preauthentication.
8. Verify both destinations directly and through the external proxy. A successful health response is not proof of protected UI/API access, TLS, group restrictions or a working connector route.

The operator ingress strips Authorization, Cookie and proxy-forwarding headers before passing ordinary operator requests to the app. It checks the proxy shared header at the ingress and then strips that too. The connector ingress does not treat the shared header, cookies or identity/group headers as connector authentication.

`GET /api/me` reports `authenticated: true`, `authMode: "external-preauth"` and the configured `connectorUrl`. This is deployment status, **not an independent identity check**. It is safe only behind the required ingress and external policy.

Open the operator URL as a top-level browser page to complete external sign-in. Hub's **Reload secure access** action returns through that entry point and never resends a mutation. Sign-out and session revocation belong to the external provider; Hub does not pretend to end that session locally.

Any preauth provider that meets this contract can be used. NetBird integration is optional; Hub core is not tied to it or to a particular identity provider. A private mesh alone does not replace the operator preauth policy and private-header check.

## Deploy

Use a clean canonical `main` checkout at the reviewed release commit and a dedicated data volume. Keep development worktrees and test stacks separate. The default Compose file has fixed project/container names; changing only a project name does not isolate all those resources.

1. Review the source revision, dependencies, ingress and external proxy configuration. Use Python 3.12 and Docker/Compose or a compatible Podman/Compose setup.
2. For a new installation, run `python scripts/configure.py`. It creates `.env`, generates only `HUB_PROXY_SECRET`, and refuses to replace an existing file. It does not generate an operator password or session secret.
3. Privately edit the generated settings below. Use owner-only file permissions or a private Windows ACL; protect backups of this file too.
4. Build and start the Hub and ingress from the reviewed checkout:

   ```text
   docker compose up -d --build hub ingress
   docker compose ps hub ingress
   ```

5. Check `http://127.0.0.1:8785/health` for the expected version/commit. An unauthenticated request to the local root should be denied. No local dashboard or native Hub login is expected.
6. Configure both external HTTPS routes and the proxy header as described above. Confirm unauthorized/authorized behavior with the [verification checklist](verification.md#ingress-and-access-policy-gates) before enrolling real worlds.
7. Enter through external sign-in, enroll worlds and use their individual connector credentials. Installation and restart of a real world stay with that world's owner.

### Deployment settings

| Setting | Meaning |
| --- | --- |
| `HUB_HOST_PORT` | Guarded operator host-loopback port; default `8785`. |
| `HUB_CONNECTOR_HOST_PORT` | Guarded connector host-loopback port; default `8788`. |
| `HUB_PUBLIC_URL` | Required external operator origin. Also the required Origin for operator mutations. |
| `HUB_CONNECTOR_PUBLIC_URL` | Required separate connector origin. Returned as `/api/me`'s `connectorUrl`. |
| `HUB_PROXY_SECRET` | Required generated URL-safe shared value, 43–128 characters, used by ingress and the trusted proxy only. |
| `HUB_COMMIT` | Reviewed source revision reported with `VERSION` by `/health`; do not leave `unknown` as release evidence. |
| `NB_MANAGEMENT_URL`, `NB_SETUP_KEY` | Optional NetBird profile settings; not needed by Hub core. |

The backend uses `/data/hub.sqlite` in the Compose data volume. App-level tuning, if needed, must be set in the backend's actual service environment; adding an arbitrary variable to Compose's `.env` does not automatically pass it to a container.

The standard Compose procedure is portable and does not require a private deployment wrapper, a personal host path or a pre-existing fleet setup. If you use `scripts/deploy.ps1`, review its supported engine/options and ownership checks in the selected revision. It must operate only on this stack and retain volumes.

For a network-namespace replacement, account for the sharing `ingress` container and optional `netbird` container. Some engines cannot replace the Hub while a sidecar still holds its namespace. Validate this stack's labels and mounts, stop/recreate only its dependent sidecars as needed, and retain every persistent volume. Do not remove unrelated containers or identities. Check both ingress listeners again after any recreation.

## Upgrade from 0.1.0

This changes operator authentication. The former native login/token/cookie flow no longer grants access. Do not publish the new backend while assuming the old login still protects it.

1. Schedule downtime for **this Hub only**. Review the target revision and prepare the two external routes and preauth policy. Do not restart a live BotNexus world as part of this upgrade.
2. Take a consistent backup of Hub data and private deployment settings. Preserve the world registry and connector-token hashes.
3. Preserve the existing `.env`; do not delete it to rerun the configurator. Add `HUB_PUBLIC_URL`, `HUB_CONNECTOR_PUBLIC_URL` and a fresh `HUB_PROXY_SECRET` privately. Use a secure generator for a 43–128 character URL-safe value. Alternatively, run `configure.py --root <empty-private-directory>` and privately transfer the new settings into the preserved deployment file.
4. Remove obsolete native operator-token/session-secret settings from the deployment. Set `HUB_COMMIT` to the reviewed new revision. Store the new shared header only in the ingress and external proxy configuration.
5. Replace the old deployment with the single loopback backend and both guarded ingress listeners. Preserve the data volume. Do not retain an old raw-backend port mapping as a fallback.
6. Verify external sign-in, denied anonymous access, mutation Origin checks and the separate connector route. Old native Hub sessions are no longer credentials.
7. Change each real connector's `url` to the new connector origin when required. Its owner plans any configuration change/restart. Existing per-world tokens remain usable unless explicitly rotated or revoked; migration is not a reason to re-enroll every world.

If rollback is needed, restore the reviewed application/configuration and a compatible consistent backup behind a valid access boundary. Do not use unauthenticated backend exposure as a recovery method.

## Optional NetBird profile

The `netbird` profile adds a separate peer sidecar. It is disabled by default and does not configure operator authentication for you. Other networking and preauth products remain supported through the generic listener/header contract.

Set your own `NB_MANAGEMENT_URL` in the private `.env` before enabling this profile. For first enrollment, supply a dedicated setup key there; do not put a real key in an example or a command argument.

```text
docker compose --profile netbird up -d hub ingress netbird
```

The sidecar stores its peer identity in its own volume. Its start script reconnects an existing identity without a setup key. After a successful first enrollment, remove the enrollment key from deployment settings. Verify identity persistence and routing in your environment; this guide does not claim that those checks have passed.

If that identity is intentionally lost, enroll a new dedicated peer. Never reuse another application's identity. Guard the operator listener even when it is reachable only over a mesh, and protect any proxy configuration/admin UI used to expose it.

## Persistent data and backups

| Data | Default storage | Backup treatment |
| --- | --- | --- |
| Hub database | `/data/hub.sqlite` in `botnexushub_hub-data` | Include. Registry, token hashes, snapshots, submitted command payloads/receipts and activity. |
| Deployment settings | Untracked `.env` and protected external proxy settings | Include in a private settings backup or maintain a documented secure recreation plan. Never publish. |
| World connector token files | Chosen by each world owner | Protect and back up under that world's policy, or plan explicit per-world rotation. Hub cannot recover plaintext tokens. |
| Optional mesh identity | `/var/lib/netbird` in `botnexushub_netbird-state` | Back up privately or record an intentional re-enrollment plan. Do not delete routinely. |
| Synthetic test state | `botnexushub_world-a-state`, `botnexushub_world-b-state` | Disposable test data only; exclude with an explicit reason. Never apply this rule to a real world. |

Names above are Compose defaults, not instructions to operate on an existing container by name alone. Confirm project labels, mounts and ownership before backup, restore or replacement.

SQLite uses WAL mode. Use a consistent SQLite backup method, or stop only this Hub before copying its full data directory. Copying an active database file alone can miss WAL data. Verify a restore in a separate disposable environment.

Snapshots contain conversation titles and question text/choices. Command storage also contains submitted messages and answers. These are sensitive even though Hub does not copy full transcripts. Activity is capped, but command receipts/payloads do not have automatic pruning in this version; plan for storage growth and private retention.

A normal redeploy never deletes volumes. Never run `compose down -v` on a configured Hub.

## Delivery semantics

`accepted` means the world admitted the command, not that the model finished. Hub stores stable command IDs and client idempotency keys. A lost connection, restart or timeout can yield `uncertain`. Inspect the world before submitting a fresh command. Do not automatically replay an uncertain write.

A completed question disappears after a later successful snapshot. A failed store read must not be reported as a successful empty snapshot. An offline world's retained snapshot is historical data; check both connection state and observation time.

## Recovery

1. Stop only the affected Hub stack and preserve its data/settings. Confirm names, project labels and volumes before any container action.
2. Restore a consistent Hub backup and the matching private ingress/proxy settings. Recreate only this stack's containers, including the guarded ingress. Keep the raw backend private.
3. Verify `/health`, denied anonymous operator access, authorized access through external sign-in and both listener rules. External sign-in/session recovery belongs to the preauth provider.
4. Allow the worlds to reconnect. Restored connector-token hashes work with worlds that still hold those original tokens. Check whether a restore rolled back any later rotation/revocation; rotate affected credentials explicitly if needed.
5. If one token is lost or compromised, rotate only that world's token and give the replacement to its owner. Rotation disconnects its old socket. Deleting a world revokes its credential. A simple **disconnect** does not revoke a token; the connector may reconnect.
6. Inspect uncertain commands in the world before any new write. Hub restarts preserve terminal receipts/idempotency and mark pending commands uncertain; they are not replayed.

Do not re-enroll all worlds, reset the database or expose the private backend to fix one failed connector.

## Disposable test profile

The `test` profile runs two real BotNexus gateways with separate synthetic volumes. Their provider is a deterministic local OpenAI-compatible fixture, not an external paid service. The worlds and provider stay on an internal network; their test-only portal proxy publishes loopback ports `8786` and `8787`. No host-world state, provider credentials or container socket is mounted.

The base image is built from unchanged BotNexus commit `04052bbe283ac9106277a841b8d4c60a5c3ce095` with an archive SHA256 check. The connected image builds the connector against those exact assemblies. These are **new isolated test images**, not updates to the installed/running BotNexus codebase. Keep images and generated assemblies local; the public release is source-only.

See [verification](verification.md#disposable-real-gateway-e2e) for the explicit opt-in, test-only secret file, browser coverage and configurable recovery targets. Do not launch the fixed-name profile beside a managed live deployment on the same engine.

## Troubleshooting without secrets

| Symptom | Check |
| --- | --- |
| Local root returns 403 | Expected without the trusted proxy header. Use the external operator entry point; do not disable ingress. |
| Operator mutation returns 403 | Check the external policy, upstream header rule, `X-Hub-Request: 1` and exact configured operator Origin. Do not fabricate Origin at the proxy. |
| Connector `/` or `/health` returns 404 | Expected. The connector listener exposes only the WebSocket route. |
| Connector receives an interactive login page | It is probably using an operator route. Check the separate connector origin and destination port. |
| WebSocket is rejected | Check the world ID, secret-file readability, token rotation, duplicate active connection, TLS and upgrade routing. Do not print the token or disable TLS validation. |
| Command is uncertain | Inspect the target world and existing receipt. Do not submit a new idempotency key as an automatic retry. |
| Data survives but no world is online | Verify ingress, connector URL and world-owned configuration. A retained snapshot is not proof of a new connection. |

These instructions describe the required design and checks. They do not certify a particular deployment as secure or production-ready.
