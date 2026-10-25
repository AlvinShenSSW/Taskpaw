# Current machine temperatures (#323)

## Scope and sequencing

V3 3.10.4 adds current sensor temperatures beside each local/remote machine's
power row. The operator authorized AFK delivery, +0.0.1 per issue (overriding
the issue's no-bump text), leave-open, and no deployment. This branch depends on
PR #324 at immutable approved base `acfd4e7f3e5135276e4742c30c19f156b35df3f1`.
The PR targets main for CI; merge #324 first. If #324 is squash-merged without
retaining this ancestry, rebase #323 onto main before merging.

V2 is frozen. Temperature history, alarms, fan control, scheduling changes and
estimated temperatures are excluded. No helper installation, privilege change,
live configuration change or production service launch is authorized.

## Verified availability and architecture adjustment

Read-only Windows verification found NVIDIA GPU core temperature through
`nvidia-smi`; the independent temperature report agreed with the selective
query. The final implementation will be compared again with fresh timestamps
and actual values. CPU/board readers were absent: neither LHM/OHM WMI namespace
nor an existing helper was found. The operator confirmed other machines have no
known reader and requested actually readable sensors first. CPU and board are
therefore explicitly unavailable (`no_reader`); no speculative LHM adapter ships.
No CPU, chipset, motherboard or ambiguous Temp1 is relabelled as chassis/ambient.

The issue suggests extending the shared NVIDIA power query. We deliberately
use a separate temperature owner instead. `core/gpu_sample.py` holds its lock
through the existing five-second subprocess call. An extended query consuming
that deadline leaves no fallback budget for power; two deadlines would lengthen
the shared lock, and stamping retained watts as new would corrupt energy
integration. This is an architectural risk analysis, not an observed native
temperature hang. The adjustment adds one bounded NVIDIA query per five-second
cycle while leaving the power/utilisation query, cache and integration unchanged.

[NVIDIA's documentation](https://docs.nvidia.com/deploy/nvidia-smi/index.html)
defines selective queries, CSV output and unsupported N/A values. The
[LibreHardwareMonitor README](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)
describes platform/sensor-dependent support and privileges; it does not establish
support on this lab's machines.

## Producer and wire contract

`core/temperatures.py` owns a single launcher-managed background sampler, even
when power estimation is off. Every five seconds it runs fixed argv
`nvidia-smi --query-gpu=index,temperature.gpu --format=csv,noheader,nounits`,
Windows-only, hidden, stdin closed, no shell, five-second subprocess timeout.
Scheduling uses a monotonic deadline; start/stop are idempotent, stop joins the
worker with timeout plus margin. HTTP handlers read a short-lock cache only.
Queries run outside the snapshot lock and outside PowerSampler.

An additive optional `temperatures` object is returned by `/status` and the
local `/control/power` view, and transported on each PowerFleet peer row:

```json
{
  "sampled_at": 1792920000.0,
  "age_s": 2.4,
  "stale": false,
  "cpu": {"celsius": null, "label": "CPU", "source": null, "reason": "no_reader"},
  "gpu": {"celsius": 62.0, "label": "NVIDIA GPU core (highest readable)", "source": "nvidia-smi", "reason": null, "readable": 1, "total": 2},
  "board": {"celsius": null, "label": "Board / chassis", "source": null, "reason": "no_reader"}
}
```

`sampled_at` is diagnostic Unix seconds from the query start, never used for
cross-machine freshness comparison. `age_s` is producer monotonic elapsed time
from that start (including query duration). A valid complete CSV query may have
zero readable temperatures: report null, never zero. Temperatures must be finite
and within -50..200 Celsius; N/A, NaN, infinity and invalid cells are unreadable.
Each nonempty CSV row requires one unique nonnegative integer GPU index and two
fields. Bad/duplicate index or malformed row makes the query invalid rather than
claiming full coverage. `total` counts the queried NVIDIA rows only; `readable`
counts valid temperatures. Max is explicitly highest *readable* NVIDIA GPU core
temperature, never sum/average and never an all-vendor coverage claim.

Provider failures clear the current GPU value and coverage, with `sampled_at`
and `age_s` null and `stale: true`; the next success replaces the snapshot.
Reasons include `pending`, `unsupported_platform`, `provider_missing`,
`permission_denied`, `timeout`, `query_failed`, `invalid_reading`, `no_sensor`,
and `no_reader`. Logs contain a bounded reason, not raw provider output, and
should avoid repeated unchanged-failure spam. Successful samples become stale
at age >=30 seconds. Missing or malformed age cannot be live.

## Transport and freshness

PowerFleet validates the optional object independently of watts/history. It
adds the complete status request elapsed duration to producer age on receipt,
then adds controller-cache monotonic elapsed time when returning a view. This
is conservative: request overlap can over-age slightly, never under-age a
reading. No remote wall clocks are compared. A failed/invalid status read marks
retained temperature stale immediately without resetting its sample age. Old
Agents without the field remain unprovided. Successful status lacking the field
clears a previous temperature. History failures (including 404), power-off and
no-power states never imply a temperature failure. Existing watt/energy rules
and self/duplicate aggregation are unchanged; no temperature is aggregated
across machines. The wire is bounded and sanitized; a malformed thermal payload
does not discard an otherwise valid power sample.

The browser uses only the day (24h) query for current temperatures. Query-cache
metadata records monotonic request start/receipt and adds that elapsed time once
to the wire age, then adds UI cache age once. Metadata survives remounts and is
not reset by a range change or structurally equal new response. A local one-second
tick ages data while polling stalls; a failed day request marks cached readings
stale immediately while keeping machine rows available. A failed history query
does not. Missing/invalid age is unavailable/stale, never silently fresh.

## Presentation

Preview: `design-system/taskpaw-v3/preview/power-temperatures-323.html`.
Keep existing dark power layout, numeric typography, accessible focus and icons.
Place a compact full-width sensor strip below each machine's power content:
CPU, NVIDIA GPU core highest, and board/chassis. Unavailable sensors show an em
dash plus explicit reason. GPU includes readable/total NVIDIA coverage and source;
stale values are marked expired and never styled as current. CPU and board remain
unavailable regardless of an unrelated valid GPU sample. Do not infer thresholds
or health alerts. Power-off still shows readable temperatures; old Agents show
unprovided/upgrade text. Narrow the page's estimate notice to watts and energy;
temperature readings are sensor values, not estimated watts. Responsive verification
covers 375, 768, 1024 and 1440 pixels with no page horizontal overflow.

## Verification and delivery

Before implementation: design debate and actual failing tests. Producer tests
cover parsing, multi-GPU partial coverage, missing helper, permission failure,
timeout/nonzero exit, nonfinite values, cache-only reads, monotonic age, thread
shutdown and power-off launcher wiring. The suite's default hardware reader is
replaced by an autouse fixture; only explicit native read-only verification uses
the real provider. Fleet tests cover old/invalid payloads, request/cache elapsed
time, status loss, history failure, no-power, unchanged watts/energy and no IO on
snapshot. UI tests cover the same independent states, remount, range changes,
request latency and stalled polling expiration.

Run canonical full `uv run --locked pytest` in the dedicated managed environment
with a new private pytest temporary child, plus uv lock, Ruff, mypy, full frontend
tests/lint/build. Actual PowerPage browser fixtures cover local/remote, old Agent,
stale/disconnected, partial multi-GPU, power-off and hanging requests at all four
widths. All browser product API traffic is mocked; no product server/database is
started. Compare fresh native sampler readings against `nvidia-smi -q -d TEMPERATURE`.
Record CPU/board and other machines as unverified/unavailable.

After author adversarial/constitution checks: commit, push, draft PR, complete
green CI, CTO self-review, Claude independent outer review, then Kimi final review.
Triage all findings. Codex must not serve as its own external reviewer. Leave
the ready PR open and report any degraded gate. Never merge or deploy.

## Design debate

Round 1 identified request/cache ages being lost at each hop, history 404 changing
the power state, UI aging only after polling completion, partial NVIDIA coverage,
and failed-provider stale-value risk. All five are incorporated above: independent
thermal state, explicit hop ages, local tick/cache metadata, validated coverage,
and cleared producer failure values. Round 2 independently reviewed this concrete
backend/UI contract and found no remaining blocker. A repeated sample identity
must retain its accrued UI age lower bound; a genuinely new sample may be younger.
For failures, coverage is 0/0 (unknown), explained by the failure reason. A valid
empty CSV is no_sensor; complete rows with no readable temperature preserve the
NVIDIA total and report invalid_reading. Zero Celsius itself remains a valid value.
