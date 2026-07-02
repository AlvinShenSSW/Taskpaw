# TaskPaw Bug Audit — 2026-05-06

Consolidated punch list from a full audit of `taskpaw.py`, `taskpaw_hub.py`, and `macsubs.py`, plus targeted investigation of the Lada-queue and "app freezes" reproductions.

Severity: **C** = Critical, **H** = High, **M** = Medium, **L** = Low.

---

## 0. The two issues you reported (root cause)

### A. Lada queue only ever shows the first filename, no fps / no %

Two bugs, both in `taskpaw.py` `LadaWatcher`:

1. **The "real-time progress" feature is not actually implemented.** The docstring at line 431–432 claims managed mode "captures stderr for real-time progress (filename, %, fps, ETA)", but the actual `Popen` call at line 472–473 uses `CREATE_NEW_CONSOLE` and does **not** redirect stdout/stderr. Lada's progress bar is shown in its own console window and TaskPaw never sees it. Severity: **H**.

2. **`_detect_current_file()` (line 576–611) compares input vs output folders by filename *stem*.** It returns the first input file whose stem isn't present in the output folder. If `lada-cli` writes its outputs with a different name (suffix like `_restored`, a different folder layout, or writes a temp file first), then *no* input stem will ever match, so it permanently reports the first file. This is exactly the "20 files in queue, only file #1 is shown" symptom. Severity: **H**.

**Fix path (recommended):**

- Capture stderr properly: drop `CREATE_NEW_CONSOLE`, use `stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1, text=True, encoding='utf-8', errors='replace'`. Run a small reader thread that parses lada-cli's tqdm output (lines like `frame=  123 fps= 24 %=42`) with regex and pushes a structured progress dict (`current_file`, `frame`, `fps`, `percent`, `eta`) to the watcher state.
- Replace `_detect_current_file` with the parsed `current_file` from stderr. Keep folder-snapshot only as a fallback for passive mode.
- For passive mode (`lada_cli_path` empty): we genuinely can't see lada's internals. Show "running" + queue counts, but don't pretend to know the current file. Document the limitation.

### B. App often freezes, can only be force-killed

Multiple contributing causes; the dominant ones:

1. **`_get_cpu_memory()` calls `wmic` twice per poll (line 368–391).** `wmic` is deprecated on Windows 11 and notoriously slow — can take 1–3 s even on a healthy box, sometimes hangs entirely. With a 10 s poll interval and 5 s subprocess timeouts, a single hung `wmic` blocks the watcher thread and stalls all status updates from that monitor. Severity: **H**.
2. **The HTTP API uses single-threaded `HTTPServer.handle_request()` in a poll loop (line 283–291).** When the Hub pings `/status` while the watcher is mid-poll, the handler can serialise behind a lock the watchers don't have, and any slow request blocks all subsequent ones. Severity: **C** (this is a top suspect for the "假死" symptom).
3. **Watcher state dicts are accessed from worker threads with no lock** (`self.watcher_status`, `self.watchers`). On Windows, racy dict mutation has been seen to throw `RuntimeError: dictionary changed size during iteration` from the UI thread, which Tk handles by spinning the message pump — visually a freeze. Severity: **C**.
4. **`_stop_watcher` doesn't `.join()` the thread** (line 1708). Closing/quitting the app while monitors are still polling means subprocesses outlive Tk shutdown; the tray-stop bare-`except` at line 2109 then swallows the deadlock signal and you get a zombie process that needs Task Manager. Severity: **H**.
5. **`tk.Tk.after(0, ...)` is called from worker threads without checking that the root window still exists** (line 1645–1647). This is benign in steady state but causes a hang during shutdown.

**Fix path (recommended, in order):**

1. Replace `wmic` with `psutil` (`psutil.cpu_percent(interval=None)`, `psutil.virtual_memory()`). Sub-millisecond, never hangs. You already have `psutil` as a dependency.
2. Switch `HTTPServer` → `ThreadingHTTPServer`, run it in its own thread with `serve_forever()`, shut down via `server.shutdown()` then `server.server_close()`.
3. Add a single `self._state_lock = threading.RLock()` in `App.__init__` and wrap every read/write of `watcher_status` / `watchers`.
4. In `_stop_watcher`, after `.stop()`, do `watcher.join(timeout=5)` and log if it didn't exit.
5. Wrap every `self.root.after(...)` in `if self.root and self.root.winfo_exists():`.

After 1–4, the freezes should stop. #5 is just hygiene.

---

## 1. taskpaw.py — full bug list

### Critical

