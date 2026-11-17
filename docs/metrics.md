# Metrics and history

Hub 0.3.0 adds a separate numeric metrics store and charts for enrolled worlds. Metrics start when a metrics-capable connector runs; Hub does not reconstruct history from transcripts or invent samples for downtime.

## Use the charts

- A world card shows **one to three** compact resource graphs for the last hour. Use its metric settings to choose the counters. Choices are shared Hub settings for that world.
- Click a graph to open the full history for that world. Choose **Metrics** in the header to compare all worlds.
- Add or remove metrics in the explorer, then narrow the world, agent, conversation and date/time range. Separate charts keep different units readable; separate series keep worlds and conversations distinct.
- The full-history view uses the retained period by default, initially seven days. A new connector may have only a few minutes of data. Check the first and last sample times.
- Blank means **not available**, not zero. CPU and I/O rates need two observations. Gaps and stale samples are not live readings. Each returned series carries the slowest declared sampling interval among its selected observations, so mixed-cadence worlds do not use a fixed 30-second freshness threshold. Changing cadence within a range uses that conservative maximum.
- Agent/conversation filters apply only to counters collected with those IDs. A machine or gateway CPU value cannot be assigned to one agent, so it is absent under that filter rather than repeated as if it were per-agent usage.

Dates are stored as UTC epoch milliseconds. Browser date/time controls and chart labels use the browser's local timezone. Downsampled chart points summarize their bucket; they are not extra measurements. Different worlds are compared, not added into a misleading machine-wide total.

## What is collected

Collection defaults to **30 seconds**. The connector runs inside the gateway, reads supported operating-system counters and safe gateway metadata, and sends a small optional payload over its existing outbound authenticated connection.

| Group | Counters | Meaning and limits |
| --- | --- | --- |
| Machine | CPU, total/used memory, memory percentage, application-filesystem capacity/free/used percentage | The OS view available to the process. A container's machine view may be the container host or VM, not the physical computer. Disk capacity is not the size of the world's files. |
| Container, when supported | Cgroup CPU and memory usage/limit | Kept separate from machine counters. Missing or unsupported cgroup information is not replaced with a host measurement. |
| Gateway process | CPU, resident/private memory, threads, uptime, process I/O rates | The gateway process only. CPU percentage is normalized to the available processor count. I/O is an operating-system observation, not SQL query throughput. |
| .NET | Managed heap and GC pause percentage | GC values use the runtime's observation window; heap size is not the same as resident process memory. |
| Visible activity | Active loops, visible conversations and pending questions | Restricted to the connector's explicit agent allowlist and visible, top-level conversations. Active loops are an instantaneous sample, not a count of all work that ran between samples. |
| Completed loops | Completed run count, mean duration and mean completed tool results per loop | Uses the optional payload-free agent-run evidence capability. Zero-tool measured runs count; unmeasured tool counts do not become zero. Counts are not success-only. Means are weighted by measured runs, not by the number of sample rows. |
| SQLite | Observed logical connections, open/close transitions, allocator current/peak bytes | Passive, process-local observations when the gateway exposes them. These exclude other processes and idle native pooled connections. They do not prove integrity, locking health or query performance. |
| Connector | Collection duration and dropped run observations | Helps show collection cost and bounded-collection limits. No unbounded transcript/history scan is used as a fallback. |

Not every counter is available on every OS or gateway version. Optional runtime capabilities fail closed: missing support means an omitted series, not a fabricated healthy zero. Per-loop token attribution is not available from the aggregate runtime token counters and is not guessed.

Completed-loop collection is bounded. It observes known visible conversations, revisits running records for terminal updates and deduplicates completed observations within the collector lifetime. The collector retains at most **8,192 completed run identities** and **512 unfinished observations**, with at most 32 bounded evidence queries per collection. Once the completed-identity ceiling is reached, new run measurements are dropped for the rest of that collector lifetime; resource metrics continue and `connector.runs.dropped` increases. A WebSocket reconnect does not reset this ceiling. Only a planned world-owner gateway restart creates a new collector, without historical backfill. This first release is not an unlimited run-event exporter. It is not a full historical export. Changing the allowlist or recent-conversation window changes what can be observed. Check the connector diagnostics and earliest available data before interpreting missing history as no work.

