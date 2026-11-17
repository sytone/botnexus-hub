# BotNexusHub protocol

Application version **0.3.0** uses wire protocol **1**. The app is Python/FastAPI with SQLite and a same-origin static UI. The world extension is a .NET 10 hosted service with an outbound WebSocket. Hub accepts only explicit new-conversation, message and prompt-answer commands. No inbound gateway port, gateway admin credential, raw SQL, shell command or platform change is part of this protocol.

Run one Hub backend process. Routing is in memory; extra workers or replicas are not supported. Every externally admitted operator can control all enrolled worlds. Hub is not a multi-tenant identity broker or a replacement for a world's own permissions.

## Operator request boundary

The backend binds to container loopback `127.0.0.1:8785`. It does **not** authenticate operators. The supplied nginx ingress shares its network namespace and enforces two distinct destination listeners:

- **Operator port `8080`:** checks `X-Hub-Proxy-Secret` against `HUB_PROXY_SECRET` before forwarding UI, static assets and operator APIs. `GET /health` is public. Connector routes and removed login/logout routes are blocked.
- **Connector port `8081`:** allows only a GET WebSocket upgrade to `/api/connect/{worldId}`, without a query string. It never proxies UI or operator endpoints, even with a spoofed `Host` or proxy shared header.

Default Compose publishes host-loopback `8785` → operator `8080` and `8788` → connector `8081`. Neither mapping publishes the raw backend. Bypassing the ingress would bypass operator authentication and is not supported.

The external proxy owns identity, SSO, groups, MFA, rate limits and sessions. It must overwrite a caller-supplied `X-Hub-Proxy-Secret` and inject the private value **only after preauthentication succeeds**. The secret belongs only in protected ingress/proxy configuration, never in browser code, cookies, world configuration, URLs or ordinary logs. Protect the proxy's administration/configuration as part of this trust boundary.

There is no native operator token, password, login cookie or logout mechanism. `/api/login` and `/api/logout` are not available. Operators enter through a top-level visit to the external proxy; the proxy manages sign-out and session expiry.

Every operator API **POST, PUT, PATCH or DELETE** requires both:

```text
X-Hub-Request: 1
Origin: https://hub.example.com
```

Use the exact configured `HUB_PUBLIC_URL` origin as `Origin`. The scheme, host and effective port must match; a missing, malformed or cross-origin value is rejected. This applies to preauthenticated browser and non-browser callers alike. Preauthentication does not replace this CSRF check. The proxy must preserve the real Origin, not manufacture one.

Operator mutation bodies are limited to 64 KiB. API responses use `Cache-Control: no-store`. The operator ingress removes Authorization, Cookie and proxy-forwarding headers before forwarding ordinary operator requests; none is a native Hub operator credential.

## Enrollment and connector authentication

`POST /api/worlds` takes exactly `{id,name,baseUrl}`. A world ID matches `[A-Za-z0-9][A-Za-z0-9_.-]{0,63}`. Use a display name of at most 160 characters for connector compatibility. The portal base URL must be HTTP(S), without credentials, query or fragment; a portal path prefix is allowed.

Enrollment returns a new random connector token under `token` and `connectorToken`, two aliases for the same value. Hub stores only its SHA256 hash. Duplicate world IDs return 409. `GET /api/worlds` cannot return or recover a token.

The token is **shown once, but reused** for each connection until explicit rotation or revocation. It is not a one-time-use token. `POST /api/worlds/{worldId}/rotate-token` returns the replacement once, invalidates the old token and disconnects the old socket. `DELETE /api/worlds/{worldId}` revokes enrollment. `POST /api/worlds/{worldId}/disconnect` only disconnects; it does not invalidate the token or prevent reconnect.

Removal does not uninstall the extension or delete world data. It revokes the existing credential; a connector cannot self-enroll, even if it keeps retrying. Re-enrollment is an explicit operator action that generates a new token. Offline enrollments have no automatic expiration.