- **C1 / line 283–291** — HTTP API uses single-threaded `HTTPServer.handle_request()`; switch to `ThreadingHTTPServer` + `serve_forever()`.
- **C2 / line 148–150** — `save_config()` writes directly over the file. A crash mid-write (or a power loss) corrupts config. Fix: write to `*.tmp` then `os.replace()`.
- **C3 / lines 1645/1660/1701/1709** — Shared dicts `watcher_status` / `watchers` are touched by worker threads and the UI thread with no lock. Add `RLock`.
- **C4 / line 295–300** — `server.shutdown()` called from a different thread than `serve_forever()`; combined with bare `except`, app exit can hang. Fix once `ThreadingHTTPServer` is in.

### High

- **H1 / line 472** — Lada managed mode does not capture lada-cli stdout/stderr (see §0.A.1).
- **H2 / line 576–611** — Lada current-file detection is fragile (see §0.A.2).
- **H3 / line 1157** — `CustomCmdWatcher` runs user-provided commands with `shell=True`. Replace with `shlex.split()` + `shell=False`.
- **H4 / line 926, 936** — ComfyUI `json.loads()` failures swallowed silently. Catch `json.JSONDecodeError` and log the body's first 200 chars.
- **H5 / line 1708** — `_stop_watcher` doesn't `join()` the thread (see §0.B.4).
- **H6 / line 364–399** — `_get_cpu_memory()` uses `wmic` (slow / deprecated). Replace with `psutil` (see §0.B.1).
- **H7 / line 1047–1048** — Folder watcher treats freshly-created 0-byte files as "stable" candidates. Add `if size == 0: continue`.
- **H8 / line 274** — HTTP API binds `0.0.0.0` with no auth. Either bind `127.0.0.1` (and tunnel via Tailscale/ZeroTier) or add a shared-secret header check matching the Hub's token. Already have a token field for OpenClaw — reuse it.

### Medium

- **M1 / line 1132** — Process name match is a substring on `tasklist` output. `python` matches `pythonw.exe` and any path containing the word. Match the whole image-name field instead (`tasklist` columns are fixed-width or use `/FO CSV`).
- **M2 / line 776–792** — ComfyUI idle-confirm state machine can double-fire on the boundary; check `was_processing` before incrementing `idle_count`.
- **M3 / line 1041** — `Path.iterdir()` + `stat()` on a network folder can hang. Wrap in a per-file `try/except OSError` and skip.
- **M4 / line 44–51** — No log rotation. Use `RotatingFileHandler(maxBytes=10_000_000, backupCount=5)`.
- **M5 / line 552–554** — Lada force-kill path doesn't log success. Add a log line so you can tell the difference between graceful and forced exit.

### Low

- **L1** — Bare `except: pass` on icon set (line 1249), tray stop (2109/2116), `root.after` (1645). Catch the specific exception and log at debug level.
- **L2** — Folder-watcher silently drops files that disappear before stable. Add a debug log so this isn't invisible.
- **L3** — `requirements.txt` should pin major versions of `psutil`, `pystray`, `Pillow`.

---

## 2. taskpaw_hub.py — full bug list

### Critical

- **HC1 / line 78–82** — `PRAGMA foreign_keys=ON` never set; FK constraints don't actually fire. Add it right after opening the connection.
- **HC2 / line 576** — `status.md` written directly. OpenClaw can read a half-written file. Write to `status.md.tmp` and `os.replace()`.
- **HC3 / line 305–306** — Pruning compares Python-side `datetime` against the stored string column lexically. **Old logs are never deleted.** Fix: `DELETE FROM status_log WHERE timestamp < datetime('now', '-7 days', 'localtime')`.

### High

- **HH1 / line 346–363** — Polling loop drifts because it counts iterations of a 1 s sleep instead of using wallclock. After a few days, polls happen 60–70 s+ apart. Use `next_due = time.monotonic() + 60` and `time.sleep(max(0, next_due - now))`.
- **HH2 / line 391** — `prune_old_status_logs()` not wrapped; if it raises, the polling thread silently degrades. Add try/except around it.
- **HH3 / lines 169/180/188/200/211** — Insert paths commit on success but have no `rollback()` on exception. Wrap each in `try/except: self._conn.rollback(); raise`.
- **HH4 / line 299–308** — Pruning never `VACUUM`s, so the DB file only grows.
- **HH5 / event dedup at lines 338, 450–457** — `last_event_ids` is in-memory only; on Hub restart, all historical events are re-ingested as duplicates. Persist into the `config` table.

### Medium

- **HM1 / lines 418/422/446/490/534** — Hardcoded 5 s HTTP timeout; no separate connect-vs-read timeout. With urllib, set `socket.setdefaulttimeout(2)` for connect and pass `timeout=5` for read.
- **HM2 / line 1048–1050, 1157–1159** — No IP/port validation on the server-add dialog. Use `ipaddress.ip_address(ip)`.
- **HM3 / lines 197/301/342/426/569** — Mixed naive `datetime.now()` and `datetime('now', 'localtime')` strings; comparisons are sometimes lexical, sometimes object. Standardise on SQLite-side timestamps and parse only when displaying.
- **HM4 / line 50–57** — Hub log has no rotation either.