## Enable or disable collection

Update/sync the connector source using the [existing-world walkthrough](../README.md#install-the-extension-into-an-existing-world). Keep the existing enrollment, token, connector URL and allowlist. The world owner then chooses and performs its normal gateway restart. Deploying Hub does not update or restart any world.

The connector settings include:

```json
{
  "metricsEnabled": true,
  "metricsIntervalSeconds": 30
}
```

These are fields in the **complete** `gateway.extensions.botnexus-hub-connector` settings object, not a replacement for required connection settings. Preserve the existing fields when using `config set`; it replaces that object rather than merging it. The environment alternatives are `HUB_METRICS_ENABLED` and `HUB_METRICS_INTERVAL_SECONDS`, with the usual `BOTNEXUSHUB_` precedence. The supported interval is 15–3600 seconds. A slower snapshot interval also limits how often new metrics can reach Hub.

Use `metricsEnabled: false` to disable collection at the next owner-run restart. Existing retained history expires normally. Older connectors stay online and keep their dashboard/command behavior, but cannot supply metrics until updated. New connectors send metrics as an optional version-one snapshot field that older Hubs ignore.

## Storage and retention

The default Compose deployment stores:

- Hub registry, commands and snapshots in `/data/hub.sqlite`.
- Numeric metric samples, IDs, retention and chart choices in **`/data/metrics.sqlite`**.
- Both files in the existing **`botnexushub_hub-data`** named volume. Include both stores and their WAL data in a consistent backup. No new data volume or separate database service is required.

The metrics store starts with **7 days** of retention. Change the shared retention setting in the Metrics page with an explicit save. The supported range is 1–365 days. The saved setting survives Hub restarts; reducing it expires old metrics. It does not expire world enrollment, conversations, questions or command receipts.

`HUB_METRICS_RETENTION_DAYS` sets the initial default for a new store. `HUB_METRICS_DB_PATH` selects the separate store path when running outside the supplied Compose settings; it must not point at the Hub database. `HUB_METRICS_MAX_ROWS` adds a hard row bound, default one million. Hitting that bound removes the oldest samples, so available history can be shorter than the chosen retention period.

Numeric values are compact, but each sample also has IDs, timestamps and index overhead. Do not treat seven days as a guarantee of tiny storage or complete history at any world count. The Metrics settings/diagnostics report rows, database bytes and the available time range. Expiry runs at startup, on retention changes and in background maintenance even when no world is sending data. SQLite may reuse freed pages; file size need not shrink immediately after each expiry.

Removing a world from Hub revokes its enrollment and deletes its metric history and chart choices. It does not delete data inside that world. Enrolling the same ID later starts fresh metric history with the newly issued token. A temporary outage or disconnect does not delete history before the normal retention/cap limits.

## Data and access boundary

The metric payload uses a fixed catalog of metric IDs, finite numeric values, timestamps, weights and bounded routing IDs. It carries **no SQL text, prompts, tool arguments/results, stack traces, credentials, file paths or free-form labels**. Invalid or oversized metric data is dropped rather than stored as arbitrary content. This does not make the entire Hub data volume public: the separate Hub database still contains the metadata/questions and submitted commands described in the README.

Metrics use the existing world token and operator ingress boundaries. A world cannot send another world's measurements, and browser filters do not grant new access. Everyone admitted by the external operator policy can see the same enrolled worlds and change shared chart/retention settings. Hub still has no per-user roles.

Queries are time-bounded, indexed and downsampled with limits on selected metrics, points and series. Truncated results must be shown as partial, not as the complete world census. Narrow the world, agent, conversation or time range when a result reaches a limit.

For exact endpoint and envelope fields, see [the protocol](protocol.md). For deployment, backup and rollback, see [operations](operations.md). Actual release gates are in [verification](verification.md); this document is a usage guide, not a claim that a real-world installation has been performed.
