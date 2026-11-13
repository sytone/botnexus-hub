# Release verification checklist

This is the gate checklist for application version **0.2.0**, not a report of passed runs. It does not certify a deployment as secure or production-ready. Record actual commands, exit codes and results against the exact reviewed source revision and `VERSION`. Do not copy counts or deployment claims from an earlier release.

All app changes require Python tests, Node UI/theme tests, connector build/tests and isolated container/browser E2E before release. Recovery and ingress/access-policy checks below are separate gates, not substitutes for unit tests.

## Isolation and evidence

- [ ] Use a development worktree for changes and a separate, explicitly disposable environment for mutating/restart tests.
- [ ] Do not change, load or restart the running host world's config, credentials, database, code or process. Never mount that world's state, tools or container socket into a test.
- [ ] Verify container-engine context, stack labels, mounts, names, ports and volumes before starting or restarting anything. The default Compose names are fixed; changing only the project name is not complete isolation.
- [ ] Use synthetic-only worlds `test-a`/`test-b`, allowlisted agent `helper` and the local deterministic provider. Refuse a registry containing other worlds.
- [ ] Use generated test-only ingress/connector credentials. Pass authentication via protected files, never token/header values on the command line.
- [ ] Record the source commit, version, environment, commands, exit codes, failed/skipped gates and remaining limits. Keep logs free of credentials and private identifiers.

## Python, UI and connector gates

Use an isolated Python environment, Node.js, Git and the .NET 10 SDK:

```text
python -m pip install -r requirements-dev.txt
python -m pytest -q
node --test tests/theme.test.cjs tests/ui-source.test.cjs
dotnet build src/BotNexus.Extensions.HubConnector/BotNexus.Extensions.HubConnector.csproj -c Release -p:BotNexusRepoRoot=/isolated/botnexus
dotnet run --project tests/Connector.Tests/Connector.Tests.csproj -c Release -p:BotNexusBinaryDir=/matching/gateway
git diff --check
```