### Low

- **HL1 / line 276–296** — Silent `except` on JSON parse in `get_recent_status_logs()`. Log it.
- **HL2 / line 1430–1435** — Timestamp parse in events tab silently corrupts on unexpected formats; log at debug.

---

## 3. macsubs.py and integration glue

### macsubs.py

- **MC1 / line 35** — API key is a Chinese-language placeholder string. Will fail on first translation. Add a startup check: refuse to run if `API_KEY` is empty or starts with the placeholder.
- **MH1 / line 180–181, 201–202** — `update_status()` and `get_mac_system_info()` swallow all exceptions silently. Status reported to the Hub will be stale and you won't know why.
- **MH2 / lines 43–54, 74, 189** — Global `_current_status` dict touched by HTTP handler thread and main thread without a lock.
- **MM1 / line 265–284** — Translation retry uses fixed 3 s delay × 3 attempts. Use exponential backoff and log each attempt.
- **MM2 / line 127–128** — `/events` endpoint always returns `{"events": []}`. Implementation never queues events, so subtitle completions can't propagate to OpenClaw via the Hub.

### Integration

- **IC1 — Port mismatch.** `macsubs.py` listens on **5679** (line 40); `taskpaw_hub.py` defaults to polling **5678**. The Hub will report MacSubs as offline forever unless the server is added with port 5679 explicitly. Either change `macsubs.py` to 5678 (it doesn't conflict with anything if MacSubs runs on the Mac Mini and TaskPaw doesn't), or document the per-server port override clearly.
- **IH1 — Event ID contract.** `macsubs.py` would need to emit `id` fields starting from 1 and monotonically increasing, persisted across restarts, otherwise the Hub's dedup at line 450–457 (`e.get("id", -1) > last_id`) drops everything. Today this means: even if `/events` were populated, nothing would flow.
- **IH2 — Monitor `type` field.** `macsubs.py` reports `"type": "macsubs"`, but `OPENCLAW_INTEGRATION.md` only lists `lada / comfyui / folder / process / custom`. Either extend the documented enum or rename to `"custom"`.
- **IM1 — Machine-name vs server-name mismatch.** The string the Hub stores in `servers.name` and the `"machine"` field returned by `/status` are independent and can diverge. Add a sanity check on first poll: if they differ, log a loud warning.
- **IM2 — Timezone contract.** Three machines, three notions of "now". Pick one (Mac Mini local time is fine) and convert on ingest in the Hub.
- **IM3 — Webhook payload schema.** The TaskPaw → OpenClaw POST has no documented schema. Worth pinning down (fields, types, version) so OpenClaw isn't guessing.

---

## 4. Suggested fix order

If we work on one batch at a time:

1. **Stop the freezes**: C1 + C3 + H6 + H5 + C4. About a day of work; will eliminate the force-quit symptom.
2. **Fix the Lada queue display**: H1 + H2. Half a day. Needs a small lada-cli output sample so we can write the regex confidently.
3. **Stabilise the data layer**: HC1, HC2, HC3, HH1. Half a day.
4. **Lock down config & API**: C2, H8. Quick.
5. **Everything else in priority order.**

Step 1 is the highest-leverage thing we can do. Step 2 is what you specifically asked about. After those, the app will feel like a different program even before any UI work.

---

## 5. About "harness concepts"

Short version: an **agent harness** is the wrapper code that turns an LLM into something useful — it manages the conversation loop, the list of tools the model can call, the memory/context window, retries, and termination. Claude Code, Claude Agent SDK, and OpenClaw are all examples.

What it would mean for TaskPaw if you adopted it:

- Today, every monitor type (Lada, ComfyUI, Folder, Process, Custom) is a hard-coded class with bespoke logic. Adding a new one means writing Python, rebuilding the .exe, redeploying.
- A harness-style TaskPaw would expose its monitors as **tools** (small functions with a JSON schema describing inputs/outputs). The "agent" part — a tiny LLM loop, possibly running in OpenClaw — would decide *when* to poll, *what* to compare, and *how* to summarise results, instead of every state machine being baked in.
- Practical consequence: instead of a `ComfyUIWatcher` class with a hand-written idle-detection state machine, you'd have a `comfyui.queue_status()` tool, and OpenClaw would say things like "tell me when SnowLeopard's queue is empty for 3 minutes straight" without you writing code.

It's worth doing, but only **after** the bugs are fixed. Rebuilding on a buggy foundation just gives you a smarter buggy thing. I'd recommend: fix bugs in the current shape → add a clean web UI → then refactor monitors into tools and wire them to OpenClaw.

---

*Generated by Kate, 2026-05-06.*
