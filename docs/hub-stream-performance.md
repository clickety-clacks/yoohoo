# Hub listener CPU regression — September 14, 2026

## Failure and cause

Osanwe's installed `window-attention` process averaged 96.4% of one CPU core.
Per-thread inspection localized that usage to `agentd-hub`; the resolver
worker and border-animation thread were not responsible for the hot loop.
The CPU package measured 94°C. After stopping only the tracker it measured
65°C; no terminal or user tmux session was stopped.

The SSE listener set a 0.5-second socket timeout, then caught read errors and
retried the same buffered HTTP response. A timeout leaves that buffered reader
unusable, so subsequent reads failed immediately. The loop both burned CPU and
stopped receiving Hub updates while still appearing connected.

An isolated real HTTP server on Plumbus reproduced this with the installed
module: after an initial snapshot and 0.8 seconds of silence, a two-second
measurement consumed 2.002 CPU seconds (99.84% of one core). A second snapshot
sent on the same connection never reached the listener. No live Hub, desktop,
terminal, or tmux fixture was involved in this reproduction.

## Correction

Keep the established SSE stream blocking while quiet, without requiring
heartbeats. Stop wakes the reader by shutting down its owned socket; the
reader thread closes the response. Read errors leave the stream and enter the
existing reconnect backoff instead of retrying a poisoned reader in place.

Regression tests cover delayed updates, CPU usage while quiet, shutdown,
disconnect/reconnect, and read-error handling. Tests run on Plumbus only.

The corrected stream used 0.002435 CPU seconds over 45.000216 wall seconds
(0.0054% of one core), receiving both snapshots on the same connection.
All 75 existing non-desktop tests and four new stream regressions passed.
Sol independently reviewed the exact source and test hashes without findings.

Stream source SHA256:
`73f9f172b16a35d28b4a13994e27d9114d6d567837e562d8db51d46b419f0c34`

Stream regression SHA256:
`d51716c6dd37a4e7b025ae9a9d4e5747aacf00536b79da10491a492aa7b67bf1`

## Background resolver refresh

Normal-service observation after the stream fix exposed a second problem:
the Hub thread dropped below 1% CPU, but the background resolver kept using
roughly 27% of one core. Every Hub snapshot queued full process collection,
even when the waiting agents and terminal windows had not changed. The
tracker was stopped again while this was corrected.

A separate Plumbus benchmark used two synthetic pending agents and twelve
synthetic compositor rows referring to owned `sleep` processes. The production
resolver and Linux collector were real; window dispatch and Hub input were
injected. With 239 unchanged-input updates over 12 seconds, the old worker ran
478 resolver calls and consumed 7.401937 CPU seconds (61.67% of one core).
Both matching windows were retained. This was not a live desktop or real Hub
load test; it isolates repeated collection under update load. All owned
processes and the worker were stopped afterward.

Inspection also found that lists longer than eight pending agents requeued
themselves forever instead of stopping after a complete sweep.

The background fix fingerprints relevant pending-agent and compositor inputs.
Unchanged inputs reuse presentation matches until the next event after a
five-second passive TTL; changed inputs bypass that reuse. Hyprland events
mark the desktop dirty, so unrelated Hub updates can skip compositor queries
as well as process collection. A desktop epoch preserves events arriving
during a scan. The empty pending state needs no window inventory. User clicks
and focus revalidation remain fresh. Multi-page refreshes stop after one
complete sweep instead of continually requeuing themselves.

The final corrected worker passed the same Plumbus benchmark with six
resolver calls and six compositor inventories for 239 updates over 12 seconds,
retaining both matching windows. It consumed 0.156546 CPU seconds (1.3045% of
one core), versus 61.67% before. All 86 relevant tests passed on Plumbus,
including stream and worker regressions. Tests also cover finite nine-agent
coverage/cache merging, fresh focus validation, desktop event queueing,
identical-input reconnects, and the empty pending state. Sol independently
reviewed the final source/test pair and approved it without findings.

Worker source SHA256:
`3bb482b983bba6ba8b5b8d8794118f68c52cfd702ac8de566749b1f04c7e2064`

Worker regression SHA256:
`c2afe085f888313ee045d154e575d3cbd278f18cd3f566b6c88c726a102e9268`

## Reproducing the measurements

Run these on Plumbus from a staged checkout. Both benchmark scripts enforce
the test-machine hostname; neither creates windows nor touches tmux.

```sh
python3 tests/integration/benchmark_hub_stream.py payload/agentd_hub.py 45
python3 tests/integration/benchmark_hub_worker.py payload/window-attention
```

The automated stream/worker tests are `tests/test_hub_stream_performance.py`
and `tests/test_hub_worker.py`. The full relevant run also included attention,
Hub, adapter, bundled-install, installer, selection, and release tests.

## Installation

The final reviewed files were installed on osanwe at 8:54 PM PT on September 14.
Only Yoohoo's tracker was restarted; no shell/compositor restart, window
activation, harness change, or user tmux operation was performed. Installed
daemon and Hub module hashes match the Plumbus-tested source above. Previous
files are recoverable under
`.local/state/yoohoo/backups/1789444467444756491` (final worker update),
`.local/state/yoohoo/backups/1789443945570839365` (pre-worker-fix daemon), and
`.local/state/yoohoo/backups/1789443097368229149` (pre-fix stream module).

Read-only observation of the final osanwe service (PID 156333) measured
6.010034 CPU seconds over 71.87 wall seconds: **8.36% of one core for the whole
service cgroup, including helper processes**, versus roughly 100% before.
This is not the isolated stream's 0.0054% figure or the synthetic worker's
1.3045% figure. Three agents were pending in the normal workload. Cache-write
observation showed background matching approximately seven seconds apart,
consistent with the five-second passive interval plus work, not a hot loop.

The same process remained connected without a Hub error; its stored Hub
revision advanced from 224252 to 224414 during the interval. CPU package
temperature measured 67°C, compared with 94°C before the fixes. Temperature
is an observation of the whole machine, not a controlled thermal benchmark.

Automated tests and benchmark execution remained Plumbus-only. Osanwe checks
were normal-service resource/status observations; no test windows or harness
turns were generated. Periodic process matching still has a measurable cost;
the fix does not claim zero CPU usage for the full tracker.
