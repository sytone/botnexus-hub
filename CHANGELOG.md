# Changelog

## 0.2.1

- Correct world, conversation and question handoff links to use the portal address in the latest validated connector snapshot. Keep the enrollment URL as a safe fallback and retain the last snapshot address while offline. Existing 0.2.0 connectors work without a protocol change or rebuild.
- Add **Remove world** to each world card with exact-ID confirmation, cancel, enrollment-only revocation and no automatic retry. Handle empty 204 responses and reject stale in-flight world reads after removal.
- Document portal corrections, owner-run configuration/restart, explicit re-enrollment with a new token, and the difference between offline, disconnect and removal. Offline worlds do not expire.

## Documentation updates

- Rework the README into a getting-started guide with a contents list: overview, container setup, existing-world extension installation, world registration/connection, and development/issues. Add copyable PowerShell path and settings examples, separate the three service/portal URLs, and keep safe update, token and owner-run restart guidance. Documentation only; app/connector version remains 0.2.1.
- Default extension installation and updates to `--ref main` for latest source. Explain the optional published-tag track without claiming that the displayed application version is already a Git tag.
- Add CLI configuration commands with an explicit target home, extension-only object replacement, secret-file references, private readback checks and update rules. Align the connector and verification guides. No app/connector code or version change.
- Add a complete existing-world extension install walkthrough to the README: prerequisites, source registration, sync, protected token-file configuration, explicit agent allowlist, owner-run restart, connection checks, and offline troubleshooting. Application and connector versions remain 0.2.0.

## 0.2.0

- Replace native operator authentication with external preauthentication. The reverse proxy owns identity, SSO, groups, MFA, rate limits and sessions; Hub no longer provides an operator token, login cookie or logout flow.
- Keep one backend process on container loopback and add separate guarded nginx listeners: operator `8080` with a private proxy-injected header, and connector `8081` for per-world authenticated WebSockets only. Default published ports reach those listeners, not the raw backend.
- Require `X-Hub-Request: 1` and the configured operator Origin for every operator API mutation, independently of preauthentication.
- Add separate operator and connector public origins. `/api/me` reports `authMode: external-preauth` and `connectorUrl`; enrollment snippets use the connector origin. External access recovery uses top-level navigation and never retries a mutation.
- Generate only the proxy shared secret in deployment setup. Keep NetBird as an optional profile with an explicitly configured management URL; Hub core remains provider-agnostic.
- Clarify that connector tokens are shown once but remain reusable until rotation/revocation. Ordinary snapshots, frames and list/receipt responses do not echo them.
- Document public source installation from `https://github.com/sytone/botnexus-hub.git`, pinned-ref review, source/binary reference checks, owner-operated world restarts, migration and private backups.
- Set a source-only public release boundary and replace historical verification counts with a gate checklist. Test outcomes must be recorded for the reviewed revision, not inferred from this changelog.

## 0.1.0

- Initial single-operator Hub with persistent world enrollment, status, agents, recent conversations, activity and pending questions.
- Outbound .NET world extension with an explicit agent allowlist, scoped commands, durable question resolution and reconnect backoff.
- Start conversations, send messages, answer or cancel prompts, and hand off to each world's own portal.
- Connector credentials displayed once, native operator cookie/token authentication at that version, idempotency receipts and honest uncertain delivery. Native operator authentication was replaced in 0.2.0.
- Responsive system/light/dark UI and isolated real-gateway Alpha/Beta test worlds with a deterministic local provider.
- Container deployment, backup boundaries, installation and recovery documentation.
