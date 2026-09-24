# #179 — Standalone「AV 翻译」task (`avsubs`) + in-process GPU lease shared with Jasna; version 3.5.0

Date: 2026-09-24 (design v3 — rounds 1–2: D1–D13, M1–M15, N1–N9 folded in)
Issue: #179 (depends on #178 and #177, both shipped in v3.4.0)
Driver: `/afk` (Claude Opus 5.5 leads; Opus subagents implement → internal review → Codex 外门
`gpt-6-astra` effort high → Kimi 终审). Merge policy: leave-open (owner merges).
Upstream: `docs/specs/2026-09-24-177-jasna-av-translate-design.md` (the shared `subs` package, the
settlement / deferral / single-dispatch rules, D1–D35, the implementation-stage record),
`docs/specs/2026-09-24-jasna-av-translate-spec-review.md` §4.4 / §5.C.

**Owner premises (recorded 2026-09-24, binding):** only ONE Jasna task ever exists; the LLM key is
never cleared while a run is in progress; there are no silent (no-speech) films. The owner tests
#177 and #179 together after this lands.

## Spec review

A new task type `avsubs` points at a library folder, walks it (recursively by default), and for
every MP4 that has no same-named `.srt` produces `<stem>.ja.srt` (WhisperJAV) and `<stem>.srt`
(Simplified Chinese via the agent's LLM setting) next to the video. It reuses #177's `subs`
package for everything engine-related; this issue adds tree planning, the task's own orchestration
and status, and a process-wide **GPU lease** so an `avsubs` task and the Jasna task never run GPU
children at the same time on the 8 GB card. The lease hands the GPU over **fairly per file**: when
one side finishes a file's GPU work while the other waits, the waiter gets it next.

The hard part is the lease's interaction with Jasna's exit/advance machinery. Jasna today gives
its hook hold when a restore exits (also before a retry of the same file) and takes a new one for
that file's ASR, and advances immediately after giving. Under a fair lease both would hand the GPU
to the waiter mid-file. v2 makes Jasna's hold **file-scoped** (D7/C5): kept while the same file
continues (retry, restore → ASR), given when the file's GPU work ends.

## Acceptance criteria

- [ ] AC1 `taskpaw_v3/core/gpu_lease.py`: process-wide lease keyed by `RunId = (instance_id,
  generation)`; `try_acquire(run, poll_interval, label="") -> bool`, `release(run) -> bool`,
  `withdraw(run) -> None`, `holder() -> Optional[RunId]`, `blocking_label() -> str`; fair hand-off
  with a reservation window `max(30 s, 2 × waiter poll_interval)`; stale-waiter pruning after
  `max(30 s, 3 × poll_interval)` without a try; a leaf lock; never blocks; never logs under its
  lock.
- [ ] AC2 Plugin `avsubs` (`type_id="avsubs"`, display「AV 翻译 (subtitles)」, category `task`,
  managed only, `manual_start()` always True) in `default_registry`; config fields per the issue
  with descriptions; zh/en UI strings; ServiceIcon glyph; About mention; README row.
- [ ] AC3 Pure `plan_tree(root, recursive, extensions)`: skip hidden dirs, link/junction dirs
  (Python 3.10-safe, C1/M1), Windows HIDDEN|SYSTEM dirs (M2), `.tmp.` files, names not encodable
  as UTF-8 (D10, reported); case-insensitive extensions; classify done / translate_only / full;
  reserve ja+zh targets in sorted order; report collisions and unreadable subfolders;
  deterministic order.
- [ ] AC4 Per-file lifecycle and settlement exactly as #177 (single writer under `_launch_lock`,
  `_settled` checked before publish, deferral rule applied by **every** lock holder, single-dispatch
  loop), terminal states `completed | failed | skipped(no_llm_key | unstable | collision |
  cancelled | no_exe)`.
- [ ] AC5 Three consecutive subtitle failures (settlement order; an unreadable pre-existing
  `.ja.srt` does not count, D9) → abort in the C7 order, one alert, `degraded`; Stop/abort never
  emit `done`; `done` only when every planned job is terminal, no live child, no queued/in-flight
  translation, results drained.
- [ ] AC6 GPU lease in `avsubs`: acquire before every ASR launch (exception-safe, D10); release on
  every ASR end path only after `terminate_tree` has killed the tracked process tree (D4/C11); a
  `False` result (a tracked process survived) raises one alert and the lease is still released
  (decision, see C11); `withdraw` on stop/abort/done/"GPU no longer
  needed" (D12) and after a refused try when stopping (D6); a refused acquire → wait, detail
  `waiting for GPU (held by <label>)`, no event, retried next check; translate-only never
  acquires.
- [ ] AC7 GPU lease in `jasna`: a **file-scoped hold** — acquired (before the probe, D5) when a
  file's first GPU child is about to start; kept across that file's retries and its restore → ASR
  (D7/C5); given when the file's GPU work ends; kept while a restore runs when subtitles are
  disabled; refused acquire → wait + retry, the file stays queued; `withdraw` on
  stop/abort/done/launch error/no-longer-needed and after a refused try when stopping.
- [ ] AC8 Stop/restart per #177, both plugins: translator cancelled first, bounded tree kill, one
  deadline, the lock-timeout branch kills without the lock (D13), every thread joined, lease
  released + withdrawn; temporaries swept at the next Start with an age gate (C3, M12).
- [ ] AC9 Status/Hub: `queue_completed/total/remaining/failed/skipped`, `current_file` (relpath),
  `phase ∈ asr | translate | waiting_gpu` (M6), `subs_translating`, cpu/mem/gpu; `status_md`
  renders `avsubs` through the lada/jasna queue block; openclaw guide rows.
- [ ] AC10 Shared helpers moved, not duplicated (C6/D1/M3): `asr_env()` and
  `validate_fields(...)` live in the `subs` package; each plugin keeps its own tiny
  `_default_spawn` bound to its module's `ChildProcess` (the D10 test seam).