Replace the example paths with isolated matching source/assembly trees, never the installed running core. The connector suite is an executable, not a `dotnet test` project. Missing/mismatched references are failures; do not combine assemblies from different revisions or disable the validation gate. See [connector build details](connector.md#build-and-reference-checks).

- [ ] Python: validation, state, routing, same-origin mutations, token isolation, idempotency and uncertain delivery.
- [ ] Node: system/light/dark modes, safe DOM/link handling, hostile storage, external-access failure handling and no native login/logout UI.
- [ ] Source-reference connector build against the compatible isolated platform checkout.
- [ ] Connector fake/transport/contract tests against complete matching runtime assemblies, including allowlist/scope checks, framing, reconnects and prompt resolution.
- [ ] Whitespace/diff review. No unrelated source or artifact changes are part of a documentation-only update.

## Ingress and access-policy gates

Test the two **destination listeners**, not only hostnames. The backend must remain container-loopback-only with one process. Default published ports must reach operator `8080` and connector `8081`, never raw backend `8785`.

### Operator listener

- [ ] `GET /health` works without a proxy shared header and exposes no world data. Version/commit match the reviewed deployment; `unknown` is not release evidence.
- [ ] Anonymous/wrong-header requests cannot read the root, static assets or operator APIs. A bare local root request is denied; no unauthenticated dashboard is expected.
- [ ] Correct ingress admission permits `/api/me`, which returns `authMode: "external-preauth"` and the separately configured `connectorUrl`.
- [ ] The app/ingress has no native operator token/login/cookie/logout flow. Removed login/logout routes do not restore access.
- [ ] Every API mutation rejects a missing/wrong `X-Hub-Request`, missing/malformed Origin or Origin other than `HUB_PUBLIC_URL`, even after valid preauthentication.
- [ ] A correct same-origin mutation passes its boundary checks. Invalid payloads and missing/changed command idempotency keys still fail as specified.
- [ ] Connector routes are blocked on the operator listener. Changing `Host` or sending a world Bearer token does not change listener policy.

### Connector listener

- [ ] Only GET WebSocket upgrades to a valid `/api/connect/{worldId}` route, without query strings, can reach the backend.
- [ ] Root, static assets, health and every operator API are unavailable through the connector listener, including with a spoofed operator Host or valid proxy shared header.
- [ ] Missing/wrong/revoked world tokens fail. A token for one world cannot connect as another. Duplicate active connections are rejected.
- [ ] Rotation invalidates the old token/socket. The newly displayed token can reconnect repeatedly until the next rotation/revocation; it is not single-use.
- [ ] Tokens/shared secrets do not appear in ordinary snapshots, results, world lists, receipts, bundles, URLs or logs.
- [ ] Oversize/malformed frames, unsupported operations and cross-world results are rejected. New work requires a snapshot from the current connection.

### Real external proxy policy

A direct test using an ingress credential does **not** prove SSO, MFA, groups, rate limits, HTTPS or proxy administration controls. Check the real provider separately, without placing the ingress secret in a normal browser:

- [ ] Top-level navigation goes through external sign-in. Unauthorized accounts/groups are denied before any upstream request is admitted.
- [ ] Caller-supplied `X-Hub-Proxy-Secret` is overwritten/removed. The correct private header is injected only after policy approval and cannot be read in browser responses.
- [ ] Proxy configuration/admin access is restricted; there is no alternate route that skips preauthentication or reaches the raw backend.
- [ ] Operator HTTPS and connector WSS have normal certificate validation. WebSocket upgrades and per-world Authorization survive the connector route without an interactive login redirect.
- [ ] Session expiry, denial, HTML login responses and network errors clear private Hub display state. **Reload secure access** performs top-level access recovery without retrying a mutation. Sign-out remains with the external provider.
- [ ] Rate limits and group/MFA policy are tested at the proxy, not inferred from Hub's `/api/me` response.

## Disposable real-gateway E2E

The `test` Compose profile uses two real, unchanged BotNexus gateways pinned to `04052bbe283ac9106277a841b8d4c60a5c3ce095`. `tests/world.Dockerfile` verifies the source archive SHA256 and builds a new isolated image. `tests/connected-world.Dockerfile` builds this connector against those exact assemblies. This does not rebuild/update the installed running BotNexus codebase.

Only the OpenAI-compatible provider is a deterministic local fixture. The profile exercises the real portal, conversation queue and durable `ask_user` flow, not an external model provider. State volumes are separate, and the gateways/provider use an internal network. Their explicit insecure-transport option is test-only.

### Prepare the test stack

Use a dedicated disposable container engine or a fully reviewed stack override with isolated names, networks, volumes and published ports. Do not launch these fixed-name defaults beside a live deployment on the same engine.

For a new test-only checkout/settings file, run `python scripts/configure.py` once. It refuses to overwrite an existing `.env`. Privately set these **test-only** origins in that file:

```dotenv
HUB_PUBLIC_URL=http://127.0.0.1:8785
HUB_CONNECTOR_PUBLIC_URL=http://127.0.0.1:8788
```

Retain the default test host ports, and set `HUB_COMMIT` to the reviewed source revision. Do not borrow a real deployment's `.env` or secrets. The fixture world containers already opt in to plain WS on their isolated network; do not weaken real connector settings or TLS validation.

Privately create `.secrets/disposable-proxy.secret` containing only this test stack's generated `HUB_PROXY_SECRET`, with owner-only permissions/private ACL. The filename, not the value, is used below. Do not print or put the value in a command argument.

```text
docker build -f tests/world.Dockerfile -t localhost/botnexushub-world-base:04052bbe .
docker compose --profile test build world-a
docker compose --profile test up -d --build hub ingress
```

`world-b` uses the image built for `world-a`. Wait for the loopback operator `/health` to return the expected version/commit, and verify denied anonymous root access before enrollment.

### Enroll, then run ingress/API/browser checks

Check each reviewed helper's `--help` before use. The external-preauth harness contract is `--base-url` plus `--proxy-secret-file`; helpers must not depend on former native operator credentials. Do not use an older helper that still requires native login. The base URL used for these direct ingress tests must match the test `HUB_PUBLIC_URL` so mutation Origin checks remain active.

```text
python scripts/enroll-test-worlds.py --env-file .env --base-url http://127.0.0.1:8785 --proxy-secret-file .secrets/disposable-proxy.secret
docker compose --profile test up -d mock-provider world-a world-b test-portals
docker compose ps
python tests/ingress_e2e.py --allow-disposable --base-url http://127.0.0.1:8785 --connector-url http://127.0.0.1:8788 --proxy-secret-file .secrets/disposable-proxy.secret
python tests/e2e.py --allow-disposable --base-url http://127.0.0.1:8785 --proxy-secret-file .secrets/disposable-proxy.secret
python tests/browser_e2e.py --allow-disposable --base-url http://127.0.0.1:8785 --proxy-secret-file .secrets/disposable-proxy.secret --output test-results/browser
```

The enroller saves synthetic connector tokens in the private test `.env` without printing them. Existing enrollment with a missing local token needs explicit recovery/rotation, not silent replacement. Wait for both worlds to connect and provide snapshots before mutating tests. The ingress suite temporarily enrolls its own synthetic probe world and must remove it before the other suites run.

The browser suite needs Playwright and the browser selected by the reviewed harness. Its disposable browser context may use only the **test-only** ingress secret, restricted to the test origin and without forwarding it through redirects or portal handoffs. This is test infrastructure, not a browser authentication design for deployment. Keep generated screenshots local in ignored test output; do not include them in the public release.

- [ ] Both `test-a` and `test-b` connect with only `helper` visible.
- [ ] Start conversations, send messages, deduplicate a repeated request, receive a real durable question, answer it and observe activity.
- [ ] Browser: no native login/token/logout controls; no mutation on page load or secure-access recovery.
- [ ] Browser: new conversation, message, question answer/cancel, portal handoff in a separate credential-free context, offline/freshness display and no uncaught page errors.
- [ ] Browser: light/dark/system at desktop/mobile widths, safe text/links and no stored credentials.
- [ ] Access denial/redirect/HTML/network failures hide private content and clear transient enrollment credentials without resending writes.

The test portals use `http://localhost:8786` and `http://localhost:8787`. Those are synthetic-only loopback endpoints, not production access instructions.

### Recovery gate

Inspect the reviewed recovery helper before running it:

```text
python tests/recovery_e2e.py --help
```

Pass `--hub-container`, `--ingress-container`, `--world-container`, `--world-b-base-url` and `--project` for the **disposable** Hub/world-b stack; select `--engine docker` or `--engine podman` if needed. Use the actual isolated names and loopback portal URL from that run, not names copied from a live installation. The approved named volumes must match the selected project's `hub-data` and `world-b-state` volumes. Confirm labels and expected volumes before permitting a restart. Never rely on a familiar container name as proof of ownership, and never let this test select the running host world. Like the other helpers, it must use `--base-url` and `--proxy-secret-file`, not authentication values on the command line.

- [ ] A pending question survives restart of only the selected synthetic gateway and remains resolvable by its original request ID.
- [ ] Answering it resumes the actual synthetic agent; confirm its reply in that world's portal/history.
- [ ] Restarting only the selected disposable Hub preserves registry, terminal command receipts and idempotency, and marks pending work uncertain rather than replaying it.
- [ ] Both worlds reconnect through the connector ingress after recovery. Recheck the namespace-sharing ingress if the engine requires recreation.
- [ ] A consistent backup restores in a separate disposable environment; no configured Hub volume is deleted as a test shortcut.
- [ ] If the optional NetBird profile is included, use a dedicated test identity and configured management URL. Verify reconnect without an enrollment key and retention of that identity across sidecar recreation. Do not claim this gate for deployments that did not run it.

## Public source-only release gate

- [ ] Check the full candidate tree and relevant history for secrets, real deployment domains, private Git remotes, personal usernames, user-profile paths, internal addresses, proxy admin details and private infrastructure references. Documentation examples use `example.com` and public source URLs only.
- [ ] Publish only reviewed source/docs/manifests/fixtures. Exclude DLLs, PDBs, gateway redistributions, `bin`/`obj`, artifacts, databases, `data`, real `.env` files, token files, screenshots and browser profiles.
- [ ] Do not push or mirror private repository history into the public repository. Prepare a clean reviewed public history instead.
- [ ] Confirm install examples use `https://github.com/sytone/botnexus-hub.git` and a reviewed pinned Hub ref, not a private mirror or an unreviewed moving branch.
- [ ] State the licensing status explicitly. This source publication does not grant an open-source license; only the maintainers may select one. Do not copy a dependency or gateway license onto this project.
- [ ] Record actual gate results for this revision. Any failed, skipped or unrun required gate remains open; a documentation edit is not evidence that an application test passed.

## Limits to keep in the release record

One Hub backend process; all externally admitted operators have equivalent access. No full transcript view, per-user Hub roles or native sign-out. Real-world installation needs the owner's planned gateway restart. Large worlds still read unbounded summary/checkpoint metadata before connector output caps, and command receipt/payload storage has no automatic pruning. The deterministic provider does not validate real external model behavior. Proxy access policy and security claims need their own observed evidence.
