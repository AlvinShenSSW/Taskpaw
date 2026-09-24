# #177 — Jasna「AV 翻译」tick box: WhisperJAV ASR + global-LLM ja→zh after each restore; shared `subs` package; version 3.4.0

Date: 2026-09-24 (design v6 — debate rounds 1–5: D1–D33 folded in)
Issue: #177 (depends on #178 — merged as 5da29c1 — and #181; #179 consumes this package)
Driver: `/afk` (Claude Fable 5.1 leads; Opus subagents implement → internal review → Codex 外门
`gpt-6-astra` effort high → Kimi 终审). Merge policy: leave-open (owner merges).
Upstream: `docs/specs/2026-09-24-jasna-av-translate-spec-review.md` (§4.2 translation contract,
§4.4 shared package + settlement/stop/identity rules, §5.B, §11 four Codex rounds, **§12 prototype
precheck of WhisperJAV 1.9.3**), `docs/specs/2026-09-24-178-llm-settings-design.md` (the
`llm-worker` cancel contract this issue consumes), `docs/specs/2026-09-18-173-jasna-task-design.md`.

## Spec review

Tick「AV 翻译」on a Jasna task and every restored film gets `<stem>_restored.srt` (Simplified
Chinese) and `<stem>_restored.ja.srt` (Japanese) next to `<stem>_restored.mp4`. The pipeline is the
industry contact's: VAD-first ASR (WhisperJAV, the JAV-tuned Whisper pipeline — an external tool
driven like `jasna.exe`), then ja→zh through the agent-level LLM setting (#178; the owner runs xAI
`grok-4.3`). GPU work stays serial (restore → ASR → next restore); translation runs in a background
thread that only talks to the terminable `llm-worker` child process, so `stop()` can end it inside
the supervisor's shared 5 s budget.

What makes it more than a tick box: the Jasna plugin's exit/stop/restart branches are restore-only
today (`_handle_exit`, `stop()` publishing any rc 0, `start()` cleaning only when `_process` exists,
`_advance()` emitting `done` when `_pending` is empty, the abort short-circuit before any poll) and
the UI's Stop/Start re-creates the instance object — so the design keeps ASR and translation in
their own objects, gives every planned subtitle job a terminal state settled by the worker thread
exactly once, and keys results/temp files by a process-wide run generation.

Ambiguities settled here: **ASR runs on the published `<stem>_restored.mp4`** (C10 — it exists for
every job kind and the subtitle is then timed to the file it accompanies); subs-only files run
**after** all pending restores; a `.ja.srt` is reused by existence only; a no-speech film gets two
0-byte subtitle files and counts as completed; the LLM key is checked per job at translation time
(live-apply); `suspect` is a success with a detail note; `done` is evaluated at the end of every
`check()` (D1); the packaged worker uses `worker_argv()` and is the first real exercise of the
bundled `llm-worker` role.

## Acceptance criteria

- [ ] AC1 Shared package `taskpaw_v3/monitors/subs/` (`child.py`, `whisperjav.py`, `srt.py`,
  `translate.py`, `job.py`) with the exact APIs below, plus `taskpaw_v3/core/generation.py`; no
  helper is imported from `lada.py`.
- [ ] AC2 `JasnaConfig` gains `av_translate` (default False, title「AV 翻译」), `whisperjav_exe_path`
  (required when ticked), `whisperjav_engine` (five presets, default `anime-whisper`),
  `whisperjav_extra_args` (owned-flag rule incl. argparse prefixes; `--translate*` forbidden, D14);
  no LLM fields; `ui:order` and zh/en i18n; wizard default unticked.
- [ ] AC3 Two-layer planning: `plan_queue` **unchanged** (signature and values); a separate pure
  `plan_subs` classifies every restorable file as `full / translate_only / none`, excludes
  `plan_queue`'s collision losers (D21), orders `subs_only` after pending restores, and yields
  `subs_total/completed/failed/skipped/remaining`.
- [ ] AC4 Phases and ownership: `_process` is restore-only; `_subs_job.child` is ASR-only;
  `_handle_exit` keeps its single post-lock action, which is now "start subs for this file **or**
  advance" (D3); `_poll_subs()` handles `succeeded / no_speech / failed / unstable`; a restore that
  finally fails settles its subtitle job `skipped(restore_failed)`.
- [ ] AC5 Settlement: every planned job reaches exactly one terminal state; results are only
  submitted by the translator and settled by the monitor worker thread in `check()` under
  `_launch_lock` and `not _stopping`, `_settled` checked **before** any publish (D7); the `done`
  predicate is evaluated at the end of every `check()` and in `_advance()` (D1); Stop/abort never
  emit `done`; a Start with no work at all (nothing pending, nothing to subtitle) emits nothing,
  exactly as today (D20).
- [ ] AC6 Degrade/preflight: 3 consecutive subtitle failures (settlement order) → one alert,
  translator cancelled, unstarted jobs and `subs_only` `skipped(cancelled)`, a live ASR child
  terminated — all blocking work outside `_launch_lock` (D8); restores continue; exe missing →
  one alert, all `skipped(no_exe)`; missing key → per-job `skipped(no_llm_key)`, one alert per run.
- [ ] AC7 Stop/restart: `stop()` cancels the translator (sentinel on its **response** queue → close
  worker stdin → tree kill), terminates any live GPU child tree, publishes a `.ja.srt` for an ASR
  that already exited 0, joins reader and translator threads within one monotonic deadline;
  `start()` takes a new generation, cleans unconditionally, sweeps `*.<gen>.tmp` and
  `.avsubs/tmp`; ASR launch, ja publish and zh publish all happen under `_launch_lock` with a
  post-spawn `_stopping` re-check (D9).
- [ ] AC8 Metrics/events/Hub: `phase ∈ restore | subs | translate`, `subs_*`, `subs_translating`;
  no per-file keys (`current_file`, progress) without a live child (D17); `detail` formats; batch
  `done` text with `Subs: S/T done, U failed, V skipped`; `status_md` appends `subs S/T`; openclaw
  guide rows.
- [ ] AC9 GPU lease hooks: `_gpu_acquire()`/`_gpu_release()` wrap every GPU child (restore and
  ASR) as strict pairs on every exit path; translate-only jobs never acquire (D12) — no-ops here,
  filled by #179.
- [ ] AC10 Owner rules/constitution: `manual_start()` unchanged; no `shell=True`; key never in
  argv/env of the ASR child/logs/events/detail; `start()`/`check()` never raise; all publishes
  `.tmp` + `os.replace`; every thread joined; ASR child gets `stdin=DEVNULL` (D15).
- [ ] AC11 Version 3.3.1 → 3.4.0 (six files); CHANGELOG; README plugin table; openclaw guide.
- [ ] AC12 Tests per the plan (incl. real `unregister/register` + `reconfigure`, real-worker
  hang/drip Stop, immediate re-Start — D11); `uv run pytest`, ruff, mypy, UI lint, vitest green.

## Frozen issue contract

**In scope:** AC1–AC12. Allowed user-visible changes: four new Jasna fields; `.ja.srt`/`.srt`
outputs next to restored videos; a `.avsubs/` staging folder inside the output folder; new metric
keys; `subs …` fragment in `status.md`; About/i18n blurbs; version label 3.4.0.

**Invariants:** constitution §2 (no `shell=True`; secrets never in argv/logs/events; atomic
publish), §4 (no silent except; clean shutdown — every thread joined, no orphan process), §5
(every behavioural change has a test); owner rules (managed Jasna never auto-starts;
`jasna_capture_progress` default false); the existing restore semantics — `queue_*` meaning,
retry/degrade/abort, hev1→hvc1 retag, staging publish — byte-for-byte unchanged when
`av_translate` is off (the existing `test_jasna.py` suite stays green untouched, D19) and
unchanged for restores when it is on; the lock contract in `jasna.py:706-716` (no blocking work
under `_launch_lock` beyond `Popen`, `os.replace`, the retag, and a bounded terminate); the
agent→hub wire shape (only keys added); the lada `status.md` line stays byte-identical.

**Frozen-contract corrections from repository evidence:**