- [ ] AC11 Version 3.4.0 → 3.5.0 (six files); CHANGELOG; README; openclaw guide.
- [ ] AC12 Tests per the plan; `uv run pytest`, ruff, mypy, UI lint, vitest green.

## Frozen issue contract

**In scope:** AC1–AC12. Allowed user-visible changes: the new task type and its fields; `.srt` /
`.ja.srt` next to videos under the chosen root; a `.avsubs/avsubs-<id8>/` working folder under the
root; Jasna may show `waiting for GPU (held by …)` and wait instead of launching; new metric keys;
version 3.5.0.

**Invariants:** constitution §2 (no `shell=True`; key never in argv/logs/events/detail; atomic
publish), §4 (no silent except; every thread joined; no orphan process), §5; owner rules (managed
GPU tasks never auto-start; `*_capture_progress` default false); `subs` public API: additions only
(one return type widened `None → bool`, callers unaffected); Jasna's behaviour is unchanged whenever
the lease is free, EXCEPT the four tests that pin the hook sequence (D2), which are edited and
listed; the agent→hub wire shape only gains keys; the lada `status.md` line stays byte-identical.

**Frozen-contract corrections from repository evidence:**

| # | Issue text | Evidence | Correction |
|---|---|---|---|
| C1 | "`os.path.islink()` 与 `os.path.isjunction()` 的目录" | `isjunction` is Python ≥ 3.12; `requires-python = ">=3.10"`; critic verified a junction has REPARSE_POINT and `st_reparse_tag == 0xA0000003`, `is_symlink() is False` (3.12, 3.13) | `_is_link_dir(entry)`: `entry.is_symlink()` or (Windows) `st_reparse_tag in {0xA0000003 (MOUNT_POINT), 0xA000000C (SYMLINK)}` from `entry.stat(follow_symlinks=False)` (in `try`); other reparse points (OneDrive placeholders) are descended (M1) |
| C2 | staging `<root>/.avsubs/<sha1(relpath)[:12]>/attempt-N/`, `--temp-dir <root>/.avsubs/tmp` | Jasna rmtree's `<output>/.avsubs/tmp` unconditionally at every Start; an `avsubs` root may be a Jasna output folder | `staging_root = <root>/.avsubs/avsubs-<sha1(instance_id)[:8]>/`; attempts `<staging_root>/<sha1(relpath)[:12]>/attempt-N`; temp dir `<staging_root>/tmp`; an `avsubs` Start removes only its own `tmp`; a job's `<staging_root>/<hash>` is removed after it settles (M11, deferred work) |
| C3 | "清扫 `*.<old generation>.tmp`" | targets are spread over the tree; a Jasna publish temp in the same folder matches the pattern; `sweep_orphan_staging` is age-gated for this reason | age-gated (≥ 10 min) sweep of `*.srt.<digits>.tmp` in the planned folders; **Jasna's own `*_restored*.srt.*.tmp` Start sweep gets the same age gate** (M12) |
| C4 | `try_acquire(run, poll_interval) -> bool`, `holder()` | the waiting detail must name who blocks; plugins have no id → name map; while free-but-reserved there is no holder (D8) | `try_acquire(..., label="")`; `blocking_label()` = holder's label, else the reserved waiter's label, else "" |
| C5 | jasna "同一文件 restore→ASR 之间不放" (+ "该文件 GPU 工作结束即放") | `_handle_exit` gives the restore hold before `_start_subs` takes; a requeued retry of the same file also gives (D7) | file-scoped hold, see Jasna section |
| C6 | (silent) | Jasna's `_asr_env` and WhisperJAV validation would be copied; the D10 seam is `J.ChildProcess`, so an alias to a function in `subs/child.py` bypasses it (critic: 20 failures) | move `asr_env()` → `subs/child.py`, `validate_fields(exe, engine, extra, *, required, owner)` → `subs/whisperjav.py`; each plugin keeps `def _default_spawn(argv): return ChildProcess(argv, env=asr_env())` in its own module |
| C7 | abort order: stop launching → terminate live ASR, confirm exit → job skipped(cancelled) → release → cancel translation → unstarted skipped → alert → degraded | #177 deferral rule; `terminate_tree` never reported failure (D4) | under the lock: `_aborted`, settle the live job and every unsettled job `skipped(cancelled)`, one alert; deferred (same holder, right after release): terminate the tree → **only if confirmed gone** release + withdraw (else keep, alert once, retry per check) → cancel the translator. `check()` runs pending deferred work before returning `degraded` (D9) |
| C8 | "缺 LLM key → skipped(no_llm_key)" | #177 CX4 | `avsubs` maps a translator result `failed("no LLM key")` to `skipped(no_llm_key)`; Jasna unchanged (owner accepted CX4) |
| C9 | exe missing → "error + 告警一次，全部 skipped(no_exe)" | — | `error`, jobs `skipped(no_exe)`, no translator, no lease, no `done` |
| C10 | lease "O(1)" | fair hand-off needs a waiter list | O(#waiters) (≤ number of GPU tasks, i.e. 2–3); recorded |
| C11 | issue "`ChildProcess` … `terminate` 并确认退出" | `terminate_tree` returns None; a kill-time psutil snapshot misses orphans once the launcher has exited (critic N1: `NoSuchProcess` → empty snapshot; Windows does not re-parent; `taskkill /T` on the dead launcher returns 128); a Job object misses children spawned before assignment (`JobKeeper` docstring; WhisperJAV's launcher starts python at once) | `ChildProcess` **tracks** descendants: every `poll()` while the direct child lives merges `psutil.Process(pid).children(recursive=True)` into `_tracked: dict[pid, create_time]`; `terminate_tree(timeout) -> bool` kills the tree (taskkill /T while intact) **and every tracked pid still running with the same create time** (psutil `kill`), waits within ONE deadline, and returns True iff the direct child and every tracked process are gone. `SubsJob.terminate() -> bool` = `terminate_tree(...) is not False` (fakes returning None count as gone, N2); `child` is reset as today. **Decision (D4 residual):** a `False` result is not a reachable state for our own children once they are tracked (TerminateProcess on a child we started); the plugins log the surviving pids, alert once and **still release the lease** — a "keep the lease" zombie state machine (v2) produced more defects (N2, N3, N6) than the risk it covered |

**Non-goals:** continuous watching; cross-process/multi-GPU coordination; several active tasks
writing the same targets (operator rule, stated in the field text — including an `avsubs` root
equal to the output folder of a Jasna task with「AV 翻译」on); other languages; subtitle editing;
lada; Jasna's #177 deferred items CX1/CX4/CX5 and IR-d (owner accepted); Shift-JIS `.ja.srt`
decoding.

**Causal boundary:** `taskpaw_v3/core/gpu_lease.py` (new), `taskpaw_v3/monitors/plugins/avsubs.py`
(new), `taskpaw_v3/monitors/registry.py`, `taskpaw_v3/monitors/plugins/jasna.py` (lease, file-scoped
hold, shared helpers, the srt-temp sweep age gate), `taskpaw_v3/monitors/subs/{child,job,
whisperjav,__init__}.py` (additions / widened return), `taskpaw_v3/hub/server/status_md.py`,
`taskpaw_v3/ui/src/{schemaI18n.ts,i18n.ts,components/ServiceIcon.tsx}`, `taskpaw_v3/tests/
conftest.py` + tests, six version files, `CHANGELOG.md`, `README.md`,
`docs/guides/openclaw-integration.md`, this doc.

## Assumptions

| # | Claim | Basis | Risk if wrong |
|---|---|---|---|
| A1 | #177 subs-package facts | verified in #177 | — |
| A2 | Both GPU tasks live in ONE agent process | verified (critic: one supervisor hosts every monitor) | two agents on one machine could overlap (non-goal) |
| A3 | `check()` runs on each instance's own supervisor thread at ≈ `poll_interval`; a raising check backs off 5–300 s | verified (critic) | stale floor 30 s (M9) covers slow checks |
| A4 | junction: REPARSE_POINT set, `st_reparse_tag == 0xA0000003`, `is_symlink()` False | verified on 3.12/3.13 (critic); 3.10 unverified (no Windows 3.10 in CI) | the helper is also unit-tested with `os.path.isjunction` removed |
| A5 | WhisperJAV accepts CJK/space paths as one argv element | #177 ran it on real paths | argv test + owner smoke |
| A6 | `psutil.Process(pid).children(recursive=True)` lists the WhisperJAV python + ASR worker while the launcher lives, and the worker lives for the whole transcription | psutil docs; the #177 precheck tree shape; the first `poll()` happens one `poll_interval` after the spawn, long before a transcription ends | a descendant born and orphaned between two polls is missed → `taskkill /T` (tree intact) still covers it while the launcher lives |

## Approach

**Lease first, as a tiny pure module.** No dependency on monitors; both plugins call it through
thin hooks. Fairness is a reservation the lease answers, never a callback, so the lease is a leaf
and every behaviour is a pure function of the call sequence and an injected clock.

**avsubs mirrors #177's orchestration, simplified** (one GPU child kind). It reuses `SubsJob`,
`Translator`, `ChildProcess`, `asr_env`, `needs_llm_key`, `validate_fields`, `srt`, and implements
only its own queue / settle / abort / done / status loop, because its decisions differ (abort the
run, no restore counters). The #177 rules carry over verbatim, with the #177 lessons built in from
the start: translator registered before its Stop re-check (CX2), 0-cue transcript before the key
check (CX3), blank-line-safe cues (F1), translator released after `done` (F2), a `no LLM key`
result is a skip (CX4), a Stop inside the poll window keeps a `no_speech` result (CX5).

**Jasna gets surgical changes:** lease-backed hooks; `_gpu_take` returns bool and a refusal
becomes a wait; the hold is file-scoped; withdraws at run ends; the shared helpers; the age-gated
srt-temp sweep.

### Module design

#### `taskpaw_v3/core/gpu_lease.py`

```
RunId = tuple[str, int]
RESERVE_MIN_S = 30.0
STALE_MIN_S = 30.0
STALE_FACTOR = 3.0

@dataclass
class _Waiter: poll_interval: float; last_try: float; label: str

class GpuLease:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None
    def try_acquire(self, run: RunId, poll_interval: float, label: str = "") -> bool
    def release(self, run: RunId) -> bool          # False when run is not the holder (warning logged AFTER the lock)
    def withdraw(self, run: RunId) -> None         # drop run's waiter entry and any reservation for it
    def holder(self) -> Optional[RunId]
    def blocking_label(self) -> str                # holder's label, else reserved waiter's label, else ""
    def reserved_for(self) -> Optional[RunId]      # diagnostics / tests
    def waiters(self) -> list[RunId]               # registration order

_LEASE = GpuLease()
def try_acquire(...); def release(...); def withdraw(...); def holder(); def blocking_label()
def _reset_for_tests(clock: Callable[[], float] = time.monotonic) -> None   # replaces _LEASE
```
State under one `threading.Lock` (leaf: nothing else is taken while it is held, no logging inside;
callers may hold their own locks): `_holder`, `_label`, `_waiters: dict[RunId, _Waiter]`
(insertion order = registration order; an update keeps the position), `_reserved:
Optional[tuple[RunId, float]]`.

`_prune(now)`: drop waiters with `now - last_try > max(STALE_MIN_S, STALE_FACTOR * poll_interval)`;
an expired reservation is cleared and its waiter dropped; a reservation whose run is no longer a
waiter is cleared; then, if the lease is free, no reservation is active and waiters remain,
reserve for the earliest waiter (deadline `now + max(RESERVE_MIN_S, 2 * w.poll_interval)`).

`try_acquire(run, pi, label)`: `_prune(now)`; holder == run → True (label refreshed); lease free
and (no reservation or reservation is for `run`) → grant (holder, label, remove from waiters,
clear reservation) → True; else register/update waiter → False.

`release(run)`: holder != run → False (warning logged after the lock with the run ids); else free
it and `_prune(now)` (reserves for the earliest waiter; `run` is not a waiter at that moment) → True.

`withdraw(run)`: remove the waiter; clear a reservation for `run`; `_prune(now)` (passes the turn
to the next waiter). Never touches `_holder`.

#### `taskpaw_v3/monitors/subs/` (additions)

```
# child.py
LLM_ENV_PREFIX = "TASKPAW_LLM_"
def asr_env(base: Optional[Mapping[str, str]] = None) -> dict[str, str]     # copy minus TASKPAW_LLM_*
class ChildProcess:
    def poll(self) -> Optional[int]
        # unchanged result; additionally, while the child is alive, merge psutil children(recursive=True)
        # into self._tracked {pid: create_time} (psutil errors ignored, never raises)
    def terminate_tree(self, timeout: float = 5.0) -> bool
        # ONE internal deadline = now + timeout (N4). Refresh _tracked once more if the child lives.
        # Windows: taskkill /PID /T /F (bounded by the remaining time); POSIX: terminate the child.
        # Then for every tracked pid: psutil.Process(pid) whose create_time matches → kill() (pid-reuse safe).
        # Then wait for the child and the tracked processes (psutil.wait_procs) until the deadline;
        # a still-running child gets proc.kill() once more.
        # Returns True iff the direct child has exited and no tracked process is running. Never raises.
# job.py
class SubsJob:
    def terminate(self, timeout: float = 5.0) -> bool      # gone = child.terminate_tree(...) is not False (N2);
                                                           # join_readers; child = None (as today); no child → True
# whisperjav.py
def validate_fields(exe: str, engine: str, extra: str, *, required: bool, owner: str) -> None
    # required and not exe.strip() → ValueError(f"{owner} needs whisperjav_exe_path — the full path to whisperjav.exe")
    # then the exact #177 texts for unparseable quotes and owned/forbidden flags
```
Jasna calls `validate_fields(..., required=self.av_translate, owner="AV 翻译 (av_translate)")` —
identical messages. Jasna keeps `def _default_spawn(argv): return ChildProcess(argv,
env=asr_env())` (D1); `_asr_env` is removed in favour of `asr_env`.

#### `taskpaw_v3/monitors/plugins/avsubs.py`

Config `AvsubsConfig(BaseMonitorConfig)` (`extra="forbid"` inherited), all fields described:
```
avsubs_root_folder: str = ""              # directory picker; required. Text: do not let two active tasks
                                          # cover overlapping folders, and do not point it at the output folder
                                          # of a Jasna task that has AV 翻译 on.
avsubs_recursive: bool = True
avsubs_extensions: list[str] = ["mp4"]    # strip, lower, drop leading ".", dedupe; each [a-z0-9]+; non-empty
whisperjav_exe_path: str = ""             # file picker; required
whisperjav_engine: Engine = DEFAULT_ENGINE
whisperjav_extra_args: str = ""           # "quote Windows paths"
avsubs_gpu_monitor: bool = True
```
Validator: root required; extensions valid; `validate_fields(..., required=True,
owner="AV 翻译 (subtitles)")`.

Pure planning:
```
Kind = Literal["full", "translate_only"]
@dataclass(frozen=True)
class TreeItem: source: Path; relpath: str; ja_target: Path; zh_target: Path; kind: Kind; identity: tuple[int, int]
@dataclass(frozen=True)
class TreePlan: items: list[TreeItem]; done: int; collisions: list[tuple[Path, Path]]; errors: list[str]
def plan_tree(root: str, recursive: bool, extensions: Iterable[str]) -> TreePlan
```
- `OSError` when `root` itself cannot be listed (→ `error`).
- Iterative `os.scandir` walk. A directory is descended when `recursive`, its name does not start
  with `.`, `_is_link_dir` is False (C1), and on Windows its attributes carry neither
  `FILE_ATTRIBUTE_HIDDEN` nor `FILE_ATTRIBUTE_SYSTEM` (M2: `$RECYCLE.BIN`, `System Volume
  Information`). Unreadable subdirectory → skipped, path appended to `errors`.
- A file qualifies when its suffix (casefolded, no dot) is in the set, `".tmp." not in
  name.casefold()`, and `relpath.encode("utf-8")` succeeds (else → `errors`, D10).
- Targets `ja = <dir>/<stem>.ja.srt`, `zh = <dir>/<stem>.srt`; `relpath` POSIX.
- Order: qualifying files sorted by `(relpath.casefold(), relpath)`.
- Reservation keys: `normcase(join(realpath(dir), name))` with `realpath(dir)` computed once per
  directory (M15). An item whose ja or zh key is already reserved → `collisions.append((source,
  first_owner_source))` (M8), not planned; else its keys are reserved, then `zh.exists()` → done
  (a 0-byte zh counts); `ja.exists()` → `translate_only`; else `full`.
- `identity = source_identity(source)` (`os.stat`, D11); an `OSError` here → `errors`, not planned.

Instance state: `_run`, `_staging_root`, `_queue: list[TreeItem]` (full items not yet started),
`_jobs: dict[str, SubsJob]` (job_id = relpath), `_kinds`, `_settled: dict[str, tuple[str, str]]`,
counters `_pre_done/_completed/_failed/_skipped/_total`, `_streak`, `_aborted`, `_launch_error`,
`_asr_job`, `_asr_started_at`, `_translator`, `_gpu_held`, `_waiting_gpu`, `_advance_requested`,
`_deferred`,
`_had_work`, `_done_emitted`, `_key_alerted`, `_stopping: threading.Event`, `_launch_lock:
threading.RLock`, `_spawn = _default_spawn`, `_idle_note`.

Flow (every lock holder ends with `_run_deferred()`; the ones marked ⟳ then call `_dispatch()`):
- `start(emit)`: stop leftovers (an ASR job, a translator, `_gpu_held` or a registered wait →
  `stop()`, which releases and withdraws the OLD run first, N7); `_run = (iid, next_generation())`;
  reset (incl. `_waiting_gpu`); `_staging_root`; `rmtree(_staging_root/"tmp")`; root
  checks → launch error (`error`, alert `launch`); `plan_tree` (OSError → launch error); age-gated
  temp sweep (C3); collisions counted failed + one alert; `errors` → one alert listing ≤ 5; exe
  missing → C9; nothing to do → idle note (`nothing to subtitle (N already have .srt)` / `no video
  files under <root>`), **no translator**, no event (M7). Else: create jobs (identity pre-set);
  create the translator, **assign `self._translator` first**, `start()`, then if `_stopping` →
  cancel + join (CX2); `_had_work = True`; load every `translate_only` job's `.ja.srt` OUTSIDE
  the lock (N8), then under the lock settle/submit each (they need no GPU, M14);
  `_run_deferred()`; request + `_dispatch()` ⟳.
- `_advance()` (only via `_dispatch`'s loop): return if stopping / aborted / launch error / ASR
  job live; if `_queue` is empty → `_maybe_done()`; else `if not self._gpu_try():
  self._waiting_gpu = True; return` (item stays at the head); else `_start_asr(self._queue[0])`.
- `_gpu_try() -> bool`: `ok = gpu_lease.try_acquire(run, cfg.poll_interval, label=cfg.name)`;
  `ok` → `_gpu_held = True`; not ok and `_stopping` set → `gpu_lease.withdraw(run)` (D6).
- `_start_asr(item)`: pops the item; `release = False`; `try:` under the lock: stopping → release;
  else `err = job.start_asr(self._spawn)` (+ one retry on a launch error), post-spawn `_stopping`
  re-check → `job.terminate` + release; `None` → `_asr_job = job`, `_asr_started_at`; `"unstable"`
  → `skipped(unstable)` + alert, release, request; other → `failed` + alert, release, request.
  `except Exception as e:` → under the lock settle `failed(f"internal: {type(e).__name__}")` +
  alert, release, request, `log.exception` (D10). `finally`: if `release` and no live child →
  `_gpu_rel()`. Then `_run_deferred()`. Never dispatches.
- `_poll_asr()` ⟳: like Jasna's `_poll_subs` (D22 guard, D33 retry branch, D35 stop re-check),
  `succeeded` → `publish_ja` → `_submit_translation`; `no_speech` → `publish_empty` →
  `completed("no speech")`; terminal → `_asr_job = None`, release, request, cleanup of the job's
  staging dir deferred (M11).
- `_submit_translation(job, cues)` (under the lock): `cues is None` → `job.load_ja()`; `SrtError`/
  `OSError` → `failed("unreadable .ja.srt: …")` + alert, **not counted in the streak** (D9);
  0 cues → publish the empty zh → `completed("no speech")` (before the key check, CX3); key
  missing → `skipped(no_llm_key)` + once-per-run alert; else submit.
- `_settle_results()` ⟳ (per result, lock released between): drop other generations /
  `CANCELLED`; skip already settled; `translated` → `publish_zh` → `completed` or `failed` + alert;
  `failed` with detail `"no LLM key"` → `skipped(no_llm_key)` (C8); other `failed` → `failed` +
  alert.
- `_settle(job_id, terminal, reason, *, streak=True)`: once; counters; streak: `completed` and
  `skipped(no_llm_key)` reset, `failed` +1 (unless `streak=False`), other skips unchanged; streak ≥
  3 and not aborted → `_abort()`. Defers removal of the job's staging dir (M11) — except for the
  job whose ASR child is still live, whose removal happens inside the kill cleanup after
  `terminate` (N6).
- `_abort()` (under the lock, C7): `_aborted = True`; settle the live job and every unsettled
  job `skipped(cancelled)`; `_queue = []`; one alert (`avsubs-aborted`: `AV 翻译 aborted after 3
  consecutive failures | Queue: X/Y done, F failed, K skipped`); defer `cleanup()` with `job`,
  `translator`, `emit` captured: `gone = job.terminate() if job is not None and job.child is not
  None else True`; not gone → `log.error` + one alert (`avsubs-survivor`: "a WhisperJAV process
  may still be running; check Task Manager"); then (always) `_gpu_rel()`, `withdraw(run)`,
  `_asr_job = None`, remove that job's staging dir (N6); then `translator.cancel()` (+ join ≤ 2 s).
- `_maybe_done()`: `_had_work`, not emitted, not aborted/stopping/error, `_queue` empty, no ASR job,
  every job settled, translator idle (queued 0, not in flight, results empty) → emit
  `done` once (`AV 翻译 complete | Queue: X/Y done, F failed, K skipped | <ts>`); `withdraw(run)`;
  `self._translator = None` then `cancel()` + `join(2.0)` (F2).
- `check(emit)`: not started → error; (1) `_settle_results()`; (2) `_run_deferred()` (D9 — any
  deferred work left by a holder); (3) launch error → `error`; (4) aborted → `degraded`; (5)
  `_poll_asr()`; (6) if `_waiting_gpu` and no ASR job and not stopping → `_waiting_gpu = False`;
  request; `_dispatch()`; if afterwards neither waiting nor an ASR job exists → `withdraw(run)`
  (D12); (7) `_maybe_done()`; (8) status.
- `stop(timeout)`: one deadline; `_stopping.set()`; cancel the translator first; timed lock
  acquire; **acquired**: `job = _asr_job` read once; live → `gone = job.terminate(bounded)`;
  exited-0 unpolled → `poll_asr()`; `succeeded` → `publish_ja`; `no_speech` → publish the empty
  **ja only** (CX5); `job.terminate(0.5)`; **not acquired** (D13): read `_asr_job` once and
  terminate it without the lock; in both branches then `_gpu_rel()` (a `False` from `terminate` is
  logged with the surviving pids) and `withdraw(run)`; release the lock; join the translator with
  the remaining budget.
- Status: state `running` while an ASR child is live or translations are pending; `idle` while
  waiting with nothing else running; `idle` when finished; `degraded` after abort; `error` on
  launch error / no exe. `phase` (M6): `asr` with a live ASR child; else `translate` while
  translations are pending; else `waiting_gpu` while waiting; absent otherwise. Detail:
  `transcribing: <relpath> [<engine>] · MM:SS elapsed · translating N · X/Y done`;
  `translating N · X/Y done` (+ ` · waiting for GPU (held by L)` when also waiting);
  `waiting for GPU (held by <blocking_label()>) · X/Y done`. Metrics: `queue_completed =
  _pre_done + _completed`, `queue_total`, `queue_failed` (incl. collisions), `queue_skipped`,
  `queue_remaining = total − completed − failed − skipped`, `current_file` (relpath, live ASR only),
  `phase`, `subs_translating`, `_cpu_mem()`, GPU via `read_gpu()` if `avsubs_gpu_monitor`.

Dedupe keys (prefixed `f"{iid}:"`): `launch`, `collisions`, `scan-errors`, `avsubs-noexe`,
`avsubs-nokey`, `avsubs:<relpath>`, `avsubs-unstable:<relpath>`, `avsubs-aborted`,
`avsubs-survivor`.

#### `taskpaw_v3/monitors/plugins/jasna.py` (AC7)

- Hooks: `_gpu_acquire() -> bool`: `gpu_lease.try_acquire(self._run, cfg.poll_interval,
  label=cfg.name)`; refused while `_stopping` → `withdraw` (D6). `_gpu_release()`:
  `gpu_lease.release(self._run)`.
- **File-scoped hold (C5/D7).** `_gpu_take(holder) -> bool` (False when refused, nothing recorded).
  New `_gpu_transfer(old, new) -> bool` swaps `_gpu_holder` from `old` to `new` without touching the
  lease (False when `old` is not the holder). Rule: `_handle_exit` does **not** give the restore
  hold when the next action continues the same file — (a) `"subs"` for a `full`-kind file, or
  (b) `"advance"` after `_requeue_current()` put `_current` back at the head of `_pending` — it
  stores it as `self._carried = hold`. `_launch_next` then transfers `_carried` to its new hold
  instead of taking (and gives it if it does not launch); `_start_subs(video, emit)` transfers
  `_carried` to the job when present. Any path that consumes or abandons a continuation without
  launching gives `_carried` (e.g. `_start_subs` early return, stop, abort) — `stop()` gives
  whatever `_gpu_holder` currently is (M4).
- `_launch_next`: **take the lease (or transfer the carried hold) BEFORE the probe** (D5); refused
  → `self._gpu_waiting = True; return` with `_pending` untouched; the existing `finally` gives the
  hold when nothing launched.
- `_start_subs` subs-only path (from `_advance`, no carried hold): refused →
  `self._subs_only.insert(0, video); self._gpu_waiting = True; return` (no settlement, no request).
- `_check_managed`: after `_poll_subs`: if `_gpu_waiting` and no live child and not stopping /
  aborted / launch error → `_gpu_waiting = False`; request; `_dispatch()`; if afterwards neither
  waiting nor a live GPU child → `gpu_lease.withdraw(self._run)` (D12).
- `withdraw` also in `stop()`, the abort branch of `_fail_current`, `_maybe_done` (after `done`),
  and `_emit_launch_error`. `start()`/`_reset_subs` reset `_gpu_waiting` and `_carried` (M5).
- Jasna ASR tree kills (`_stop_asr`, the `_disable_subs` cleanup, `_poll_subs` stop re-check) get
  the tracked-tree kill through `SubsJob.terminate()`; a `False` result is logged (pids) and, where
  an `emit` is available, alerts once (`subs-survivor`); the hold is released exactly as today — no
  new state (N3).
- `_launch_next`: the `try/finally` that gives an unused hold now opens immediately after the
  take/transfer, so it covers the probe and the reader join too (N5).
- `_reset_subs` (start): release and withdraw the OLD run's lease if `_gpu_holder` is set before
  clearing it (N7).
- `_sweep_subs_leftovers`: the `*_restored*.srt.*.tmp` deletion becomes age-gated (≥ 10 min, M12);
  the `.avsubs/tmp` rmtree is unchanged.
- `_build_status`: `_gpu_waiting` with no live child → state `idle` (or `running` if translating),
  detail `waiting for GPU (held by <blocking_label()>)` + the usual queue text; `phase` keeps its
  #177 values.

#### Hub / UI / docs

- `status_md.py`: `is_lada = tid in ("lada", "jasna", "avsubs") or …` (lada line byte-identical).
- `schemaI18n.ts` `avsubs` block (zh, seven fields); `i18n.ts` `services.avsubs` en/zh + About
  mention; `ServiceIcon.tsx` `avsubs` glyph (film frame + two subtitle lines).
- README plugin row; CHANGELOG `## V3 3.5.0`; openclaw guide `avsubs` rows.

## Files to change

| Path | Change | Reason |
|---|---|---|
| `taskpaw_v3/core/gpu_lease.py` | create | AC1 |
| `taskpaw_v3/monitors/subs/child.py`, `job.py`, `whisperjav.py`, `__init__.py` | `asr_env`, `terminate_tree -> bool`, `SubsJob.terminate -> bool`, `validate_fields` | C6, C11 |
| `taskpaw_v3/monitors/plugins/avsubs.py` | create | AC2–AC6, AC8, AC9 |
| `taskpaw_v3/monitors/registry.py` | register | AC2 |
| `taskpaw_v3/monitors/plugins/jasna.py` | hooks, file-scoped hold, wait/retry, withdraw, helpers, sweep age gate | AC7, C3, C5, C6 |
| `taskpaw_v3/hub/server/status_md.py` | tuple | AC9 |
| `taskpaw_v3/ui/src/schemaI18n.ts`, `i18n.ts`, `components/ServiceIcon.tsx` | strings, glyph | AC2 |
| `taskpaw_v3/tests/conftest.py` | autouse `_gpu_lease_isolation` (fresh lease per test) | D3 |
| `taskpaw_v3/tests/test_gpu_lease.py`, `test_avsubs.py`, `test_jasna_lease.py`, `test_gpu_cross_plugin.py` | create | AC12 |
| `taskpaw_v3/tests/test_subs_child.py`, `test_subs_job.py`, `test_subs_whisperjav.py`, `test_status_md.py`, `test_catalog.py` | additions | AC9, AC10, C11 |
| `taskpaw_v3/tests/test_jasna_subs.py` | the four hook-sequence tests (D2) edited; nothing else | C5 |
| six version files, `CHANGELOG.md`, `README.md`, `docs/guides/openclaw-integration.md` | modify | AC11 |

The four D2 edits: `test_restore_then_asr_then_translation_end_to_end` (acquire count 4 → 2 per
the file-scoped hold); `test_asr_retry_that_cannot_start_settles_like_a_final_failure[unstable]`
and `[raises]` (3 acquires / 2 releases → 2 / 1); `test_stop_winning_the_lock_before_start_asr_
still_releases_the_gpu` (rewritten onto the subs-only `_start_subs` path, which still acquires).

## Execution surface

Writes: the files above. Executes: `uv run pytest`, `uv run ruff check .`, `uv run ruff format
--check taskpaw_v3 tests scripts`, `uv run mypy`, `cd taskpaw_v3/ui && npm run lint && npx vitest
run`. Tests never run `jasna.exe` / `whisperjav.exe` / the network / ports 5680-5681; ASR children
are `ChildProcess` fakes via `_spawn` or the module-level `ChildProcess` seams (`J.ChildProcess`,
`AV.ChildProcess`); translators via `J.Translator` / `AV.Translator`; the lease is reset per test
by the autouse fixture with a controllable clock; `terminate_tree -> bool` is tested with real
`sys.executable` trees (Windows grandchild + a POSIX variant); the junction test uses
`subprocess.run(["cmd", "/c", "mklink", "/J", …])` (list argv) and is skipped off Windows.

## Key implementation notes

- Lock order: `_launch_lock` → Jasna `_gpu_lock` → the lease's leaf lock. The lease never calls out
  and never logs under its lock.
- Every lock holder in both plugins ends with `_run_deferred()`; `check()` also runs it at step 2.
- `_dispatch()` is a loop; a refused acquire re-sets `_gpu_waiting` and ends it (no spin).
- The reservation window uses the WAITER's poll interval; the stale limit has a 30 s floor.
- `terminate_tree` is bounded by one internal deadline (`timeout`) plus at most one final
  `proc.kill()` + 1 s (N4); callers pass their remaining stop budget.
- `ChildProcess.poll()` tracking must never raise and must stay cheap (one psutil call per poll).

## Risk assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Jasna regression from the file-scoped hold / wait | medium | stalled restores or double launches | existing suites green except the 4 listed edits; new wait/transfer/retry/stop tests |
| Lease stuck held | low | the other task waits forever | release on every end path; stop always releases; `blocking_label` in the detail |
| A tracked WhisperJAV process survives the kill | very low | VRAM held by an orphan after release | tracked-pid kill (create-time checked); `log.error` + `survivor` alert names it |
| Ghost waiter after Stop | low | 30–2×pi s delay | D6 + withdraw everywhere + stale pruning |
| Junction detection on 3.10 | low | a loop | reparse-tag check + a test with `isjunction` removed |
| Huge library scan in `start()` | low | slow Start | scandir/stat only; realpath per directory |

## Out of scope

See non-goals. Also: per-file pause/resume; per-folder engine choice.

## Test plan

**`test_gpu_lease.py`** (fake clock): acquire/release/holder/`blocking_label` (holder, reserved
waiter, empty); idempotent re-acquire; release by a wrong instance or wrong generation refused,
warning emitted outside the lock, never raises; double release refused; waiter order kept on
update; releaser re-acquire refused while another waits and the waiter then wins; window =
`max(30, 2 × poll)` (both branches); expired reservation passes to the next waiter / frees the
lease; stale pruning uses `max(30, 3 × poll)`; `withdraw` removes waiter and reservation and passes
the turn; no waiter → immediate re-acquire; 8-thread hammer never sees two holders.

**`test_subs_child.py` / `test_subs_job.py` / `test_subs_whisperjav.py`** (additions): `asr_env`
strips `TASKPAW_LLM_*` only; `poll()` tracks descendants; `terminate_tree` returns True for a
killed two-level tree (Windows grandchild; use `sys._base_executable` because the uv venv's
`sys.executable` is a trampoline that adds a level, N9); **the launcher exits while its grandchild
lives (after one `poll()`) → `terminate_tree` still kills the grandchild and returns True** (N1);
True for an already-exited child with nothing tracked; False when a tracked process survives
(simulated by monkeypatching psutil `kill`), and a second call still returns False; one deadline
(total time ≤ timeout + 1.5 s); a reused pid (different create time) is never killed;
`SubsJob.terminate` returns `ok is not False` (a fake returning None counts as gone) and resets
`child`; `validate_fields` messages (owner label, required vs not, quotes, owned /
forbidden flags incl. prefixes and `custom`).

**`test_avsubs.py`**: config; `plan_tree` (recursive / non-recursive; hidden dir; symlink dir;
Windows junction (skip off win32); `_is_link_dir` with `os.path.isjunction` removed; OneDrive-like
reparse tag descended (fake entry); Windows HIDDEN|SYSTEM dir skipped (fake entry attributes);
extension case; `.tmp.` files incl. `x_restored.tmp.mp4`; non-UTF-8-encodable name → `errors`;
done / translate_only / full; 0-byte zh = done; cross-extension and cross-role collisions counted
failed; same stem in two folders → two staging dirs; spaces and CJK names; case variants; stable
order; unreadable subdir → `errors`; identity equals `source_identity`); lifecycle (order;
translate_only submitted at Start, never acquires; `no_speech`; `unstable`; retry + alert; spawn
failure releases the lease; an exception inside the launch releases and settles failed; missing key
→ skipped + one alert, next job translates after the key appears; `failed("no LLM key")` → skipped
(C8); unreadable pre-existing `.ja.srt` → failed, streak unchanged; 3-strike abort in C7 order —
tree terminated BEFORE release, translator cancel after, all outside the lock, in the same check,
including when the third failure is settled from the Start-time translate-only path (D9); the
aborted live job's staging dir is removed only after its kill (N6); a `False` kill → one
`survivor` alert and the lease is still released;
`done` exactly once, each condition alone blocks it, translator released after `done`; Stop with a
live ASR releases + withdraws; Stop lock-timeout branch kills without the lock (D13); Stop with an
exited-0 unpolled `no_speech` keeps the empty ja (CX5); Stop while a request hangs joins in budget;
immediate re-Start no deadlock; translator registered before its Stop re-check (CX2); real
`Supervisor.register → unregister → register` + `reconfigure`: old results and a late
old-generation release do not affect the new run; exe missing (C9); root missing; empty tree →
idle, no event, no translator thread; own `tmp` swept, a sibling `.avsubs/tmp` survives (C2);
age-gated temp sweep (C3); job staging dir removed after settle (M11); metrics / phase / detail
contract incl. waiting-while-translating; waiting detail names the reserved waiter while the lease
is free (D8); refused try while stopping withdraws (D6); a retry that finds nothing to do withdraws
(D12)).

**`test_jasna_lease.py`**: lease taken before the probe (probe not called while refused, D5);
file-scoped hold — no release between a file's restore and its ASR, none between a failed restore
and its same-file retry (D7), released once the file's GPU work ends; subs-only ASR acquires;
refused acquire leaves `_pending` / `_subs_only` intact, shows the waiting detail, emits nothing,
launches on the next check once free; stop while waiting withdraws; stop with a carried hold gives
it (M4); stop / abort / done / launch error withdraw; subtitles disabled during a restore keep the
hold until that restore ends; a probe that raises gives the hold (N5); a restart releases and
withdraws the old run's lease (N7); the
`*_restored*.srt.*.tmp` sweep is age-gated (M12); Stop racing the carried hand-over (D2).

**`test_gpu_cross_plugin.py`** (real instances with fakes, fake-clock lease): avsubs holds → Jasna
restore and subs-only launches wait with the detail naming avsubs; avsubs finishes its file →
Jasna gets the next turn (avsubs's immediate retry refused) and vice versa (Jasna finishes one
file's restore + ASR → avsubs gets the next turn, not Jasna's next file); avsubs 3-strike abort
while its ASR child lives keeps the lease until that child's tree has been killed; Jasna disabling
subtitles mid-restore keeps the lease; stopping one side lets the other acquire on its next check.

**`test_status_md.py` / `test_catalog.py`**: `avsubs` line like jasna; lada unchanged; catalog lists
`avsubs` with directory/file pickers. **UI**: vitest — i18n keys in both languages; ServiceIcon
renders `avsubs`.

**Manual smoke (owner, with #177):** an AV 翻译 task on a folder with subfolders → only videos
without `.srt` are processed; files appear next to each video; `done` reports `Queue: X/Y done`;
Start again → all skipped. With a Jasna task running at the same time, one shows `waiting for GPU
(held by …)` and they alternate per file.

## Handoff notes

- Pilot split: **A** = `core/gpu_lease.py`, `subs` additions (`asr_env`, descendant tracking in
  `ChildProcess.poll`, `terminate_tree -> bool`, `SubsJob.terminate -> bool`, `validate_fields`),
  `tests/conftest.py` fixture, and their tests —
  no plugin edits. **B** (after A) = `jasna.py` (everything in its section) + `test_jasna_lease.py`
  + the four D2 edits in `test_jasna_subs.py`. **C** (after A, parallel with B) = `avsubs.py`,
  registry, `status_md.py`, openclaw guide, `test_avsubs.py`, `test_status_md.py`,
  `test_catalog.py`. **D** (any time) = UI strings + glyph + vitest. **E** (after B and C) =
  `test_gpu_cross_plugin.py` by C's pilot. Driver: version bump, CHANGELOG, README, sweep,
  commit/push/PR.
- Do not touch `lada.py`; `subs` changes are additions (plus the widened return type).

## Implementation-stage record (initial implementation, before the PR gates)

- **Clean-round items carried into the implementation (round 3 minors):** m1 — `SubsJob.poll_asr`
  kills still-running tracked processes after the direct child exits (via the new
  `ChildProcess.kill_tracked()`); m4 — `_tracked` is guarded by a small lock and read as a copy;
  m5 — the Start-time translate-only submit re-checks `_stopping`; (c) — `SubsJob.terminate()`
  keeps `child` while the DIRECT child still runs, so the plugins' live-child guards keep blocking
  launches (Jasna reaps it with `_reap_survivor`, avsubs in `_poll_asr`); m3 — the C7 row above is
  superseded by AC6/C11: the abort always releases after the kill, a `False` kill raises one
  `survivor` alert.
- **Driver decisions during implementation:** macOS AppleDouble files (`._*`) are skipped by
  `plan_tree` (a Mac-copied library would otherwise feed the three-strike abort);
  `test_jasna_subs.py::test_restart_takes_a_new_generation_cleans_up_and_sweeps` backdates its
  temporary by 11 minutes because the frozen C3/M12 age gate now keeps fresh temporaries — the
  fifth listed edit to that file (the four D2 edits are listed under "Files to change").
- **Accepted pilot additions:** no fallback lease take while stopping after a failed carried
  transfer (Jasna); `TreePlan.folders` (the temp-sweep scope); a translator that fails to start is
  an avsubs launch error; a `no LLM key` translator result also raises the once-per-run key alert;
  the avsubs test harness sets `avsubs_gpu_monitor=False` (no real `nvidia-smi` in tests).