A world authenticates its WebSocket handshake with:

```text
Authorization: Bearer <world-connector-token>
```

The public connector root is configured separately as `HUB_CONNECTOR_PUBLIC_URL`, for example `https://connect.example.com`. The extension converts it to WSS and appends `/api/connect/{worldId}`. The operator origin is not a connector endpoint. A world token grants neither operator access nor access to another world's socket.

Remote connectors use WSS with normal certificate validation. The provided ingress-to-backend hop is private loopback. A connector does not follow login redirects or use browser cookies. Non-browser connectors normally omit `Origin`; if supplied, the backend also checks it against the WebSocket request origin. Browsers are not supported as world connectors. Duplicate active sockets for a world are rejected rather than creating split-brain routing.

Plain WS is for loopback or explicitly isolated test fixtures, not a workaround for TLS errors. Forwarded protocol/identity headers do not grant trust. The ingress rejects other connector HTTP routes, non-upgrade requests and query strings before the backend.

No ordinary snapshot, command, result, receipt or list response echoes a connector token or the proxy shared secret. Plaintext connector-token responses are limited to explicit enrollment/rotation.

## WebSocket frames

Frames are JSON with camelCase properties. A complete message is limited to **1 MiB**, including fragmented messages. Snapshots require `protocolVersion: 1`; command/result frames may omit it, and version 1 is the only supported value when present. There are no arbitrary remote operations.

### Snapshot: world to Hub

The connector sends an initial snapshot and then normally updates every 15 seconds:

```json
{
  "type": "snapshot",
  "protocolVersion": 1,
  "worldId": "test-a",
  "name": "Workshop Alpha",
  "baseUrl": "https://world.example.com",
  "version": "0.3.0",
  "observedAt": "2030-01-01T00:00:00Z",
  "agents": [
    {"id": "helper", "name": "Helper", "description": "Example agent"}
  ],
  "conversations": [
    {"id": "c_example", "agentId": "helper", "title": "Example", "updatedAt": "2030-01-01T00:00:00Z", "status": "active", "pendingQuestion": null}
  ],
  "questions": [
    {"conversationId": "c_example", "agentId": "helper", "title": "Example", "requestId": "q_example", "prompt": "Which option?", "inputType": "choice_or_free_form", "choices": [{"value": "blue", "label": "Blue"}], "allowMultiple": false, "allowFreeForm": true}
  ]
}
```

The Hub UI uses the latest normalized snapshot `baseUrl` for portal and conversation handoffs, including a retained offline snapshot. Enrollment `baseUrl` is only the fallback when no safe snapshot address is available. The authenticated connector may update its own portal address without rotating the token or re-enrolling. This is compatible with the 0.2.0 connector; the wire protocol is still 1.

Only allowlisted, visible, enabled named agents and active, user-facing, top-level conversations are exported. Defaults are a seven-day conversation activity lookback, at most 100 conversations and at most 100 pending questions. Questions are queried separately from recent conversations, so an older unresolved question can still appear.

Snapshot strings/arrays and encoded output are capped. Snapshots contain routing IDs, titles, timestamps and explicit question presentation fields, not transcripts, tool payloads, system instructions, authentication or configuration. Network content is data, never HTML or instructions for Hub. The UI uses escaped DOM text.