| # | Issue text | Evidence | Correction |
|---|-----------|----------|------------|
| C1 | "复用 lada/jasna 的 reader/terminate 配方" | `lada.py:438` reader is an instance method; `jasna.py:505` kills the direct child only; WhisperJAV is a 3-level tree (§12) | `subs/child.py` has its own readers + `terminate_tree`; nothing imported from `lada.py` |
| C2 | "`_check_managed` 的 abort 短路" | `jasna.py:1081-1083` short-circuits before polling | Restore 3-strike abort first disables subtitles (live ASR terminated, translator cancelled) — the short-circuit never strands a child |
| C3 | Output naming / manifest | §12 | `read_outcome()` takes the srt path from the manifest (`files[0].output`), never from a computed name (D5) |
| C4 | "`chat()` … CancelToken" | #178 shipped a synchronous `chat()` + `llm_worker` | The translator drives the worker child; it never calls `chat()` |
| C5 | LLM default OpenRouter | #181 → xAI; E1 verified JSON mode on `grok-4.3` | No change |
| C6 | translate-only when `.ja.srt` exists | `plan_queue` counts done by the mp4 alone | `plan_subs` classifies per file (`full / translate_only / none`) |
| C7 | Issue item 1: "terminate → wait → `taskkill /T /F`" | critic exp 2: once the launcher has exited, `taskkill /T` returns 128 and the grandchild survives | `terminate_tree` on Windows calls `taskkill /PID <pid> /T /F` **first** (while the tree is intact), then waits; POSIX terminate→kill (D18) |
| C8 | Issue item 3: reserve ja/zh targets, `skipped(collision)` | `plan_queue` already drops a later file whose casefolded `_restored.mp4` path repeats (`jasna.py:228-236`); every subtitle target derives from that same stem with the `_restored` infix, so no two surviving files can share a ja or zh target and no ja target can equal a zh target | No subtitle collision machinery in Jasna (D13); #179's `plan_tree` keeps it (cross-dir, cross-ext) |
| C9 | Issue item 1: `start_asr() -> bool`, `poll_asr() -> Optional[AsrOutcome]` | error text is needed for the alert; the source-identity check yields a fourth outcome | `start_asr() -> Optional[str]`, `poll_asr() -> Optional[JobOutcome]` (D19) |
| C10 | Issue item 4: "ASR 的输入是输入文件夹里的源文件" | A3 (Jasna keeps the timeline) was an unverified guess; the published `_restored.mp4` exists for every job kind (pending files reach subs only after their restore is published; subs-only files are restored by definition) | ASR input = `output_path_for(output_folder, video)`; the subtitle is timed to the file it sits next to; A3 removed |
| C11 | Issue item 6 / metrics `phase: restore \| subs` | while only translations run there is no GPU child; reporting `current_file`/progress then contradicts `jasna.py:1281-1283` (D17) | `phase` gains `translate`; per-file keys only with a live child |

**Non-goals:** #179 (standalone task, GPU lease body); OCR; LLM configuration; retries beyond
those stated; ensemble-specific fields (`--ensemble*` stays allowed in extra args; the manifest
path handles its naming); `.ja.srt`/`.srt` invalidation by source change (conscious decision,
Kimi F3: a re-restore of the same source keeps the same timeline, so existing subtitles stay
valid and an mtime rule would redo about an hour of ASR + LLM per film for nothing; a source
replaced under the same name is handled by deleting its old `.srt` files, which the field text
now says); subtitle editing; lada.

**Causal boundary:** `taskpaw_v3/core/generation.py` (new), `taskpaw_v3/monitors/subs/*` (new),
`taskpaw_v3/monitors/plugins/jasna.py`, `taskpaw_v3/hub/server/status_md.py`,
`taskpaw_v3/ui/src/schemaI18n.ts`, `ui/src/i18n.ts`, `ui/src/test/wizard.test.tsx`, tests
(`test_subs_*.py`, `test_generation.py` new; `test_jasna.py`, `test_status_md.py`,
`test_catalog.py`), six version files, `CHANGELOG.md`, `README.md`,
`docs/guides/openclaw-integration.md`, this doc.

## Assumptions (unverified claims are listed as such)

| # | Claim | Basis | Risk if wrong |
|---|-------|-------|---------------|
| A1 | WhisperJAV 1.9.3 CLI facts (presets, `--language japanese`, output flags, manifest states, rc 0 for `empty`/`suspect`, prefixes accepted, 3-level tree) | **Verified** on the prototype (§12); the critic re-derived the prefix rule against the real 179-flag list from `main.py` (no non-owned flag is caught) | — |
| A2 | xAI `grok-4.3` honours `response_format json_object` and returns `{"<id>": "<zh>"}` | **Verified** live (E1) | other providers: content validation → `failed` per file |
| A3′ | Jasna's `_restored.mp4` carries the source audio stream (C10 runs ASR on it) | **Verified** on the prototype (2026-09-24): three real Jasna outputs in `C:\OUTPUT` (`SSNI-012-C`, `-033-C`, `-056-C`) each carry `aac` stereo 48 kHz with the same duration as the `hevc` video; the sources in `C:\TODO` are `h264` + `aac` stereo | — (the one-line `media()` fallback to the source stays available) |
| A4 | Packaged `taskpaw-backend llm-worker` starts and answers | not exercised yet | `Popen`/protocol failure → `network` → per-file `failed` with an alert; restores unaffected |
| A5 | `taskkill /PID <pid> /T /F` ends a live WhisperJAV tree | **Verified** (critic exp 2: 0.27 s, grandchild gone) — and **only** while the launcher is alive (C7) | — |
| A6 | `no_speech` ⇔ manifest `empty` | **Verified** (§12 + `run_outcome.py` docstring) | — |
| A7 | `--temp-dir` redirects the qwen pipeline's audio/scenes/raw_subs; the (off by default) enhancer/nemo backends still use the system temp | **Verified from source** (critic) | leftovers in `%TEMP%` only with those optional backends |
| A8 | Throughput 5–10 min per hour of film on the RTX 5060 | README + §12 | throughput only |
| A9 | Packaged worker first spawn ≤ 2 s | inherited from #178 | latency only |
| A10 | WhisperJAV's GPU preflight only prompts when stdin is a tty | critic: `utils/preflight_check.py:855-870` (source read) | `stdin=DEVNULL` makes it moot |

## Approach

**Package boundaries.** Everything engine-related lives in `taskpaw_v3/monitors/subs/` and knows
nothing about Jasna: paths in, outcomes out. The Jasna plugin owns planning, policy (retry,
degrade, abort), counters, events and publishing of the final files. #179 reuses the package and
copies none of it.

**Two child kinds, one helper.** `ChildProcess` wraps `Popen` for the ASR child (stdout+stderr
merged into a bounded tail, `stdin=DEVNULL`) and for the LLM worker (stdin pipe, stdout lines
delivered to a per-child queue, stderr into the tail). It owns the reader threads, the tail,
`write_line`, `close_stdin`, and `terminate_tree` (Windows: `taskkill /PID <pid> /T /F` first).

**Translator = client of the worker.** One daemon thread per instance run. It never writes a
subtitle file: it returns `TranslateResult`s on a queue that the monitor worker thread drains in
`check()`. Each spawned worker gets its **own** response queue (D4); cancel = set the flag, put a
sentinel on the **current worker's response queue** (D6), close the worker's stdin (its EOF
watcher `os._exit`s even mid-HTTP, #178), tree-kill as fallback, put a sentinel on the request
queue — then the thread is joinable within the stop budget. Spawn and cancel serialise on an
internal lock with a post-spawn cancel re-check.

**Run identity.** `core/generation.py` hands out a process-wide monotonic integer; `RunId =
(instance_id, generation)`. Results and temp-file names carry it.

**Settlement is single-writer.** Only the monitor worker thread (inside `check()`) changes
counters, emits events and publishes `.srt` files; it does so under `_launch_lock`, only while
`not _stopping`, and only for jobs not yet in `_settled`. Blocking side effects of a settlement
(cancelling the translator, terminating an ASR child, launching the next GPU child) are deferred
to after the lock is released (D8).

### Module design

#### `taskpaw_v3/core/generation.py`

```
_lock = threading.Lock(); _last = 0
def next_generation() -> int   # 1, 2, 3, … never reused within the process
```

#### `taskpaw_v3/monitors/subs/child.py`

```
class Eof:            # sentinel type delivered on line_sink at stdout EOF
    pid: int          # the child it belongs to (D4)
class ChildProcess:
    def __init__(self, argv: list[str], *, env: Optional[Mapping[str, str]] = None,
                 cwd: Optional[str] = None, stdin_pipe: bool = False,
                 line_sink: Optional["queue.Queue[object]"] = None, tail_lines: int = 40) -> None
    # Popen(list argv, shell=False, stdin=PIPE if stdin_pipe else DEVNULL (D15), stdout=PIPE,
    #   stderr=STDOUT if line_sink is None else PIPE, bufsize=0, env=env, cwd=cwd,
    #   creationflags=CREATE_NO_WINDOW on win32). Popen errors propagate (callers record them).
    pid: int; proc: subprocess.Popen
    def poll(self) -> Optional[int]
    def write_line(self, line: str) -> None     # UTF-8 + "\n", flush; raises OSError when the child is gone (errno 22 / BrokenPipe / closed pipe)
    def close_stdin(self) -> None              # idempotent; swallows OSError
    def tail(self, lines: int = 10, max_chars: int = 800) -> str
    def terminate_tree(self, timeout: float = 5.0) -> None
        # win32: subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], creationflags=CREATE_NO_WINDOW,
        #   capture_output=True, check=False) — rc 0 or 128 (already gone) are fine; then proc.wait(timeout) → kill() → wait(2)
        # POSIX: terminate() → wait(timeout) → kill() → wait(2)
    def join_readers(self, timeout: float = 2.0) -> None
```
Readers: when `line_sink is None`, one thread reads stdout byte-wise splitting on `\r`/`\n` (lada's
tqdm rule) into the tail deque; with a `line_sink`, one thread reads stdout lines
(`readline()`, UTF-8 `errors="replace"`, `rstrip("\r\n")`), puts each non-empty line, then
`Eof(pid)`; a second thread drains stderr into the tail. Threads are daemon, named
`subs-child-<pid>-out/err`. Nothing in `ChildProcess` blocks except `terminate_tree` (bounded).

#### `taskpaw_v3/monitors/subs/whisperjav.py`

```
Engine = Literal["anime-whisper", "large-v3", "large-v2", "qwen3", "custom"]
ENGINES: tuple[Engine, ...]; DEFAULT_ENGINE = "anime-whisper"
PRESETS = {"anime-whisper": ("--mode", "qwen", "--qwen-generator", "anime-whisper"),
           "large-v3": ("--mode", "balanced", "--model", "large-v3"),
           "large-v2": ("--mode", "balanced"), "qwen3": ("--mode", "qwen"), "custom": ()}
OWNED_FLAGS = ("--output-dir", "--output-format", "--language", "--temp-dir", "--no-signature")
PRESET_FLAGS = ("--mode", "--model", "--qwen-generator")
FORBIDDEN_PREFIX = "--translate"     # WhisperJAV's own translation options carry an API key on argv (D14)
def owned_flags_in(extra: str, engine: str) -> list[str]
    # shlex.split(extra); for each token: name = token.split("=", 1)[0]; if not name.startswith("--"): continue
    # protected = OWNED_FLAGS + (PRESET_FLAGS if engine != "custom" else ())
    # rejected when name == flag, or (len(name) >= 4 and flag.startswith(name)) for any protected flag,
    # or name.startswith(FORBIDDEN_PREFIX). Returns the protected/forbidden names hit (for the error).
def build_argv(exe: str, source: Path, out_dir: Path, tmp_dir: Path, engine: str, extra: str) -> list[str]
    # [exe, str(source), *PRESETS[engine], "--language", "japanese", "--output-dir", str(out_dir),
    #  "--output-format", "srt", "--temp-dir", str(tmp_dir), "--no-signature", *shlex.split(extra)]   (§12 verified)
def manifest_path(out_dir: Path) -> Path                       # out_dir / "whisperjav_run.json"
def attempt_dir(staging_root: Path, relpath: str, attempt: int) -> Path
    # staging_root / hashlib.sha1(relpath.encode("utf-8")).hexdigest()[:12] / f"attempt-{attempt}"
@dataclass(frozen=True)
class AsrOutcome: kind: Literal["succeeded", "no_speech", "failed"]; cues: tuple[Cue, ...]; detail: str
def read_outcome(out_dir: Path, rc: Optional[int], tail: str) -> AsrOutcome
    # rc None → failed("no exit code"); rc != 0 → failed(f"exit code {rc}: " + tail)
    # manifest missing / not JSON / no non-empty files[] → failed("no manifest")
    # entry = files[0]; state = entry.get("state"); output = entry.get("output")
    # state == "empty" → no_speech(detail from entry)
    # state in {"done", "suspect"}: output must be a str path that exists → else failed("manifest names no output"/"output missing");
    #   srt.load(output): SrtError → failed("unparseable srt"); 0 cues → no_speech; else succeeded(cues, detail="suspect: <detail>" if suspect else "")
    # any other state → failed(f"manifest state {state}: {detail}" + tail)
```

#### `taskpaw_v3/monitors/subs/srt.py`

```
@dataclass(frozen=True) class Cue: index: int; start_ms: int; end_ms: int; text: str
class SrtError(ValueError)
def parse(text: str) -> list[Cue]      # strict; BOM + CRLF tolerated; blank-line separated blocks;
                                       # "HH:MM:SS,mmm --> HH:MM:SS,mmm" (also "." ms sep); end >= start;
                                       # multi-line text joined with "\n"; empty/whitespace-only input → []
def serialize(cues: Iterable[Cue]) -> str   # renumbers 1..n, "\n" line endings, trailing blank line
def load(path: Path) -> list[Cue]           # utf-8-sig
```

#### `taskpaw_v3/monitors/subs/translate.py`

```
BATCH_SIZE = 40; CONTEXT_SIZE = 5; RESPONSE_DEADLINE_S = 60.0; REQUEST_TIMEOUT_S = 30.0
SYSTEM_PROMPT = (the E1-tested text: ja→zh colloquial Simplified Chinese; JSON protocol; ○ restored from context; no explanations)
RunId = tuple[str, int]
@dataclass(frozen=True) class TranslateRequest: run: RunId; job_id: str; cues: tuple[Cue, ...]
@dataclass(frozen=True) class TranslateResult: run: RunId; job_id: str; outcome: Literal["translated", "failed"]; zh_cues: tuple[Cue, ...]; detail: str
CANCELLED = object()   # put on `results` by cancel() so a draining worker thread can observe it
class Translator:
    def __init__(self, run: RunId, *, name: str, spawn: Callable[..., ChildProcess] = ChildProcess,
                 settings_fn = get_llm_settings, worker_argv_fn = worker_argv, job_fn = assign_kill_on_close_job,
                 deadline_s: float = RESPONSE_DEADLINE_S) -> None
    def start(self) -> None                       # daemon thread "subs-translate-<name>"
    def submit(self, req: TranslateRequest) -> None
    results: "queue.Queue[TranslateResult | object]"
    def queued(self) -> int                       # 0 after cancel (D7)
    def in_flight(self) -> bool                   # False after cancel (D7)
    def cancel(self) -> None                      # idempotent; see below
    def join(self, timeout: float) -> None; def is_alive(self) -> bool
```
Worker handle: `_worker: Optional[tuple[ChildProcess, "queue.Queue[object]", JobKeeper | None, str]]`
= (child, its own response queue, job keeper, the key it was spawned with). `_spawn_lock`
serialises `_ensure_worker()` and `cancel()`.

`_ensure_worker(settings)`: under `_spawn_lock`: if cancelled → raise `_Cancelled`; if a worker
exists and its key == `settings.api_key` → return it; else **detach** the old one (`_worker = None`)
and release the lock; tear the old one down outside it with short bounds (`close_stdin`,
`wait(0.5)`, `terminate_tree(1.0)`, `join_readers(0.5)`, keeper.close() — D24); re-acquire the
lock, re-check cancel, and spawn a fresh one: `lines = queue.Queue()`; `child =
spawn(worker_argv_fn(), env=worker_env(settings), stdin_pipe=True, line_sink=lines)`; keeper =
`job_fn(child.proc)`; **re-check cancel after spawn** — if cancelled, kill it and raise
`_Cancelled` (D6). The lock is therefore only ever held for bookkeeping and the `Popen` itself. Old queues are dropped with the old worker, so a stale `Eof`
can never reach a new request (D4). A `spawn` that raises → `_WorkerError("spawn: <TypeName>")`
(network kind for the batch).

Thread loop: `req = requests.get()`; `None` or cancelled → exit. Per request: `settings =
settings_fn()`; empty key with a non-loopback base → `failed("no LLM key")`. Batches of
`BATCH_SIZE` cues with the preceding `CONTEXT_SIZE` cues as `context`; request line `{"id":
"<job_id>#<batch>#<try>", "messages": [system, user(json)], "temperature": 0.3, "max_tokens":
min(4096, 64 + 8 * total_ja_chars), "json_mode": true, "timeout": 30, "api_base":
settings.api_base, "model": settings.model}`; `child.write_line` (OSError → worker gone → drop it
→ `network`); then `lines.get(timeout=deadline_s)`: `CANCELLED`/cancel flag → exit; `Eof` (whose pid
== child.pid; others ignored) → worker died → drop worker → `network`; timeout → `terminate_tree`,
drop worker → `network`; a reply whose `id` mismatches → ignored, keep waiting on the same
deadline (re-armed once). Reply handling: `ok: false` → its `kind`; `ok: true` → content-layer
validation: `json.loads(content, object_pairs_hook=_no_dupes)` (duplicate key → invalid), an
object, key set == the batch's ids as strings, every value a non-empty `str` after strip → zh
cues rebuilt from the original cue index/timing. Retry policy (spec review §4.2): content invalid,
`rate_limit`, `network`, `bad_response` → split the batch in two halves, each tried once more
(recursion depth 1; a half that fails again fails the file); `auth`, `refusal`, `no LLM key` →
the file fails at once. Each batch is logged as kind/latency only. A file yields one
`TranslateResult`.

`cancel()`: set the flag; under `_spawn_lock`: if a worker exists → detach it and `lines.put(CANCELLED)`
on **its** queue (D6); release; then `close_stdin()`, `wait(1.0)` best effort, `terminate_tree(1.0)`,
`join_readers(0.5)`, keeper.close() (D24: explicit and bounded — about 1–2.6 s in practice;
≈ 5.5 s only if the worker survives both stdin EOF and `taskkill /F`, which no live process
does); then
`requests.put(None)`; `results.put(CANCELLED)`. After cancel, `queued()`/`in_flight()` return 0/False
(D7). `join(timeout)` joins the thread. A worker detached by `_ensure_worker` at that moment is torn
down by `_ensure_worker`'s own path, which then sees the cancel flag and never spawns.

#### `taskpaw_v3/monitors/subs/job.py`

```
Terminal = Literal["completed", "failed", "skipped"]
SkipReason = Literal["restore_failed", "no_llm_key", "unstable", "cancelled", "no_exe"]
def source_identity(path: Path) -> tuple[int, int]     # (size, mtime_ns); raises OSError
@dataclass(frozen=True)
class JobOutcome: kind: Literal["succeeded", "no_speech", "failed", "unstable"]; cues: tuple[Cue, ...]; detail: str
@dataclass
class SubsJob:
    run: RunId; job_id: str; media: Path; relpath: str; ja_target: Path; zh_target: Path
    staging_root: Path; exe: str; engine: str; extra: str
    identity: Optional[tuple[int, int]] = None; attempt: int = 0; child: Optional[ChildProcess] = None; started_at: float = 0.0
    def start_asr(self, spawn: Callable[..., ChildProcess] = ChildProcess) -> Optional[str]
        # identity = source_identity(media) (first call records; later calls compare → "unstable";
        #   OSError — the media was removed — → "unstable" as well);
        # attempt += 1; d = attempt_dir(staging_root, relpath, attempt); shutil.rmtree(d, ignore_errors=True); d.mkdir(parents=True)
        # argv = build_argv(exe, media, d, staging_root / "tmp", engine, extra); child = spawn(argv)
        # returns None, "unstable", or the spawn error text ("launch: <TypeName>: <msg>") — never raises
    def poll_asr(self) -> Optional[JobOutcome]
        # None while child.poll() is None; else child.join_readers(1.0); identity changed → JobOutcome("unstable");
        # read_outcome(attempt dir, rc, child.tail()) mapped 1:1; child = None
    def publish_ja(self, cues) -> Optional[str]      # serialize → ja_target.with_name(f"{ja_target.name}.{run[1]}.tmp") → os.replace; error text or None
    def publish_zh(self, cues) -> Optional[str]      # same for zh_target
    def publish_empty(self) -> Optional[str]         # 0-byte ja + zh (no_speech)
    def load_ja(self) -> list[Cue]                    # srt.load(ja_target)
    def terminate(self, timeout: float = 5.0) -> None  # child.terminate_tree + join_readers; child = None
```
No retry policy and no counters live here.

#### `taskpaw_v3/monitors/plugins/jasna.py`

Config (after `unet4x_4k` in `ui:order`):
```
av_translate: bool = False                      # 「AV 翻译」
whisperjav_exe_path: str = ""                   # file picker; required when av_translate
whisperjav_engine: Literal[ENGINES] = "anime-whisper"
whisperjav_extra_args: str = ""                 # owned_flags_in(extra, engine) must be empty
```
Validator additions: `av_translate and not whisperjav_exe_path.strip()` → error; owned/forbidden
flags → error naming them.

Constants: `_SUBS_STAGING = ".avsubs"` (D16) — `staging_root = <output_folder>/.avsubs`; `attempt`
dirs under it; `.avsubs/tmp` is WhisperJAV's `--temp-dir`. `plan_queue` never sees it (it scans the
input folder, non-recursively) and `sweep_orphan_staging` only looks at files (critic, D16).