**Store limitation:** `GetSummariesAsync` and `GetPendingAskUserCheckpointsAsync` in the older gateway contract have no limit/lookback argument. The connector reads those metadata sets before filtering/capping output, and checks relevant conversation metadata by ID. Output caps do not bound initial database work on a large world. It does not query full transcripts or session history. See [connector data limits](connector.md#data-limits-and-privacy).

An omitted question is cleared by a later successful snapshot. A failed store read is not converted into a successful empty snapshot. Hub stores receipt-time `lastSeen` separately from the snapshot's `observedAt`. A connection must supply a snapshot before Hub admits new commands; an offline world's retained snapshot is historical data.

### Optional numeric metrics on a snapshot

A metrics-capable connector can add `metrics` to an otherwise unchanged version-one snapshot. Omission/null means no new metrics batch; it is not a zero-valued sample. Older Hubs ignore this field. Hub does not retain it in the dashboard snapshot or turn every history sample into an activity event.

```json
{
  "schemaVersion": 1,
  "bootId": "00000000-0000-4000-8000-000000000001",
  "sequence": 1,
  "observedAt": 1893456000000,
  "intervalSeconds": 30,
  "points": [
    {"metricId": "gateway.memory.rss_bytes", "value": 104857600, "weight": 1},
    {"metricId": "agent.loops.tools", "value": 2.5, "weight": 4, "agentId": "helper", "conversationId": "c_example"}
  ]
}
```

This object is the value of the `metrics` property, not a standalone WebSocket message. `observedAt` is UTC Unix milliseconds. The boot ID identifies a collector lifetime, and the positive sequence increases for its batches. Hub persists a world-scoped high-water mark to suppress replay across Hub restarts. Completed-run deduplication at the connector is bounded to its own lifetime; it is not a durable exactly-once historical export across gateway restarts.

The envelope is capped at 64 KiB and 512 points. Interval is 15–3600 seconds. Values must be finite, nonnegative JSON numbers no larger than 1e18; percentages cannot exceed 100. Booleans, null and numeric strings are not numbers. Weight is a positive integer no larger than 1,000,000. Only the fixed IDs returned by the catalog are accepted. No labels, arbitrary tags, SQL text, file paths or other fields are stored. IDs are bounded routing IDs. Unknown or invalid optional metrics are discarded without breaking a valid snapshot/command connection; malformed whole-frame JSON and the existing 1 MiB frame bound still apply.

World identity is taken from the authenticated connection, never a metrics-supplied world ID. World counters cannot contain agent/conversation dimensions. Agent counters must match allowed agents and an unambiguous owner in the current normalized visible conversation/question set. Run counters are stored per conversation, not again as duplicate agent totals. An agent-only active-loop zero is allowed when that visible agent has no visible conversations. Hidden, nested, non-user-facing and archived conversations cannot supply metric dimensions. These are connector visibility checks, not per-user operator roles.

Samples older than retention or more than five minutes in the future are rejected. Storage has separate cardinality/boot/row bounds. Default retention is seven days; the row cap can make available history shorter. Maintenance expires old rows even without traffic. Explicit world removal purges that enrollment's metrics/choices; token rotation preserves history. See [metrics](metrics.md) for measurement meanings and limitations.

### Commands: Hub to world

Only these three actions are allowed:

```json
{"type":"command","id":"00000000-0000-4000-8000-000000000001","action":"start_conversation","payload":{"agentId":"helper","title":"Example","message":"Hello"}}
{"type":"command","id":"00000000-0000-4000-8000-000000000002","action":"send_message","payload":{"agentId":"helper","conversationId":"c_example","message":"Next message"}}
{"type":"command","id":"00000000-0000-4000-8000-000000000003","action":"answer_question","payload":{"agentId":"helper","conversationId":"c_example","requestId":"q_example","text":"Blue, please","selectedValues":["blue"],"cancelled":false}}
```

These are separate messages, not one JSON array. IDs are non-empty canonical UUIDs. Cancellation uses `answer_question` with `cancelled: true`, rather than a fourth action.

Hub accepts `{action,payload}` from the operator, rejects unsupported fields, and checks targets against the current connection's snapshot. The connector rechecks the explicit agent allowlist, enabled/visible named agent, conversation owner, Active/UserFacing/top-level state and current prompt request ID before mutation. No Hub-specified URL, config, tool or shell operation is honored.

Hub limits message/answer text to 16,384 characters and titles to 200 characters. For interoperability with the .NET connector, selected answers must fit its stricter limit of 20 values, each at most 128 characters. Payload and frame byte limits apply as well.

### Results: world to Hub

Accepted:

```json
{"type":"result","id":"00000000-0000-4000-8000-000000000001","ok":true,"data":{"conversationId":"c_example","status":"accepted"}}
```

Rejected:

```json
{"type":"result","id":"00000000-0000-4000-8000-000000000001","ok":false,"error":"busy"}
```

Errors use fixed safe codes, not exception text, URLs, paths or secret values. Successful result data is restricted to the conversation ID and accepted status. Results are scoped to a command for the connected world.

## HTTP APIs

All routes below except `GET /health` are operator routes behind external preauthentication and the operator ingress. The connector listener does not expose them.

| Method and route | Contract |
| --- | --- |
| `GET /health` | `{status,version,commit}`. Public on the operator ingress; no world data. |
| `GET /api/me` | `{"authenticated":true,"authMode":"external-preauth","connectorUrl":"https://connect.example.com"}`. Deployment status, not a user identity or an independent auth check. |
| `GET /api/worlds` | `{items:[{id,name,baseUrl,connected,lastSeen,snapshot}]}`. No connector tokens. |
| `POST /api/worlds` | `{id,name,baseUrl}` → 201 world metadata plus the newly issued token aliases. |
| `POST /api/worlds/{worldId}/rotate-token` | Explicit replacement token, returned once; old credential/socket invalidated. |
| `POST /api/worlds/{worldId}/disconnect` | Disconnect current socket without revoking its credential. |
| `DELETE /api/worlds/{worldId}` | Revoke enrollment; 204. |
| `GET /api/activity?worldId=&limit=50&before=` | `{items:[{id,worldId,kind,summary,createdAt,conversationId,agentId}],nextCursor}`. Optional world filter; limit 1–200. Use the positive `nextCursor` as `before` for the next page. |
| `POST /api/worlds/{worldId}/commands` | `{action,payload}` → 202 command resource. Requires `Idempotency-Key` in addition to the mutation boundary headers. Offline/not-ready, out-of-scope and full-queue requests return 409. |
| `GET /api/commands/{commandId}` | `{id,status,action,worldId,result,error}`; 404 if unknown. |
| `GET /api/metrics/catalog` | `{items:[{id,name,unit,scope,aggregation,description}],defaultMetrics}`. Fixed catalog; `scope` is `world` or `agent`; aggregation is `mean` or `sum`. |
| `GET /api/metrics/settings` | `{retentionDays,maxRows,sampleIntervalSeconds,rowCount,databaseBytes,oldestAt,newestAt,rejectedBatches}`. Interval is the default, not a promise that every world samples at that rate. |
| `PUT /api/metrics/settings` | Exactly `{retentionDays:7}`; integer 1–365, persisted and immediately pruned on reduction. Returns settings. |
| `GET /api/metrics/dimensions?worldId=` | `{worlds:[ids],agents:[{worldId,agentId}],conversations:[{worldId,agentId,conversationId}],truncated}` from enrolled retained dimensions. |
| `GET /api/metrics/query` | Repeated `metricId` (1–6), optional repeated `worldId`, optional exact `agentId`/`conversationId`, UTC millisecond `from`/`to`, and `maxPoints` (16–720, default 240). Response below. |
| `GET /api/metrics/tiles?worldId=` | Last-hour/60-bucket history: `{from,to,metrics,worlds:{id:{metrics,series}},retentionDays,truncated}`. Default or saved 1–3 world metrics. |
| `PUT /api/worlds/{worldId}/metrics` | Exactly `{metrics:[ids]}`; 1–3 distinct known world-scope IDs. Returns `{worldId,metrics}`. Shared preference, not a per-user browser setting. |

All operator mutations require the headers described above. A Bearer world token or an old native operator credential does not replace preauthentication or CSRF checks.

### Metrics query response and bounds

`query` returns `{from,to,retentionDays,bucketMs,series,truncated}`. Each series is `{metricId,worldId,agentId,conversationId,points,latestAt,sampleIntervalSeconds}`. Series cadence is the maximum declared interval among its selected samples; legacy sample rows default to 30 seconds. Freshness and chart segmentation use that per-series cadence, not the global default. Each point is `{timestamp,value,count}`, with the bucket-start timestamp and total measured weight. `latestAt` is the latest actual observation, not the bucket's start. `from` is inclusive, `to` exclusive; both must be integer epoch milliseconds and `from < to`. Omitted dates select the retained window. Buckets are aligned to the requested `from`.

Means use `SUM(value * weight) / SUM(weight)`; completed-loop interval counts use `SUM(value)`. There is no implicit cross-world or cross-dimension rollup. An agent/conversation filter never falls back to world counters. Unknown filters return no matching series, not all data. Missing buckets are absent, never zero-filled. At most 64 exact series are returned, with at most `maxPoints` points each. Dimensions lists are bounded to 1,000 each; tiles are bounded to 64 worlds. `truncated: true` means partial output; narrow the filters. Storage allows at most 8,192 series (1,024 per world), 8,192 boot high-water marks (128 per world), and the configured sample row cap. New excessive cardinality is rejected instead of evicting live replay protection.

The metrics database is separate from the registry/commands database, in the same default data volume. A failed metrics database returns a fixed 503 for history operations without logging raw storage errors or disabling ordinary world commands. API availability alone does not mean a given world supports collection.

## Delivery and idempotency

`Idempotency-Key` is required for command submission: 1–128 printable non-space ASCII characters. A key is global within Hub, not scoped to a world. The same key and same canonical JSON request/world return the same stored receipt. Reusing the key for a different request returns 409. Fingerprinting covers the actual request body, not only its normalized fields. The browser uses a random UUID for a new action.

Receipt states are:

| State | Meaning |
| --- | --- |
| `queued` | Stored and waiting for the current connector send queue. |
| `sent` | Marked for delivery to the connector. This is not proof of execution. |
| `succeeded` | Connector reported admission/acceptance, **not model completion**. |
| `failed` | Connector reported a known rejection. |
| `uncertain` | Delivery/admission cannot be established, including timeout, disconnect, Hub restart or `outcome_unknown`. |

There are at most 64 pending commands per world. The default command timeout is 30 seconds. Disconnects/restarts mark pending work uncertain rather than replaying it. Terminal receipts and idempotency survive a Hub restart.

The connector keeps a bounded command-ID/fingerprint/result cache for its service lifetime, including reconnects. At its 4,096-entry bound it rejects new commands instead of evicting admitted IDs and allowing silent replay. This is not a durable exactly-once guarantee across gateway restarts. Conversation creation also uses a stable derived conversation ID to avoid blindly re-seeding an existing row.

**Never automatically replay an uncertain mutation.** Read the existing receipt and inspect the target world before choosing a fresh action/key. A successful question answer is removed from Hub only after a later successful snapshot. Access recovery and page reload do not retry writes.

## Portal handoff and persistence

Deep links use the configured portal `baseUrl` plus `/agent/{agentId}/conversation/{conversationId}`, matching the desktop/mobile portal routes at pinned platform commit `04052bbe283ac9106277a841b8d4c60a5c3ce095`. IDs are encoded and unsafe or credential-bearing URL schemes are rejected. The world's own sign-in still applies; no connector token or proxy secret belongs in a portal URL.

The private Hub data volume contains registry data, hashed connector credentials, snapshots, submitted command payloads, receipts/idempotency and capped activity. A separate metrics SQLite file in that same volume holds bounded numeric history, routing IDs and shared chart/retention settings. Messages and answers submitted through Hub are sensitive stored data, even though full transcripts are not copied. Command payloads/receipts have no automatic pruning in this version. Deployment secrets and gateway token files stay outside source control and need a private backup/rotation plan. See [operations](operations.md).