Pure planning (D19, C6, C8, C10, D21): `plan_subs(input_folder: str, output_folder: str, pending:
list[Path], excluded: Iterable[Path]) -> SubsPlan` — `excluded` is the list of losers from
`plan_queue`'s `collisions` (`[loser for loser, _ in collisions]`; a loser's `output_path_for()` is
the winner's file, so without this it would become a duplicate `subs_only` job on the same media
and the same targets — D21). It scans the input folder exactly like `plan_queue` (same extension
set, sorted), skips the files in `pending` and `excluded`, and for every other video whose
`output_path_for()` exists classifies it; for `pending` files it records the kind they will need
once restored:
```
kind(video): zh = zh_target(video); ja = ja_target(video)
  zh.exists() → "none" (a 0-byte zh counts) ; ja.exists() → "translate_only" ; else "full"
SubsPlan(for_pending: dict[Path, kind], subs_only: list[Path] (restored files with kind != "none", sorted), total: int)
  total = |{v in for_pending: kind != none}| + |subs_only|
ja_target(video) = <out>/<stem>_restored.ja.srt ; zh_target(video) = <out>/<stem>_restored.srt
media(video) = output_path_for(output_folder, video)   # C10: ASR input is the restored file
```

Instance state added: `_run: RunId`, `_phase: "restore" | "subs" | "translate"`, `_plan: SubsPlan`,
`_subs_only: list[Path]`, `_subs_job: Optional[SubsJob]`, `_translator: Optional[Translator]`,
`_jobs: dict[str, SubsJob]` (job_id = video name), `_settled: set[str]`, `_subs_completed/_failed/
_skipped`, `_subs_consecutive_failures`, `_subs_disabled: Optional[str]`, `_subs_key_alerted`,
`_next_action: Optional[str]` (D3), `_deferred: list[Callable]` (D8), `_advance_requested: bool`
(D25 — the single consumable advance request), `_had_work: bool` (D20), `_spawn` (ChildProcess
factory; tests inject a fake, D10).

Flow (managed):
- `start()`: if any of `_process`, `_subs_job`, `_translator` is live → `stop()`; `_run = (iid,
  next_generation())`; reset everything incl. subs state; sweep `<out>/*_restored*.srt.*.tmp` and
  `<out>/.avsubs/tmp` (best effort); `_start_managed`: existing preflight; if `av_translate`:
  exe preflight (missing/dir → one alert `f"{iid}:subs-noexe"`, `_subs_disabled = "no_exe"`);
  `plan_subs`; create `_jobs` for every file needing work; `_translator = Translator(...)`.start()
  unless disabled; if disabled → settle every job `skipped(no_exe)`. Then — **D2** — when
  `_pending` is empty: if `av_translate` and `_subs_only` → `_advance_requested = True;
  _dispatch()` (the loop, so a 1500-file translate-only backlog at Start is walked iteratively —
  D29); else the existing idle note (`nothing to process (N already restored)`).
- Restore success: `_handle_success` (under the lock, as today) after the existing counters sets
  `_next_action = "subs"` if subs enabled and `_plan.for_pending.get(video)` needs work, else
  `"advance"` (D3). `_handle_exit` keeps its shape: after releasing `_launch_lock` it performs
  **exactly one** action: `"subs"` → `_start_subs(video)`, `"advance"` (or after a failure) →
  `_advance_requested = True`; then, **after either action**, `_run_deferred()` and `_dispatch()`
  (deferral + single-dispatch rules below — D31: `_start_subs` never dispatches, so a
  translate-only file that settles synchronously, e.g. `skipped(no_llm_key)`, leaves its request
  for this `_dispatch()` to launch the next pending restore). The existing unconditional trailing
  `_advance()` is replaced by this; a restore failure sets `"advance"`.
- `_start_subs(video)` — always called from a dispatching context (`_handle_exit`'s post-lock
  section or `_advance()` inside `_dispatch()`'s loop) and therefore **never dispatches itself**
  (D29): job = `_jobs[name]`; translate_only → under `_launch_lock`: `_submit_translation(job)`
  (no GPU) and `_advance_requested = True`; full → `_gpu_acquire()`; `release_gpu = False`; under
  `_launch_lock`: `if _stopping: release_gpu = True` (no bare `return` inside the `with` — the
  post-lock section always runs, D30) else: `err = job.start_asr(self._spawn)`; re-check
  `_stopping` after spawn → `job.terminate()`, `release_gpu = True` (D9); `err is None` →
  `_phase = "subs"`, `_subs_job = job`; `"unstable"` → settle `skipped(unstable)` + alert,
  `release_gpu = True`, `_advance_requested = True`; other error → counts as a failed attempt
  (`attempt < 2` → `start_asr` again inside the same lock hold) → on the second error settle
  `failed` + alert, `release_gpu = True`, `_advance_requested = True`. **After the lock** (D27):
  `if release_gpu: _gpu_release()`; `_run_deferred()`. The caller's `_dispatch()` loop consumes
  any request. Nothing in the terminal branches calls `_advance()` or a closure under the lock.
- `_check_managed`: (1) `_settle_results()`; (2) `_launch_error` → error; (3) `_batch_aborted` →
  degraded (subs were disabled by `_fail_current`, and the deferred cancel/terminate already ran
  inside that same `_handle_exit` — see the deferral rule); (4) restore child poll as today;
  (5) `_poll_subs()`; (6) `_maybe_done()` (D1); (7) status.
  **Deferral rule (D8/D22):** every method that acquires `_launch_lock` and may queue deferred
  work — `_settle_results` (per result), `_handle_exit`, `_poll_subs`, `_start_subs` — calls
  `_run_deferred()` itself immediately after releasing the lock, never as a later check step, so
  an abort's early `return degraded` (`jasna.py:1092-1093`) and a `_poll_subs` in the same check
  can neither skip nor overtake it. `_settle_results`, `_handle_exit` and `_poll_subs` then call
  `_dispatch()`; `_start_subs` does not (it runs inside its caller's dispatch, D29).
  `_run_deferred()` pops and runs each closure; closures capture the job object they were queued
  for and are no-ops when it has already been **reaped** (`job.child is None` — D26: an
  exited-but-unpolled child is *not* reaped and is still terminated, joined and released by the
  closure).
  **Single-dispatch rule (D25/D29):** nothing but `_dispatch()` calls `_advance()` on these paths.
  Terminal branches and closures only set `_advance_requested = True`; `_dispatch()` is a
  **loop**, not a call: `while self._advance_requested: self._advance_requested = False;
  self._advance(emit)`. A subs-only file that finishes synchronously inside `_start_subs`
  (translate-only, `skipped(no_llm_key)`, `"unstable"`, launch error) sets the request and returns
  to the loop, so 1500 such files are walked iteratively on one frame — never
  `_advance → _start_subs → _dispatch → _advance …` (D29). The loop ends when an `_advance` launches
  a GPU child, finds nothing to do (`_maybe_done`), or is stopped/aborted. `_handle_exit`'s
  existing post-lock `_advance()` becomes `_dispatch()` with the request set by the `"advance"`
  action (D3). As a safety net, `_advance()` returns at once while a GPU child is live
  (`_process is not None`, or `_subs_job` with `child is not None`) — `_handle_exit` already clears
  `_process` before advancing (`jasna.py:1109`), so the existing restore flow is unaffected.
- `_poll_subs()`: under `_launch_lock` (D9): if `_subs_disabled` or the job is in `_settled` →
  return (the deferred terminate owns that job, D22); outcome = `_subs_job.poll_asr()`; None → return;
  `succeeded` → `publish_ja` (error → settle `failed`) → `_submit_translation(job)`; `no_speech` →
  `publish_empty` → settle `completed("no speech")`; `unstable` → settle `skipped(unstable)` +
  alert; `failed` → `attempt < 2` → `err = job.start_asr(self._spawn)` again (same job, new
  attempt dir, still under the lock, post-spawn `_stopping` re-check): `err is None` → the job
  stays current, nothing is flagged, fall through to the post-lock section (which then does
  nothing); `"unstable"` → settle `skipped(unstable)` + alert; any other error → settle
  `failed("launch: …")` + alert — both continue into the terminal handling below exactly like a
  final failure (D33); else settle `failed(detail)` + alert
  (`f"{iid}:subs:{name}"`, bounded tail, never argv). Then `_subs_job = None`, `release_gpu =
  True`, `_phase = "translate"` if translations pending else `"restore"`, `_advance_requested =
  True` (D28); **after the lock**: `if release_gpu: _gpu_release()`; `_run_deferred()`;
  `_dispatch()`.
- `_submit_translation(job)`: `s = get_llm_settings()`; empty key and non-loopback base → settle
  `skipped(no_llm_key)` (+ one alert per run `f"{iid}:subs-nokey"`); else cues from the just
  published ja or `job.load_ja()` (`SrtError` → settle `failed("unreadable .ja.srt")`) →
  `_translator.submit(TranslateRequest(run, job_id, cues))`.
- `_settle_results()`: loop `results.get_nowait()`: `CANCELLED` → continue; `r.run != _run` → drop;
  under `_launch_lock`: `if _stopping: return`; `if r.job_id in _settled: continue` (D7);
  `translated` → `job.publish_zh` → settle `completed` or `failed(publish error)`; `failed` → settle
  `failed(detail)` + alert. Lock released between results.
- `_settle(job_id, terminal, reason_or_detail)`: no-op if already settled; counters;
  `_subs_consecutive_failures` = 0 on `completed` or `skipped(no_llm_key)`, +1 on `failed`,
  unchanged on other skips; ≥ 3 and not disabled → `_disable_subs("3 consecutive failures")`.
- `_disable_subs(reason)` (may run under `_launch_lock`): `_subs_disabled = reason`; alert once
  (`f"{iid}:subs-disabled"`); settle every non-terminal job `skipped(cancelled)` (queued, in-flight,
  unstarted, `_subs_only`); `_subs_only = []`; then **defer** (D8) the blocking part to
  `_deferred`: `translator.cancel()`, and — with `job = self._subs_job` captured (may be None) — if
  `job is not None and job.child is not None` (not yet reaped, whether live or exited-unpolled —
  D26): `job.terminate()` (terminate_tree + join_readers; harmless on an exited child, C7),
  `_gpu_release()` once, and if `self._subs_job is job`: `_subs_job = None`, `_phase = "restore"`,
  `_advance_requested = True` (D25: a request, never a direct `_advance()`). The caller's post-lock
  `_run_deferred()` + `_dispatch()` execute them (deferral rule).
- Restore failure (`_fail_current`) → settle that file's job `skipped(restore_failed)` (if it
  needed work). Restore 3-strike abort: `_fail_current` calls `_disable_subs("batch aborted")`
  before setting `_batch_aborted` (C2); its deferred part runs inside that same `_handle_exit`
  right after the lock is released (deferral rule), before `_check_managed` returns `degraded`.
- `_advance()` (never under `_launch_lock`; reached only through `_dispatch()`'s loop): if
  aborted/stopping → return; if a GPU child is live (`_process is not None`
  or `_subs_job is not None and _subs_job.child is not None`) → return (D25 safety net); if
  `_pending` → `_launch_next` (phase `restore`); elif `_subs_only` and subs enabled → pop and
  `_start_subs()`; else `_maybe_done()`.
- `_maybe_done()` (D1): if `_had_work` (set in `_start_managed` when `_pending` is non-empty or
  at least one subs job was planned — a zero-work Start stays silent exactly as today, D20) and not
  emitted and restore side finished (`_pending` empty, `_process is None`) and (`not av_translate`
  or (all jobs terminal ∧ `_subs_job is None` ∧ `_subs_only` empty ∧ translator queued 0 ∧ not
  in_flight ∧ results empty)) → emit `done` once: `"Jasna processing
  complete | Queue: X/Y done, F failed | Subs: S/T done, U failed, V skipped | <ts>"` (the `Subs:`
  part only when `av_translate`); `_phase = "restore"`.
- `stop(timeout)`: `_stopping.set()`; `_translator.cancel()` first (non-blocking beyond ≤ 1 s + a
  bounded tree kill); timed `_launch_lock` acquire as today; live restore child →
  `_terminate_child` + delete staging; restore child rc 0 unpolled → publish (C-4, unchanged);
  live ASR child → `_subs_job.terminate()` → `_gpu_release()`; ASR child rc 0 unpolled →
  `poll_asr()` and, if `succeeded`, `publish_ja` only; release lock; join reader (2 s),
  `_translator.join(remaining)`; never emits.
- `_build_status`: existing keys; `phase`; when `av_translate`: `subs_total`, `subs_completed`,
  `subs_failed`, `subs_skipped`, `subs_remaining`, `subs_translating = queued + in_flight`. Per-file
  keys (`current_file`, progress) only when a child is live (D17): restore child → as today; ASR
  child → `current_file` = the media name, no progress keys. State: `running` while a child is
  live or translations are pending; `idle` when nothing is left; existing `error`/`degraded`.
  `detail`: phase subs → `subtitling: <file> [<engine>] · MM:SS elapsed · translating N · subs S/T`;
  phase translate → `translating N · subs S/T`; phase restore → existing head (+ ` · translating N`
  when > 0).
- GPU hooks: `_gpu_acquire() -> bool: return True`, `_gpu_release() -> None: pass`; strict pairs:
  restore launch ↔ restore exit branch / Popen failure / stop; ASR launch ↔ every `_poll_subs`
  terminal branch / start error / stop / disable (D12). Translate-only never acquires.

Dedupe keys: `f"{iid}:subs:{name}"`, `f"{iid}:subs-disabled"`, `f"{iid}:subs-noexe"`,
`f"{iid}:subs-nokey"`, `f"{iid}:subs-unstable:{name}"`.

#### Hub / UI / docs

- `status_md.py`: inside the lada/jasna block, after `lada_parts` (unchanged), a separate part when
  `_is_num(m.get("subs_total"))`: `f"subs {int(subs_completed or 0)}/{int(subs_total)}"` (+ `f" ({n}
  failed)"` when `subs_failed` > 0).
- `docs/guides/openclaw-integration.md`: rows for `subs_total/completed/failed/skipped/remaining`,
  `subs_translating`, `phase` (jasna); note that `.srt` files land next to the restored video.
- `schemaI18n.ts` jasna block: four entries (zh titles/descriptions; `whisperjav_engine` lists the
  presets and the §12 finding; `whisperjav_extra_args` lists the owned flags, the prefix rule and
  the `--translate*` ban; `av_translate` explains outputs, ordering, `.ja.srt` reuse, `.avsubs`).
- `i18n.ts`: `services.jasna` (en/zh) mentions AV 翻译; About blurbs mention subtitles.
- `wizard.test.tsx`: fake jasna schema gains `av_translate` (boolean, default false); assert it
  renders unticked.
- README plugin table row; CHANGELOG `## V3 3.4.0 — Jasna「AV 翻译」(#177)`.

## Files to change

| Path | Change | Reason |
|------|--------|--------|
| `taskpaw_v3/core/generation.py` | create | process-wide run generation |
| `taskpaw_v3/monitors/subs/__init__.py`, `child.py`, `whisperjav.py`, `srt.py`, `translate.py`, `job.py` | create | shared engine (AC1) |
| `taskpaw_v3/monitors/plugins/jasna.py` | modify | config, `plan_subs`, phases, settlement, deferred actions, stop/start, status, GPU hooks |
| `taskpaw_v3/hub/server/status_md.py` | modify | `subs S/T` part |
| `taskpaw_v3/ui/src/schemaI18n.ts`, `i18n.ts`, `test/wizard.test.tsx` | modify | fields, blurbs, default |
| `taskpaw_v3/tests/test_generation.py`, `test_subs_child.py`, `test_subs_whisperjav.py`, `test_subs_srt.py`, `test_subs_translate.py`, `test_subs_job.py` | create | package tests |
| `taskpaw_v3/tests/test_jasna.py` (append only), `test_jasna_subs.py` (new), `test_status_md.py`, `test_catalog.py` | modify/create | integration, render, catalog |
| six version files, `CHANGELOG.md`, `README.md`, `docs/guides/openclaw-integration.md` | modify | 3.4.0, docs |
| `docs/specs/2026-09-24-177-jasna-av-translate-design.md` | create | this doc |

## Execution surface

Writes: the files above only. Reads/executes: `uv run pytest`, `uv run ruff check .`, `uv run ruff
format --check taskpaw_v3 tests scripts`, `uv run mypy`, `cd taskpaw_v3/ui && npm run lint && npx
vitest run`. Tests never run `jasna.exe`, `whisperjav.exe`, or the network: Jasna's own `jasna.exe`
fake stays at the `subprocess.Popen` level (existing harness); the ASR child and the LLM worker in
Jasna tests are `ChildProcess` fakes injected through `JasnaInstance._spawn` /
`Translator(spawn=…)` (D10 — a Jasna test never reaches `taskkill`); `ChildProcess` tests use real
`sys.executable -c` children; one translator integration test uses the real `llm_worker` against a
loopback `http.server`; `taskkill` is exercised on Windows only against a `sys.executable` tree.
No default ports; no real key (the #178 autouse fixture clears `TASKPAW_LLM_*`).

## Key implementation notes

- Lock order stays `_launch_lock` → `_lock`; the translator thread takes neither. Blocking work
  never runs under `_launch_lock`: cancel/terminate/advance triggered by a settlement go through
  `_deferred` (D8), and every lock holder runs `_run_deferred()` right after its own release
  (deferral rule) — `_handle_exit` does so before the caller can return `degraded`;
  `_launch_next`'s probe stays outside the lock as today.
- `_had_work` gates `done` (D20); `plan_subs` receives `plan_queue`'s collision losers (D21).
- `_handle_exit` shape after the change: `with _launch_lock: … _handle_success/_handle_failure …
  action = self._next_action; self._next_action = None` then, outside the lock, `if action ==
  "subs": self._start_subs(video) else: self._advance(emit)`.
- `ChildProcess.terminate_tree` on Windows must call `taskkill` with a list argv and
  `CREATE_NO_WINDOW`; rc 128 is not an error; never `shell=True`.
- Publish temp names carry the generation: `<target>.<generation>.tmp`; `start()` sweeps
  `glob("*_restored*.srt.*.tmp")` in the output folder and `rmtree(.avsubs/tmp, ignore_errors)`.
- 0-byte `.srt`/`.ja.srt` are legitimate outputs (no_speech); `plan_subs` treats an existing
  0-byte zh as done.
- The engine description in `schemaI18n.ts` must say `custom` frees `--mode/--model/--qwen-generator`
  and that `--translate*` options are always rejected (the key would land on argv).
- Keep `jasna.py` `_split_args`/`_flag_names` for Jasna's own flags; `subs/whisperjav.py` has its
  own `owned_flags_in`; do not cross-import.
- Windows-only tests skip elsewhere with `pytest.mark.skipif(sys.platform != "win32")`.
- mypy covers `taskpaw_v3/monitors/subs/*`; keep `Optional` narrowing explicit.

## Risk assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Packaged worker fails to start (A4) | low-med | translations `failed` with alerts, restores unaffected | alert names the worker; owner smoke |
| Provider rejects JSON mode (non-xAI) | low | file fails | half-batch retry then per-file failure; `.ja.srt` kept |
| Stop budget exceeded by a hung ASR child | low | `taskkill /T` is bounded | `terminate_tree(timeout)` then join with the remaining budget |
| `taskkill` when the launcher already exited (C7) | low | grandchild survives holding the GPU | `poll_asr` reaps normally-exited trees; only a crashed launcher with a live worker is affected — surfaced by the next launch failing |
| Generation reuse across process restarts | n/a | temp names collide only within one process | swept at `start()` |

## Out of scope

See non-goals. Also: continuous folder watching; per-file engine choice; subtitle QA.

## Test plan

**`test_generation.py`**: monotonic across 1000 calls from 8 threads; no duplicates.

**`test_subs_child.py`** (real `sys.executable -c` children): tail captures merged stdout/stderr
lines (split on `\r`); `line_sink` receives stdout lines then `Eof(pid)`; `write_line` → child
echoes; `write_line` after the child exited raises OSError; `close_stdin` idempotent; ASR-style
child has `stdin` = DEVNULL (a child that reads stdin gets EOF immediately); `terminate_tree` ends a
live two-level tree (Windows: grandchild sleeping 60 s gone after the call; POSIX: terminate→kill);
`terminate_tree` on an already-exited child returns quietly (rc 128 path); `join_readers` returns
within timeout.

**`test_subs_whisperjav.py`**: five presets' argv exact (incl. `--language japanese`,
`--no-signature`, `--temp-dir`, extra appended last); `owned_flags_in`: exact, `=`, prefixes
(`--out`, `--lang`, `--temp`, `--mod` with anime-whisper; `--mode` allowed under `custom`;
`--sensitivity`, `--vad-version`, `--qwen-segmenter`, `--fail-on`, `--ensemble` allowed;
`--translate`, `--translate-api-key`, `--translate=...` rejected); `attempt_dir` hashing;
`read_outcome` matrix with recorded manifests: done + output srt → succeeded; suspect → succeeded
with detail; empty → no_speech; done with an existing 0-cue srt → no_speech; done whose `output`
is missing on disk → failed; done with an ensemble-style `output` name → succeeded (path taken from
the manifest); failed/skipped state → failed; missing manifest → failed; rc ≠ 0 → failed with tail;
unparseable srt → failed.

**`test_subs_srt.py`**: parse the §12 sample (7 cues, CJK), CRLF, BOM, `.` millisecond separator,
multi-line text, empty → []; strictness (bad timestamp, end < start, non-integer index → SrtError);
serialize renumbers and round-trips; `load` with utf-8-sig.

**`test_subs_translate.py`** (fake worker via injected `spawn` speaking the protocol; injected
`settings_fn`/`worker_argv_fn`/`job_fn`): batch shaping (40 + 5 context, ids as strings, system
prompt, `json_mode`, `max_tokens` bound, `api_base/model` from settings); key change → old worker
closed (stdin closed before kill) and a new one spawned with the new env; **a stale `Eof` from the
old worker never affects the next request** (D4: the fake old worker emits `Eof` after being
killed); success → `translated` with rebuilt timings; content failures (duplicate key, missing id,
extra id, empty value, non-string) → half-batch retry → success / second failure → `failed`;
`rate_limit`/`network`/`bad_response` → retry policy; `auth`/`refusal` → immediate `failed`;
response timeout → `terminate_tree` + `network` + fresh worker on the next batch; `Eof` → `network`;
`write_line` OSError → `network`; mismatched reply id ignored; `spawn` raising → `network`;
`cancel()` while waiting → sentinel on the current worker's queue, thread joins ≤ 1 s, no respawn,
stdin closed before kill, `queued()==0`, `in_flight()==False`, `CANCELLED` on results; **cancel
during spawn** (fake `spawn` blocks on an Event until cancel runs) → the new child is killed and the
thread exits; **cancel during an old-worker teardown** (key change; the fake old child ignores
`close_stdin` and only dies on `terminate_tree`) → `cancel()` returns within 3 s, the thread joins,
no new worker is spawned (D24); readers of the cancelled worker are joined (no live
`subs-child-*` thread after `join`); results carry the run id; no key → `failed("no LLM key")`; logs contain kind/latency
only. Integration: the real `llm_worker` (real `worker_argv()`) against a loopback `http.server`
speaking the JSON protocol → end-to-end `translated`; and **Stop while a request hangs** (server
holds the connection) and **Stop during a slow-drip body** → `cancel()` returns and the thread
joins within 2 s in both (D11).

**`test_subs_job.py`**: `start_asr` creates a fresh attempt dir (previous contents removed), builds
argv with the media path, returns None; identity change before start → `"unstable"`; media deleted
before start → `"unstable"`, never raises; spawn error → error text; `poll_asr` maps outcomes and returns `unstable` when the media changed during ASR;
`publish_ja/zh/empty` write via `.<gen>.tmp` + replace; `load_ja` round-trip; `terminate` on a
live/dead child.

**`test_jasna.py`** (existing file **untouched**; new cases in `test_jasna_subs.py` with the same
harness imported plus a `_FakeChild`/`_FakeTranslator` — D10/D19):
- config: defaults; ticked needs exe; owned/forbidden flags incl. prefixes rejected; allowed
  extras pass; schema defaults exposed.
- `plan_subs`: full/translate_only/none per file, `subs_only` order after pending, 0-byte zh counts
  as done, `.avsubs/`/`.srt` never scanned; `a.mkv` + `a.mp4` both restored → exactly one job and
  the collision loser never gets ASR (D21); `plan_queue` untouched (its tests unchanged).
- lifecycle: restore rc0 → `_next_action` dispatch → ASR launched **once** for the same file with
  the exact argv (media = the restored mp4, attempt dir, `--no-signature`) and no second
  `jasna.exe` launch (D3); ASR succeeded → `.ja.srt` published under the lock → translation
  submitted → next restore launched without waiting; translated result → `.srt` published →
  `completed`; a result for a job already settled `skipped(cancelled)` is not published (D7);
  `no_speech` → two empty files → completed; ASR failed → one retry (new attempt dir) → alert +
  `failed`, restore counters unchanged; `unstable` → skipped; restore failure → subs job
  `skipped(restore_failed)`; empty `_pending` with `subs_only` → starts subs-only immediately (D2)
  and never launches jasna; translate_only skips ASR and never acquires the GPU (D12); missing key
  → `skipped(no_llm_key)` + one alert; key appears later → the next job translates; 3 consecutive
  subs failures (mixed ASR/translation, settlement order) → one alert, translator cancelled and
  live ASR terminated **only via the deferred step outside the lock** (assert the lock is not held
  when the fake translator's `cancel` runs), unstarted + `subs_only` skipped(cancelled), restores
  continue and `queue_*` unchanged; restore 3-strike abort disables subs before `degraded` **and**
  the fake translator's `cancel` plus the live ASR fake's terminate run inside that same `check()`
  (D8 — the deferred step cannot be skipped by the `degraded` return); the third consecutive
  failure arriving from a translation result while the ASR child has exited `failed` (attempt 1)
  or `succeeded` in the same check → no relaunch, no ja publish, no second `_gpu_release`, and
  `check()` does not raise (D22); the ASR's second-attempt failure being the third consecutive
  failure while two restores are pending → exactly **one** new `jasna.exe` launch (D25); a
  translation result being the third failure while the ASR child has exited but is unpolled →
  the closure terminates and joins it, releases the GPU hook exactly once, `_subs_job` is None,
  and `done` can still fire once the restores finish (D26); after a `_start_subs` launch error the
  fake `_probe` for the next restore runs with `_launch_lock` **not** held (RLock probed from a
  second thread with `acquire(timeout=0)`, D27); 1500 translate-only subs-only files with an
  empty `_pending` → `start()` and `check()` do not raise and all 1500 are submitted to the fake
  translator, with the stack depth observed inside the fake `submit` bounded (D29); the same 1500
  files with **no key** → `start()` walks them all, every job `skipped(no_llm_key)`, one alert,
  and `done` is emitted with 1500 skipped (D32); a translate-only file with no key followed by a
  pending restore → exactly one `jasna.exe` launch in the same `check()` (D31); first ASR attempt
  `failed` and the retry's `start_asr` returns `"unstable"` / raises in spawn → job settled
  `skipped(unstable)` / `failed`, exactly one GPU release, `_subs_job` None, next restore launched
  (D33); `_gpu_acquire`
  followed by a Stop that wins the lock before `start_asr` → `_gpu_release` still called once (D30); a zero-work Start (empty folder, all restored, or all `none`)
  emits no event with `av_translate` on and off (D20); `done`
  emitted exactly once, and only when every condition holds — including after the **last**
  translation settles with no further `_advance` (D1); each condition alone blocks it; Stop with a
  live ASR child terminates it and releases the GPU hook; Stop with an exited-0 ASR child publishes
  `.ja.srt` only; Stop cancels the translator first and joins it within the budget; `start()` after
  Stop takes a new generation, cleans up when only the translator was alive, sweeps `*.<gen>.tmp`
  and `.avsubs/tmp`; results from a previous generation are dropped; **real supervisor paths**:
  `Supervisor.register` → `unregister` → `register` (as `admin.set_enabled` does) and
  `Supervisor.reconfigure` with a live fake translator/ASR child → the old instance is stopped
  (threads joined, child terminated), the new instance has a new generation and ignores old
  results; **immediate re-Start does not deadlock** (join with timeout, D11); exe missing → one
  alert, all `skipped(no_exe)`, restores run; `av_translate` off → the existing `test_jasna.py`
  suite passes unchanged; metrics contract (`phase` incl. `translate`, `subs_*`, no `current_file`
  without a live child, D17) and detail strings; GPU hooks called in strict pairs (spy) for
  restore, ASR, retry, unstable, start error, stop, disable.
- `test_status_md.py`: `subs 3/5` part with `subs_total`; lada line byte-identical; failed suffix.
  `test_catalog.py`: the four fields with their defaults.
- UI: `wizard.test.tsx` renders `av_translate` unticked; i18n keys in both languages.

**Regression risk areas**: every existing `test_jasna.py` case, `status_md` lada tests,
`test_version.py`, the #178 worker tests.

**Manual smoke (owner)**: (the ffprobe audio-stream check on a real Jasna output — A3′ — is done:
`aac` stereo on every probed `C:\OUTPUT\*_restored.mp4`); tick「AV 翻译」on a Jasna task with a
short film → after restore the detail shows `subtitling:`; `<name>_restored.ja.srt` then `<name>_restored.srt` appear; `done`
says `Subs: 1/1 done`; Start again → all skipped; delete `.srt` → translate-only; delete both →
subs-only without a Jasna launch; Stop during translation stops within seconds; the packaged app's
first translation exercises `taskpaw-backend llm-worker` (A4).

## Handoff notes

- Pilot split: **A** = `core/generation.py` + the whole `subs` package + its tests, tests-first, no
  Jasna edits; **B** (after A lands) = `jasna.py` integration, `status_md.py`, openclaw guide,
  `test_jasna_subs.py`/`test_status_md.py`/`test_catalog.py`; **C** (parallel with A) =
  `schemaI18n.ts`, `i18n.ts`, `wizard.test.tsx`. Driver: version bump, CHANGELOG, README,
  adversarial sweep, commit/push/PR.
- B codes against the APIs in this doc exactly; if A had to deviate, A's handoff lists it and B
  reads A's code first.
- `plan_queue` is not modified (D19); `plan_subs` scans the input folder itself.
- The Jasna FakePopen (jasna.exe) is unchanged; ASR/worker children are `ChildProcess` fakes
  injected via `JasnaInstance._spawn` and `Translator(spawn=…)`; `jasna.py` binds `Translator` and
  `ChildProcess` as module-level names (`J.Translator`, `J.ChildProcess`) so supervisor-created
  instances can be faked by monkeypatching them (critic round 3); a Jasna test never reaches
  `taskkill` (D10).
- Do not touch `lada.py`.

## Post-debate deferrals carried into the implementation (round 6, design frozen at v6)

Two round-6 findings were closed as `Deferred` at the design stage (design untouched; the shared
review-cycle allowance was held for the PR-gate stage) and are **required** of the implementation
and its tests; the PR-gate reviews verify them against the code:

- **D34 (P2)** — the exe-missing preflight in `_start_managed` must also empty `_subs_only` (route
  it through `_disable_subs("no_exe")` with the `subs-noexe` alert, or clear the list explicitly),
  otherwise `_maybe_done` can never fire for that run. Tests: pending restores plus one subs-only
  film with the exe missing → all `skipped(no_exe)`, one alert, exactly one `done` after the last
  restore with `Subs: 0/T done, 0 failed, T skipped`; the zero-pending variant → `done` on the
  first check.
- **D35 (minor)** — the `_poll_subs` retry's post-spawn `_stopping` re-check does exactly what
  `_start_subs` does: `job.terminate()`, `release_gpu = True`, `_subs_job = None`, no advance
  request; covered by the strict-pairs spy test.

## Implementation-stage record (PR #183)

- **Contract deviation (recorded, harmless):** `test_jasna.py` is not byte-untouched — its
  Jasna-field count assertion moved from 15 to 19 (one line, commented) because the four new
  fields live directly on `JasnaConfig`; every other line and every existing test is unchanged.
- **Review-driven repair batch (the issue's sixth and last review cycle):** S1 (P2 — settle
  translation results before the `_launch_error` early return, plus a `_launch_error` guard in
  `_advance`), S3 (the D6 cancel sentinel is now tested with a fake worker that emits no `Eof`),
  S4 (`needs_llm_key` exported once from `subs/translate.py`), IR-a (`_stop_asr` reads
  `job.child` once), IR-b (post-start `_stopping` re-check cancels/joins a translator started
  during a concurrent Stop), IR-e (the degraded snapshot re-derives `phase`), S2 (openclaw guide:
  `phase` is present for every managed Jasna). Deferred as minor: IR-d (attempt dirs under
  `.avsubs/<sha1>/` are not removed after settle; revisit with #179).
- **Out of scope (pre-existing):** `start()` clears `_stopping` unconditionally, so a Stop that
  completes just before a Start can be undone; predates #177.
- **Seventh cycle (operator-authorized after the gates):** CX2 (`_translator` assigned before the
  post-start Stop re-check), CX3 (a 0-cue `.ja.srt` completes with an empty `.srt` before the
  key check), F1 (blank lines inside an LLM value are dropped; `srt.serialize` never emits an
  empty line inside a cue), F2 (`_maybe_done` cancels and joins the translator after `done`),
  K-m1 (over-long digit fields → `SrtError`), K-m2 (a relative manifest output resolves only
  under the attempt dir), K-m3/K-m5 docstrings, K-m4 field text, K-m8 status test. F3 deferred
  by decision (see non-goals); CX1 deferred (two monitors sharing an output folder is forbidden
  by the field text; revisit with #179's shared staging).
