# C2 (#377): Windows graceful backend shutdown and fast stale-port reclaim

Date: 2026-10-31 · Issue: #377 (EPIC #375, audit items 3 and 4) · Base: `main` @ 3ddf070 (4.0.2)

Plan only. Nothing here is implemented yet.

Revision 2, after debate round 1. Changes from revision 1:

- **C2-D1.** The blocking `os.read(0)` watcher is replaced by a non-blocking
  `PeekNamedPipe` poll. A pending synchronous read made Python grandchildren hang at
  startup; this was reproduced, and the fix was verified.
- **C2-D2.** AC2 is narrowed to the normal close path. With `KILL_ON_JOB_CLOSE`, a
  shell crash stays a hard kill.
- **C2-D3.** A busy-close smoke step and a risk note are added.
- **AC5 wording.** `net.py` must be behaviour-equivalent on all OSes (macOS, Linux
  and Windows).

Revision 3, after debate round 2:

- **C2-D4.** After resolving the pipe handle, the watcher points the process's
  `STD_INPUT_HANDLE` at a NUL handle, so children inherit NUL instead of the
  lifeline pipe. Revision 4 makes the handle process-lifetime; it now lives in a
  module global (C2-D6). Verified in the frozen + Rust
  setup; the `stdin=DEVNULL` fallback is not needed.
- **C2-D5.** `get_osfhandle` is wrapped in `try`/`except OSError`. The watcher starts
  only when `GetFileType` returns `FILE_TYPE_PIPE`. The poll loop ends once shutdown
  is in progress.

Revision 4, after debate round 3:

- **C2-D6.** The NUL handle is held in a **module-level global**. It is a raw
  `os.open(os.devnull, os.O_RDONLY)` fd, opened once and never closed. It used to
  be an attribute on the shutdown object, and that object can be garbage-collected
  after `run_agent` returns. The in-process Windows tests save and restore the
  worker's `STD_INPUT_HANDLE`.
- **C2-D7.** The "shutdown already in progress" test is rewritten so the watcher
  actually peeks before shutdown.
- **§7.3.** Adds a note on CRT spawns (`os.system`, `os.spawn*`, `os.exec*`).

## 1. Spec review

The issue has two independent parts.

**A. Windows shutdown.** On Windows the Tauri shell never asks the backend to stop.
`terminate_child` (`taskpaw_v3/src-tauri/src/main.rs`, Windows branch) polls
`try_wait` for 5 s and then calls `child.kill()`. The Job object then kills the
tree. The backend listens only for SIGTERM/SIGINT
(`taskpaw_v3/core/lifecycle.py`, `install_signal_handlers`), which the shell cannot
deliver on Windows. As a result:

- every close stalls for 5 s;
- `agent.stopping` is never recorded, so every launch reports
  `previous_exit: unclean` (the operator's task log shows this on every launch since
  2026-10-04, apart from one in-app restart on 2026-10-24);
- `workflow_host.stop`, the supervisor stop and the power-sampler flush never run;
- the killed PyInstaller onefile bootloader leaks its `%TEMP%\_MEI*` folder.

The core need is a stop request the shell can send on Windows during its normal
quit path. A shell *crash* is out of reach: the backend is in the shell's
`KILL_ON_JOB_CLOSE` Job object, so the OS kills the whole tree when the shell's
last job handle closes. That is before any pipe EOF can be acted on (§3, contract
correction).

**B. Startup.** `reclaim_ports_from_stale_instance` (`taskpaw_v3/core/net.py`) calls
`_find_stale_backends` once per port. Each call runs a full `psutil.process_iter()`.
On a normal launch every port is free, so the work is wasted. The core need is to
skip the scan when it cannot find anything, and to scan at most once otherwise.

**Ambiguities, settled from the code:**

- *"macOS and Hub unchanged".* The Mac Hub is connect-only and spawns no backend
  (`hub_connection::connect_only`). Hub releases are macOS-only
  (`.github/workflows/release.yml`, job `hub`). The macOS hub service runs under
  launchd and never sets the new environment variable. "Unchanged" therefore means:
  - no Rust change outside `cfg(windows)`;
  - a backend watcher that is Windows-only and off unless the shell enables it;
  - `net.py` changes that are behaviour-equivalent on every OS (AC5).
- *"filtering by name before reading cmdline/exe"* (the issue's proposal).
  Measurement shows this saves nothing; see §6.5. The plan drops it.

## 2. Acceptance criteria

1. On Windows, closing the app makes the backend run `GracefulShutdown.shutdown()`.
   `agent.stopping` is recorded, and the next launch reports `previous_exit: clean`.
2. On the normal close path, the shell closing the backend's stdin pipe triggers the
   backend's graceful shutdown. The existing kill after the grace period stays as the
   fallback. A shell crash remains a hard kill of the backend tree by the Job object,
   as today (corrected scope, §3).
3. A normal close no longer stalls for 5 s. `terminate_child` returns as soon as the
   backend process exits; it polls every 100 ms, as today.
4. `reclaim_ports_from_stale_instance` returns without calling
   `psutil.process_iter()` when every port passes `port_available()`. When a scan is
   needed, it calls `process_iter()` exactly once for all ports.
5. **macOS is behaviour-equivalent:**
   - no Rust change outside `cfg(windows)`;
   - the stdin watcher is Windows-only and inert unless the shell sets
     `TASKPAW_STDIN_LIFELINE=1`;
   - `net.py` reclaim produces the same outcome for every reachable state on macOS,
     Linux and Windows (same kills, same return value). It only makes fewer or no
     `process_iter()` calls (§5.2).
   - The Hub service paths are unchanged.
6. The stdin watcher must not change what child processes see. A child spawned with
   `stdin=None` inherits NUL, not the lifeline pipe. A child that reads stdin gets an
   immediate EOF, as before this change. A concurrent Python grandchild starts
   normally (C2-D1 and C2-D4 regressions). The watcher never makes a pending read.
   It starts only when stdin is a pipe (`GetFileType == FILE_TYPE_PIPE`). With any
   other stdin, or a bad fd, it logs a warning and does not start (C2-D5).
7. Python tests cover AC1, AC2, AC4, AC5 and AC6. Windows-only Rust tests cover AC2's
   fallback and AC3.

## 3. Frozen issue contract

**Acceptance criteria:** §2 (AC1–AC7).

**Contract correction from repository evidence.** Issue #377 AC2 says the backend
"also stops gracefully when the shell dies and the pipe closes". The repository
contradicts this. The shell assigns the backend to a Job object with
`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` (`main.rs` `jobobj::assign`), so when the shell
dies the OS kills the tree before EOF can be handled. The critic confirmed this by
experiment in debate round 1. The shell-crash clause is therefore dropped: a shell
crash stays a hard kill, exactly as today. Keeping the Job object is an invariant;
it is the no-orphan guarantee.

**Invariants:**

- *No orphan backends.* The Job object (`KILL_ON_JOB_CLOSE`) and the post-grace
  `child.kill()` stay exactly as they are.
- *Constitution "clean shutdown".* Stopping joins threads, terminates managed
  children and releases ports.
- *Only positively identified backends are killed.* Reclaim kills only this app's
  own backend for this role, and never when any port has a foreign holder. All
  existing identification predicates (`_backend_role`, `_addr_conflicts`,
  `_same_bind_target`) stay byte-for-byte.
- *Readiness handshake untouched.* Stdout stays piped and is read by
  `read_readiness`.
- *Graceful-stop semantics untouched.* The `GracefulShutdown.shutdown()` contract
  (idempotent, LIFO, sticky) does not change.
- *Restart exit code untouched.* An in-app restart still exits with code 75 through
  `restart.request`.
- *No pending read on the backend's stdin* (C2-D1). The watcher only peeks.
- *Children never inherit the lifeline* (C2-D4). The process's `STD_INPUT_HANDLE`
  points at NUL once the watcher has resolved the pipe. That NUL is a module-global
  fd that is never closed (C2-D6). The original pipe handle is never duplicated
  over, closed or `dup2`ed.

**Allowed user-visible changes:**

- Closing on Windows becomes faster: about 1–1.5 s when idle instead of 5 s.
- The next launch logs `previous_exit: clean`.
- `_MEI*` folders are no longer leaked on a normal close.
- Launch is about 2.2 s faster when all ports are free.
- None for child processes: with `stdin=None` they get NUL, so a read sees an
  immediate EOF, the same result as today (AC6).

**Non-goals:**

- Graceful stop on a shell crash (Job object, above).
- macOS crash-orphan protection.
- Changing the grace length (§6.3, §11.4).
- Changing the singular `reclaim_port_from_stale_instance`, which only tests call.
- Replacing `psutil.process_iter` with a system-wide socket scan.
- Any version bump or CHANGELOG work beyond the EPIC's release batching.

**Smallest causal boundary:**

- `spawn_backend` and `terminate_child` (Windows branch) in `main.rs`;
- `GracefulShutdown.install_signal_handlers` in `core/lifecycle.py`;
- `reclaim_ports_from_stale_instance` and `_find_stale_backends` in `core/net.py`;
- the tests for these.

No launcher, Hub or monitor code changes.

## 4. Assumptions

"Verified" means a hermetic experiment was run in a temporary directory, then
deleted. The procedures are in Appendix A.

| # | Claim | Status | Risk if wrong |
|---|---|---|---|
| A1 | Rust `Stdio::piped()` stdin, with the production creation flags `CREATE_NO_WINDOW \| CREATE_BREAKAWAY_FROM_JOB`, reaches the **Python child of a PyInstaller 6.21.0 onefile bootloader**. After the shell drops `ChildStdin`, `PeekNamedPipe` on the child's stdin handle fails with `ERROR_BROKEN_PIPE` (109). | **Verified, revision 2**: 3/3 runs with a Rust driver using production flags and a onefile built with the locked PyInstaller 6.21.0. Detection came 0.26 s after the drop (half of one poll interval). Not exercised: Job-object assignment (it does not change handles) and the production `.spec` (same bootloader, `console=True`). | Low. If detection failed, the 5 s kill fallback runs exactly as today. |
| A2 | After the Python child exits normally, the bootloader deletes `_MEI*` and exits with the child's code. The shell's `try_wait` then sees the exit. Killing the bootloader leaks `_MEI*`. | **Verified**: the peek watcher with a 0.5 s simulated stop gave `ExitStatus(0)` 1.10 s after the drop in 3/3 runs, with every `_MEI` removed. Killing the bootloader left `_MEI` behind (revision 1). | Low. |
| A3 | ~~A shell crash leads to a graceful stop.~~ **Refuted.** Under the production `KILL_ON_JOB_CLOSE` Job object, a shell crash kills the tree first (critic experiment, debate round 1). Revision 1's crash experiment ran without a Job object, so it did not represent production. | Refuted; scope corrected (§3). | n/a |
| A4 | An idle agent's real `_stop` finishes well within 5 s on Windows. | **Not measured directly.** Evidence: no streaming or long-poll responses exist, so uvicorn exits within about 0.1 s ticks. Each step is event-driven with bounded joins. The one real in-app restart (2026-10-24) went from `agent.stopping` to the next `agent.started` in 4.9 s, and that includes respawn, onefile extraction and a ~2.2 s port scan. A **busy** stop can take much longer: `workflow_host.stop` has up to about 60 s of joins (`workflow/local_host.py` `stop`). | Medium-low for idle. A busy overrun is R3. The manual smoke (§11.4) measures both. |
| A5 | The watcher's daemon thread (sleeping between peeks) does not affect interpreter exit, including the restart path (exit 75). | By construction: the thread holds no lock and sits in `time.sleep`. A daemon thread blocked in `os.read(0)` was verified harmless at exit (revision 1, 5/5 runs each on Python 3.13 and 3.14), and sleeping is strictly simpler. | Low. |
| A6 | A **pending synchronous read** on the inherited stdin pipe makes Python grandchildren that inherit stdin hang at startup. A **peek poll** does not. | **Verified, revision 2**: with a blocking `os.read(0)` reader, `subprocess.run([python, "-c", "print(1)"], capture_output=True, timeout=8)` timed out. With the peek watcher it returned `rc=0` in 0.04 s, both non-frozen and inside the onefile (3/3). Likely cause: the pending synchronous `ReadFile` holds the file object's lock, and the grandchild's stdio initialization queries the same file object. Not root-caused further. | This is the reason for the revision. |
| A7 | Inherited stdin handles share one FILE_OBJECT. Without hiding the pipe, a child that blocks reading stdin stalls the poller's `PeekNamedPipe` and hangs other Python spawns for as long as it lives (C2-D4). | **Verified, revision 3** (frozen + Rust, control run without hiding): the reader grandchild blocked, a concurrent `print(1)` hung for more than 8 s, three later `python -c pass` spawns each timed out at 8 s, and one peek stalled for **35.07 s**. | This is why A13 is needed. |
| A13 | `SetStdHandle(STD_INPUT_HANDLE, <NUL from a never-closed module-global fd>)`, called after the watcher resolves the pipe handle, makes `subprocess` (which reads `GetStdHandle` when `stdin=None`) give children NUL. The CRT fd 0 and the watcher's handle still refer to the pipe. | **Verified, revision 3** in a frozen PyInstaller 6.21.0 onefile driven by Rust with production flags, 3/3 runs: (a) a stdin-reading grandchild got `''` at once (0.0 s); (b) a concurrent `print(1)` took 0.04 s; (c) the poller still saw error 109 after the drop, with no slow peeks; (d) `_MEI` removed; (e) three later `stdout=PIPE` spawns, no `WinError 6`. | Low. |
| A14 | `GetFileType` returns 3 (`FILE_TYPE_PIPE`) for the shell's pipe, 2 for NUL and 1 for a regular file. `msvcrt.get_osfhandle` on a closed fd raises `OSError(9)`. `PeekNamedPipe` fails with error 1 on non-pipes (C2-D5; critic's finding, not re-run). | **Verified, revision 3** (frozen) for the `GetFileType` values. **Verified** (non-frozen) for `OSError(9, 'Bad file descriptor')`. | Low. |
| A15 | Any child spawned *before* `install_signal_handlers()` (that is, before the hide) inherits the pipe. In `run_agent` and `run_hub`, the supervisor, samplers and workflow host all start after `install_signal_handlers()`. | Read from code. Short-lived startup probes before that point, if any, were not enumerated. | Low. Such a probe would have to block reading stdin to matter. |
| A8 | `os.dup2(nul, 0)` is **not** a safe way to hide the pipe from grandchildren. | **Verified** (revision 1): in a frozen onefile it leaves `STD_INPUT_HANDLE` pointing at the closed handle, so the next `subprocess.run(stdout=PIPE)` raises `OSError [WinError 6]`. | Must not be used. |
| A9 | `psutil.process_iter()` itself is the cost: 0.73–0.84 s per call for 375 processes with psutil 5.9.8 on the operator's machine. Reading `name`/`cmdline`/`exe` for every process costs about 5 ms in total. | **Measured** on the dev machine, not inside the frozen backend. Consistent with the issue's 0.86/0.69/0.69 s. | Low. |
| A10 | On Windows a specific-address bind coexists with another socket's wildcard bind, and `::1` coexists with `127.0.0.1`. Identical `addr:port` is refused (10048). | **Verified** on ephemeral ports. | Feeds R6. |
| A11 | The production frozen backend's Python minor version behaves like 3.13/3.14 for ctypes `PeekNamedPipe` and thread finalization. | Assumed. The release Python version was not checked. | Low. Covered by Windows CI (Python 3.12) and by the manual smoke. |
| A12 | `PeekNamedPipe` also fails with `ERROR_BROKEN_PIPE` on the anonymous pipes from `os.pipe()` and `subprocess.PIPE` (`CreatePipe`) once the writer closes, so tests can use them. | **Verified** for `subprocess.PIPE` (non-frozen run, error 109). `os.pipe()` uses the same `CreatePipe` path; assumed. | Low. A failing test would show it at once. |

## 5. Approach

### 5.1 Shutdown: a stdin lifeline, detected by a non-blocking peek

**Shell (Windows only):**

1. `spawn_backend` adds `command.stdin(Stdio::piped())` and
   `command.env("TASKPAW_STDIN_LIFELINE", "1")` inside the existing `#[cfg(windows)]`
   block. The `ChildStdin` stays in `Child::stdin`, a public field, so no new state
   or struct is needed. Every spawn site goes through `spawn_backend` (setup, the
   recovery respawn, `ShellRespawn::spawn`), so all of them get the lifeline.
2. `terminate_child` (`#[cfg(not(unix))]` branch) first runs
   `drop(child.stdin.take())`, then runs the existing 5 s `try_wait` loop and the
   existing `child.kill()`. All callers share it: `kill_backend` on
   `ExitRequested`, `fatal_startup`, `Backend::drop`, `ShellRespawn::install` while
   exiting, and the setup recovery path.

**Backend (Windows only):** `GracefulShutdown.install_signal_handlers()` gains one
call, `self.watch_parent_stdin()`. Both `run_agent` and `run_hub` already call
`install_signal_handlers()` at the right point: after `_stop` is registered and
before the servers start. The new method:

- pops `TASKPAW_STDIN_LIFELINE` from `os.environ`;
- returns `False` unless the value is `"1"` **and** `sys.platform == "win32"`;
- resolves the stdin `HANDLE` once with `msvcrt.get_osfhandle(fd)`. On `OSError`
  (bad fd) it logs a warning and returns `False` (C2-D5);
- returns `False` with a warning unless `GetFileType(h) == FILE_TYPE_PIPE` (3). A
  NUL or file stdin would otherwise fail the first peek with error 1 and shut the
  backend down at startup (C2-D5);
- **hides the pipe from children (C2-D4, C2-D6).** It opens NUL once as a raw fd in
  a **module-level global**, for example
  `_STDIN_NUL_FD = os.open(os.devnull, os.O_RDONLY)`, created on first use and never
  closed, so it truly lives for the whole process. It then calls
  `SetStdHandle(STD_INPUT_HANDLE, msvcrt.get_osfhandle(_STDIN_NUL_FD))`.

  It must **not** hang off `self`. Nothing keeps the `GracefulShutdown` alive after
  `run_agent` returns: `_main` discards it, and the `StartupShutdown` ↔ callback
  cycle is collectable. The cycle collector would then close NUL while a straggler
  daemon thread is still spawning. The critic showed that deleting the holder plus
  `gc.collect()` makes the next `subprocess.run(stdout=PIPE)` raise
  `OSError(9)` / `WinError 6`, the A8 trap.

  From then on
  `subprocess` with `stdin=None`, which duplicates `GetStdHandle`, gives children
  NUL, exactly like today's immediate EOF. The original pipe handle and CRT fd 0 are
  left alone: never closed, never `dup2`ed (A8);
- starts one **daemon** thread that loops
  `while not self.is_stopping and PeekNamedPipe(h, None, 0, None, byref(avail), None): time.sleep(0.5)`.
  If the loop ends because shutdown is already in progress, the thread just returns.
  It never logs or starts a thread late, for example during interpreter finalization
  after an exit-75 restart (C2-D5). ctypes follows the existing
  `ctypes.WinDLL("kernel32", use_last_error=True)` pattern in `agent/server/detect.py`;
- when `PeekNamedPipe` fails, logs one line and starts a **non-daemon** thread that
  runs `self.shutdown()`. Any failure counts, because every failure means the writer
  is gone or the handle is unusable. The usual one is `ERROR_BROKEN_PIPE` (109),
  recorded in the log line. The non-daemon thread mirrors `core/restart.request`; its
  docstring records that a daemon stopper let the interpreter finalize under the last
  log write (0xC0000409).

**Why a peek poll and not a blocking read (C2-D1).** A thread parked in a
synchronous `ReadFile` on the inherited stdin makes every Python grandchild that
inherits stdin hang at startup (A6). Affected spawn sites include
`monitors/plugins/lada.py` (capture mode: stdout redirected, no stdin, no timeout)
and `monitors/plugins/custom_cmd.py`, plus any site not enumerated. With the peek
there is never a pending read, so there is nothing to block on. The cost is up to
0.5 s of extra latency on close.

**Why the pipe is also hidden from children (C2-D4).** The peek alone is not
enough. Inherited handles share one FILE_OBJECT, so a *child* blocked reading the
inherited stdin stalls the poller and other Python spawns just as the backend's own
read did (A7). An example is a `custom_cmd` operator command with `capture_output`
and no `stdin=`, which got immediate EOF before this change. Pointing
`STD_INPUT_HANDLE` at NUL means no child ever receives the pipe, which removes the
whole class without a per-call-site audit (A13). The fallback, explicit
`stdin=subprocess.DEVNULL` at `custom_cmd.py` and lada's capture mode, was not
needed because every check passed.

**Alternatives considered:**

- *Blocking `os.read(0)` / `sys.stdin.buffer.read()`.* Rejected: C2-D1.
- *Peek without hiding the pipe.* Rejected: C2-D4 (A7).
- *A `stdin=DEVNULL` sweep of spawn sites.* Rejected: it misses sites not
  enumerated, and the single `SetStdHandle` covers them all.
- *`os.dup2(nul, 0)`.* Rejected: it breaks frozen builds (A8).
- *A control-API "shutdown" call from the shell.* It would need the control token
  and an HTTP client in the shell's quit path, plus a new endpoint. The pipe needs no
  credentials and no new surface. A crash is now out of scope for both options, so
  crash coverage no longer decides between them.
- *`GenerateConsoleCtrlEvent`.* The backend runs with `CREATE_NO_WINDOW` and has no
  console, and the event cannot target the Python child behind the bootloader.
- *An ungated watcher.* `python -m taskpaw_v3.agent`, services and tests have no
  shell-owned pipe. The environment variable is the explicit contract between shell
  and backend.

**Thread safety:** `GracefulShutdown.shutdown()` takes an `RLock`, is idempotent,
and is already called off the main thread by `restart.request` and by the uvicorn
`_serve` crash handler. A peek-triggered stop that races a restart request runs
once. If the pipe breaks during startup, `StartupShutdown` already handles it: the
next `checkpoint()` raises and `finish()` cleans up, exactly like a signal during
startup.

### 5.2 Port reclaim: fast path plus a single enumeration

In `reclaim_ports_from_stale_instance`, after the existing pure mutual-bindability
check:

1. Probe every spec once and keep the results:
   `free = {(h, p): port_available(h, p) for h, p, _ in specs}`. If all are `True`,
   return `False` with no `process_iter()` call.
2. Otherwise enumerate once with a new helper, `_our_listeners(role)`. It runs
   `process_iter()` once, keeps the processes `_backend_role` identifies as `role`,
   and returns `[(proc, listen_conns)]`. Phase 1 then filters that list per spec
   with the unchanged predicate (`CONN_LISTEN`, same port, `_addr_conflicts`).
3. Phase 1 uses `free[(h, p)]` instead of probing again. Phase 2 (terminate, then
   wait for each port) is unchanged.

`_find_stale_backends(host, port, role)` keeps its signature as a thin filter over
`_our_listeners(role)`, so the singular `reclaim_port_from_stale_instance` and
`test_find_stale_backends_filters_by_bind_address` are unaffected.

**Why this is behaviour-equivalent (AC5, Q5).** `port_available` is `claim_port`
followed by an immediate close, so it has exactly the startup bind semantics on
every OS:

- Windows: a plain bind and listen, no reuse option.
- Linux: `SO_REUSEADDR` is set, and the kernel still refuses conflicting listeners.
- macOS: an exclusive bind, then a connect probe.

A listening socket that has never accepted a connection does not enter TIME_WAIT
when closed. When every port binds, today's code returns `False` unless a backend of
ours is listening on an address that `_addr_conflicts` calls conflicting but that
the OS lets coexist (A10). That needs one of:

- a wildcard listener, which `guard_bind_exposure` has refused since #114, and which
  `guard_worker_api` refuses for the worker API;
- a `::1` versus `127.0.0.1` split on **both** the network and the control port.

A stale instance of the same install reads the same config, so its numeric-loopback
`control_host` is identical, the control-port bind fails, and the code takes the
full-scan path. On macOS the exclusive bind plus connect probe already reports a
port held by a wildcard listener as unavailable. A stale backend that no longer
listens is not found by today's scan either. When a scan is needed, the single
enumeration feeds the same per-spec predicate in the same order and reuses the same
probe results, so kills and return values are unchanged. Only the number of
`process_iter()` calls drops.

## 6. Answers to the six questions

**6.1 PyInstaller onefile (Q1).**

- Yes, the Python child inherits the stdin pipe.
- When the shell drops the pipe, the child's `PeekNamedPipe` fails with 109 (A1).
- On a graceful exit the bootloader removes `_MEI` and exits with code 0 (A2).

Supporting code evidence: the stdout readiness line already travels from the Python
child through the bootloader on the same handle-inheritance path. A shell crash is
out of scope because of the Job object (A3, §3).

**6.2 Who reads the backend's stdin today (Q2).** Nobody.

- Rust spawns with stdin unset, i.e. `Stdio::inherit`. On Windows release builds the
  windowed shell has no stdin handle; debug builds inherit the console.
- Stdout is piped for `read_readiness`. Stderr goes to the log file in release and
  is inherited in debug.
- No agent or Hub code reads `sys.stdin`. `hub/server/upstream_worker.py` and
  `core/llm_worker.py` read their *own* stdin, which their parent supplies
  explicitly with `stdin=PIPE`.
- After the change the backend still never *reads* stdin; it only peeks. Its
  children get NUL through `STD_INPUT_HANDLE`.
- The dev run (`TASKPAW_BACKEND_CMD=python …`, or a `uv run` wrapper) goes through
  the same `spawn_backend` and gets the same pipe and variable.

**6.3 Entry point and grace (Q3).**

- Call `GracefulShutdown.shutdown()` from a non-daemon thread, as `restart.request`
  does. It is thread-safe (§5.1).
- Cost of `_stop`: it records `agent.stopping` **first**, then:
  - `workflow_host.stop` (up to about 60 s of joins; event-driven when idle);
  - `worker_host.stop`;
  - `supervisor.stop` (bounded at 5 s; monitors record their own
    `task.interrupted`);
  - the samplers (the power flush);
  - the uvicorn exits (about 0.1 s; joins are bounded at 10 s).
- Idle, the graceful stop is expected to be well under 1–2 s (A4). Close time is
  then at most 0.5 s of peek latency, plus the stop, plus about 0.3 s of bootloader
  cleanup.
- **Decision: keep the 5 s grace for now.** Parity with Unix, and the idle case has
  margin. The busy-close smoke (§11.4) records the real stop time. Revisit the grace
  **only if** that measurement exceeds 5 s (C2-D3).

**6.4 macOS and Hub (Q4).** The lifeline is Windows-only on both sides:

- Rust: `stdin(piped)` and the environment variable sit under `#[cfg(windows)]`.
- Python: the platform check comes before any ctypes import.
- macOS keeps SIGTERM-to-group plus its `peek_child` loop byte-for-byte.
- The launchd Hub service, headless `python -m` runs, `bootstrap --run` and tests
  never set the variable.
- A Windows desktop Hub exists only as a dev override (`TASKPAW_UI_ROLE=hub`); it
  would get the same lifeline through `run_hub`, which is harmless and consistent.

**6.5 Fast path (Q5).** This is §5.2.

- Use `port_available()`. It reserves nothing; the later `claim_port` is the real,
  race-free bind.
- Skipping the scan is behaviour-equivalent for every reachable configuration (R6).
- **Do not add the name pre-filter.** The cost is `process_iter()` (A9).

**6.6 Tests (Q6).**

- Rust: `main.rs` already has a `#[cfg(test)] mod tests` that spawns `node` children.
  CI runs `cargo test --locked` on ubuntu-latest and again as "Native Windows Rust
  tests" on windows-latest, with node 22 available. That makes two
  `#[cfg(windows)]` tests practical (§11.2).
- Do not call `spawn_backend` from tests, because `CREATE_BREAKAWAY_FROM_JOB` can
  fail inside a CI runner's job object.
- Python: §11.1. The watcher tests are Windows-only. CI's test matrix includes
  `windows-latest` with Python 3.12.

## 7. Key implementation notes

### 7.1 Rust

- Use the `Child::stdin` field; do not add a new field to `Backend`. Dropping it
  closes the shell's only write end. Rust's parent pipe ends are non-inheritable, so
  no other process holds a write end.
- Put `drop(child.stdin.take())` before the `try_wait` loop. It is harmless on an
  already-exited child.
- `ShellRespawn::release_old` waits on an already-exited old child, and dropping that
  `Child` drops its stdin. No change is needed there.
- Update the `terminate_child` doc comment to describe the Windows mechanism: close
  stdin, wait up to 5 s, then kill.
- Add a `const STDIN_LIFELINE_ENV: &str = "TASKPAW_STDIN_LIFELINE";` under
  `#[cfg(windows)]`, so macOS builds get no unused warning.

### 7.2 Python (`core/lifecycle.py`)

- Add the module constant `STDIN_LIFELINE_ENV = "TASKPAW_STDIN_LIFELINE"` and the
  method `watch_parent_stdin(self, fd: int = 0) -> bool`. Call it at the end of
  `install_signal_handlers()` and update that docstring. The `fd` parameter exists
  for tests.
- Body order:
  1. pop the variable;
  2. if the value is not `"1"`, return `False`;
  3. `if sys.platform != "win32": return False` on its own line, so mypy narrows
     the platform as in `detect.py`;
  4. local imports `ctypes`, `msvcrt`, `ctypes.wintypes`;
  5. a private `ctypes.WinDLL("kernel32", use_last_error=True)` instance, with
     argtypes and restype set for three functions. Use a private instance, never
     the shared `ctypes.windll`, so other users' argtypes are not touched:
     - `PeekNamedPipe`: `[HANDLE, c_void_p, DWORD, c_void_p, POINTER(DWORD), c_void_p]`, returns `BOOL`;
     - `GetFileType`: `[HANDLE]`, returns `DWORD`;
     - `SetStdHandle`: `[DWORD, HANDLE]`, returns `BOOL`;
  6. resolve `msvcrt.get_osfhandle(fd)` inside `try`/`except OSError` (warn, return
     `False`);
  7. if `GetFileType(h) != 3`, warn and return `False`;
  8. open NUL into the module global `_STDIN_NUL_FD`. Open it only if it is still
     `None`; never close it, and never put it on `self` (C2-D6). Then call
     `SetStdHandle(STD_INPUT_HANDLE, msvcrt.get_osfhandle(_STDIN_NUL_FD))`. Pass `STD_INPUT_HANDLE` (-10) as the
     unsigned `DWORD` `0xFFFFFFF6`, or let ctypes convert -10. If `SetStdHandle`
     fails, log a warning and still start the watcher; the watcher is what the
     acceptance criteria need;
  9. start the thread.
- Name the threads `taskpaw-stdin-lifeline` (daemon poller) and `taskpaw-stdin-eof`
  (non-daemon stopper). Use a poll interval of 0.5 s as a module constant.
- Log one `INFO` line, for example "Desktop shell closed stdin (error %d) → graceful
  shutdown", before starting the stopper.
- Never read from the handle. A non-zero `avail` (bytes written) cannot happen
  because the shell never writes; ignore it and keep polling.
- Popping the variable means children spawned by the backend never see it, and a
  second call is a no-op.

### 7.3 Children and the pipe

Python's `subprocess` with `stdin=None` and a redirected stdout/stderr duplicates
`GetStdHandle(STD_INPUT_HANDLE)` as inheritable. Once the watcher has called
`SetStdHandle`, that is the NUL handle, so children read an immediate EOF, exactly as
before this change (A13). EOF detection is unaffected: it depends only on write
ends, and only the shell holds one.

- Do **not** `os.dup2(os.devnull, 0)` or close the original handle (A8).
- Hold NUL as a module-global raw fd that is never closed (C2-D6). A NUL tied to an
  object's lifetime gets closed when the object is collected. `STD_INPUT_HANDLE`
  then points at a closed handle, which is the same `WinError 6` trap as `dup2`
  (critic experiment).
- CRT spawns (`os.system`, `os.spawn*`, `os.exec*`) pass the CRT's fd 0, which is
  still the pipe, not `STD_INPUT_HANDLE`. There are no such call sites in
  `taskpaw_v3` (checked with grep). Keep it that way; use `subprocess`.

### 7.4 `core/net.py`

- Keep the function's docstring contract and update it to mention the fast path and
  the single enumeration.
- `_our_listeners(role)` absorbs today's `_find_stale_backends` body (enumeration,
  `me` skip, `_proc_identity`, `_backend_role`, `_proc_listen_conns`) and keeps the
  same `psutil.Error`/`OSError` tolerance.
- Keep `if psutil is None: return False` first and the mutual-bindability check
  second, both before the fast path.

## 8. Files to change

| Path | Change | Reason |
|---|---|---|
| `taskpaw_v3/src-tauri/src/main.rs` | Modify | `spawn_backend` Windows block: piped stdin and the lifeline variable. `terminate_child` Windows branch: drop stdin first, update the doc comment. Add 2 `#[cfg(windows)]` tests. |
| `taskpaw_v3/core/lifecycle.py` | Modify | `STDIN_LIFELINE_ENV`, a Windows-only `watch_parent_stdin()` (pipe-type gate, `SetStdHandle` to a never-closed module-global NUL fd, ctypes `PeekNamedPipe` poll that ends on `is_stopping`), called from `install_signal_handlers()`. |
| `taskpaw_v3/core/net.py` | Modify | Fast path, `_our_listeners()`, single enumeration, phase 1 reuses the probe results. |
| `taskpaw_v3/tests/test_core.py` (or a new `test_lifecycle_stdin.py`) | Add tests | Stdin lifeline: unit, gate, subprocess end-to-end including the grandchild-startup regression. |
| `taskpaw_v3/tests/test_net_reclaim.py` | Add and adjust tests | Fast path and once-only enumeration. Fixtures where "ours holds the port" must make `port_available` return `False` until the fake `terminate`. |
| `taskpaw_v3/tests/test_net.py` | Adjust 1 test | `test_reclaim_slow_probe_does_not_sleep_past_deadline[multiple=True]` patches `_find_stale_backends`. Point it at `_our_listeners`, and re-state its assertions for the fast-path probes (§11.1). |

No changes to `agent/server/launcher.py`, `hub/server/app.py`, the packaging spec,
`backend_main.py`, monitor plugins or the UI.

## 9. Execution surface

- **Allowed writes:** the six files in §8 only.
- **Read only:**
  - `agent/server/launcher.py`, `hub/server/app.py`, `core/restart.py`,
    `core/tasklog.py`, `core/llm_worker.py`;
  - `agent/server/detect.py` (the ctypes pattern), `workflow/local_host.py`;
  - `packaging/backend_main.py`, `packaging/taskpaw-backend.spec`;
  - `src-tauri/src/restart_supervisor.rs`, `hub_connection.rs`;
  - `.github/workflows/ci.yml`.
- **Executed:**
  - `uv run pytest taskpaw_v3/tests/test_core.py taskpaw_v3/tests/test_net.py taskpaw_v3/tests/test_net_reclaim.py taskpaw_v3/tests/test_launcher.py`
    (targeted; `test_launcher.py` is a regression check);
  - `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`;
  - `cargo test --locked`, from `taskpaw_v3/src-tauri`, on Windows.
- **Generated outputs:** none. No lockfile, bundle or schema changes, and no new
  dependency (`ctypes`, `msvcrt`, `threading`, std Rust only).
- **Forbidden:**
  - starting, stopping or restarting the installed TaskPaw;
  - touching `%APPDATA%\TaskPaw` (reading logs is fine) or the operator's work dirs.
  - Any manual backend run must use a temp `APPDATA`/`LOCALAPPDATA` and a temp
    `FILMPAW_DB`.

## 10. Risk assessment

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | The broken pipe is not detected in the real bundle. | Low (A1 verified with the same bootloader and flags). | Behaviour stays as today: 5 s, then kill. | Manual smoke §11.4 checks `previous_exit: clean`. |
| R2 | A watcher starts where it should not (headless or service run), or the variable is set but stdin is NUL, a file or a bad fd. | Very low. | Without the gate, the first peek would fail and the backend would shut down at startup. | Variable gate (popped), the Windows-only check, `get_osfhandle` `OSError` handling, and the `FILE_TYPE_PIPE` check (C2-D5). Tests assert each gate. |
| R3 | A busy close (workflow stage or GPU monitor running) overruns 5 s and is killed mid-`_stop`. **Because `agent.stopping` is written first, the next launch reads `clean` and skips `task.interrupted` reconstruction.** Interruption rows that monitors had not yet logged are lost, and that one `_MEI` folder leaks. Before this change every close was `unclean`, so reconstruction always ran. | Low (closing during work is rare). `workflow_host.stop` can join for up to about 60 s. | Activity-log gaps and an occasional `_MEI`. Workflow state recovers through its own store. | Measure in the busy-close smoke (§11.4). If it exceeds 5 s, revisit the grace (operator, §12). Same property as the existing macOS SIGTERM and restart paths. |
| R4 | A child inherits the lifeline pipe and blocks reading it, which stalls the poller and other Python spawns (C2-D4). | Very low after the fix: children get NUL through `SetStdHandle` (A13). The remaining window is a child spawned before `install_signal_handlers()` that blocks on stdin (A15). | A stalled poller delays detection; the 5 s kill fallback still applies. | `SetStdHandle` to a never-closed module-global NUL fd (C2-D6), with an AC6 regression test. Never `dup2`. Fallback, if ever needed: `stdin=DEVNULL` at the call site. |
| R5 | The non-daemon stopper keeps the process alive if `shutdown()` hangs. | Low (every step is bounded). | The shell's 5 s kill fires anyway. | The same trade-off `restart.request` already made. |
| R6 | The fast path skips killing a stale backend whose listeners coexist with ours. | Very low (§5.2). | Two backends run. | A test pins "every port binds → no enumeration". |
| R7 | Existing reclaim tests break because their fakes report ports free while "ours" holds them. | Certain. | Red tests. | Planned fixture adjustment (§11.1). |
| R8 | Up to 0.5 s of extra close latency from the poll interval, plus one idle peek every 0.5 s while running. | Certain. | Negligible: a single non-blocking syscall per poll. | Accepted, in exchange for AC6. |

## 11. Test plan

### 11.1 Python

**Stdin lifeline** (`core/lifecycle.py`):

- **In-process isolation (C2-D6).** Every in-process Windows test that can reach
  `SetStdHandle` uses a fixture. The fixture saves the pytest worker's
  `STD_INPUT_HANDLE` with `GetStdHandle(-10)` before the test and restores it with
  `SetStdHandle(-10, saved)` after. A monkeypatched no-op `SetStdHandle` is an
  acceptable alternative. The module global `_STDIN_NUL_FD` may stay open; it is
  one NUL fd. The real hide is verified only by the subprocess end-to-end test
  below.
- **Windows-only** (`skipif(sys.platform != "win32")`): `watch_parent_stdin` with
  the variable set and `r, w = os.pipe()`. Close `w`, then assert `gs.stopped.wait(5)`
  and that the registered callback ran. Also assert the variable was popped.
- Gate, all OSes:
  - variable unset → returns `False` and starts no `taskpaw-stdin-lifeline` thread;
  - variable set to anything other than `"1"` → the same;
  - non-Windows with the variable set to `"1"` → returns `False` and pops the
    variable.
- **Gate, Windows (C2-D5):** run a subprocess with `stdin=subprocess.DEVNULL`
  (fd 0 is NUL) and the variable set. It calls `install_signal_handlers()`, waits
  about 1.5 s (three poll intervals), and prints whether `is_stopping` is set and
  whether a `taskpaw-stdin-lifeline` thread exists. Assert `is_stopping` is `False`,
  no watcher thread exists, and the warning is logged.
- **Unit, Windows:** `watch_parent_stdin` with a bad fd (an fd already closed)
  returns `False` without raising.
- **End-to-end subprocess test, Windows-only.** Run `sys.executable -c` with
  `stdin=PIPE` and the variable set, with an explicit timeout. The script:
  1. builds `GracefulShutdown` and registers a callback that prints a marker;
  2. calls `install_signal_handlers()` (the real wiring);
  3. starts `subprocess.Popen([sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"], stdout=PIPE)`.
     This is a stdin reader with `stdin=None`; it must print `''` at once (C2-D4);
  4. while that runs, runs
     `subprocess.run([sys.executable, "-c", "print(1)"], capture_output=True, timeout=8)`
     (C2-D1). It must print `1`;
  5. prints both results, then waits on `stopped`.

  The parent reads the result lines, then closes stdin. Assert:
  - the reader printed `''` within its timeout;
  - the grandchild printed `1`;
  - exit code 0 within 10 s;
  - the marker is in stdout.

  This also proves the non-daemon stopper and a clean interpreter exit.
- **Unit, Windows (C2-D5 loop end, rewritten for C2-D7):**
  1. start the watcher on `r` from `os.pipe()`;
  2. wait at least one poll interval (about 0.6 s), so it has really peeked at
     least once;
  3. call `gs.shutdown()`;
  4. close `w`;
  5. wait about two poll intervals.

  Assert that no "closed stdin" log line appears (`caplog`), that no
  `taskpaw-stdin-eof` thread exists, and that the `taskpaw-stdin-lifeline` thread
  has exited.
- Regression: the `test_launcher.py` tests that patch `install_signal_handlers` stay
  green.

**Port reclaim** (`core/net.py`):

- New: every port bindable, and a fake `psutil` whose `process_iter` raises
  `AssertionError`. Expect a `False` return with no enumeration.
- New: three specs, all held by one fake backend of ours. `port_available` returns
  `False` until the fake logs `terminate`. Expect `process_iter` called exactly once,
  the backend terminated once, and `True` returned.
- New: one port foreign, the others free. Expect one enumeration, nothing killed,
  `False`.
- Adjust these to the "held until terminate" fixture:
  - `test_multiport_reclaims_when_both_ours`;
  - `test_multiport_distinct_loopbacks_are_bindable`;
  - any other plural-path test that uses `_fake_ports_free()` while a backend of
    ours holds the ports.
- Adjust `test_net.py::test_reclaim_slow_probe_does_not_sleep_past_deadline`,
  `multiple=True` case:
  - monkeypatch `_our_listeners`, returning one fake backend whose conns cover ports
    1234 and 1235, instead of `_find_stale_backends`;
  - count only the probes made after `_terminate_backend`: at most one slow probe
    within `wait`, and no sleeps.
  - The singular case is unchanged.
- The fake-psutil tests run on all three CI OSes and pin AC5's equivalence.

### 11.2 Rust (`main.rs` tests, `#[cfg(windows)]`)

- `terminate_returns_when_the_backend_exits_on_stdin_eof`:
  - spawn `node -e "process.stdin.resume();process.stdin.on('end',()=>setTimeout(()=>process.exit(0),200))"`
    with piped stdin;
  - call `super::terminate_child(&mut child)`;
  - assert elapsed is under 4 s (it is 5 s or more on today's code);
  - assert `child.try_wait()` reports `success()` (a kill exits with code 1).
- `terminate_still_kills_a_backend_that_ignores_eof`:
  - spawn `node -e "process.stdin.on('data',()=>{});process.stdin.on('end',()=>{});setInterval(()=>{},1000)"`;
  - assert `terminate_child` takes at least 4.5 s and the status is not success
    (fallback). This test costs about 5 s.
- Both run in CI's "Native Windows Rust tests" job, which already has node 22.

### 11.3 Regression areas

- Restart flow (exit 75 → respawn): `restart.request` is untouched, and the new
  child gets a fresh pipe.
- Startup-failure recovery path: `kill_backend` now drops stdin first, which is
  harmless.
- Spawned children (lada-cli, ffprobe, custom commands, llm-worker) start normally,
  and children with `stdin=None` get NUL (AC6).
- macOS: Rust diff is `cfg(windows)`-only. The Python watcher is Windows-only.
  `net.py` is behaviour-equivalent.

### 11.4 Manual smoke (Windows, packaged or dev shell)

Operator-run, or run with fully isolated temp `APPDATA`/`LOCALAPPDATA` and a temp
`FILMPAW_DB`. Never against the installed app.

1. Launch idle, then close via the confirm dialog. The window process exits in about
   1–2 s, not 5 s.
2. The backend log contains "Desktop shell closed stdin … → graceful shutdown" and
   "Graceful shutdown complete".
3. Relaunch. The `agent.started` row has `previous_exit: "clean"`.
4. No new `%TEMP%\_MEI*` folder remains after the close.
5. **Busy close (C2-D3).** Close while a workflow stage, or a GPU monitor task, is
   running. Record the time from close until the backend exits, and whether
   "Graceful shutdown complete" was logged before the kill. Revisit the 5 s grace
   only if this exceeds 5 s.
6. Backend log timing: the agent reaches readiness about 2 s sooner. The reclaim
   step no longer dominates.

## 12. Operator decisions

- **None required now.**
- **Conditional:** if the busy-close smoke (§11.4 step 5) exceeds 5 s, decide whether
  to raise the Windows grace (a one-constant change). That would avoid R3's
  occasional `_MEI` leak and lost interruption rows, at the cost of a longer stall on
  a busy close.

## 13. Out of scope

- Graceful stop on a shell crash (the Job object kills the tree; §3).
- macOS crash-orphan protection.
- Explicit `stdin=DEVNULL` sweeps across monitor spawn sites (unnecessary:
  `SetStdHandle` gives every `stdin=None` child NUL).
- The singular `reclaim_port_from_stale_instance`.
- A faster scan path for the rare "port held" case.
- Version bump and CHANGELOG, which follow the EPIC #375 release batching.

## 14. Handoff notes

- Write the TDD RED tests first:
  - the Rust "returns before grace" test takes 5 s or more on today's code;
  - the Python lifeline tests fail on the missing method;
  - the end-to-end grandchild test must exist before the watcher;
  - the net "no enumeration when free" test fails on today's code.
- **Never read the backend's stdin.** Peek only (C2-D1). Never `dup2` or close the
  original handle (A8). Hide the pipe with `SetStdHandle` to a module-global NUL fd
  that is opened once and never closed, never stored on `self` (C2-D4, C2-D6).
  In-process tests restore `STD_INPUT_HANDLE`. Start the watcher only for a real pipe (C2-D5).
- The variable name `TASKPAW_STDIN_LIFELINE` appears once in Rust and once in
  Python. Keep the literals identical.
- Do not add the name pre-filter in `net.py` (§6.5).
- Run only the targeted test files listed in §9, plus ruff, mypy and the Windows
  `cargo test`. CI runs once at the end of the EPIC, per the operator's rule.

## Appendix A — Experiments (hermetic, under `%TEMP%`, deleted afterwards)

**Revision 1**

1. **Onefile build:** a tiny script, built with the locked PyInstaller 6.21.0
   (`--onefile --console`). It logs its pid, ppid and `sys._MEIPASS`, prints a
   `ready` line, and has a daemon stdin watcher. On EOF it starts a non-daemon thread
   that sleeps 0.5 s (a stand-in for `_stop`), then ends the main thread.
2. **Python driver** (`Popen(stdin=PIPE, stdout=PIPE, creationflags=CREATE_NO_WINDOW)`):
   - *close*: exit `rc=0` 0.82 s after `stdin.close()`; `_MEI` removed.
   - *kill*: after `kill()` of the bootloader, `_MEI` remained. This is the leak.
   - *crash*: the intermediate parent was killed, and the child saw EOF. **No Job
     object** was used, so this is not representative of production; A3 is refuted.
3. **Rust driver** (`rustc` 1.96, production creation flags): with a blocking-read
   watcher, `ExitStatus(0)` 0.902 s after dropping `child.stdin`, `_MEI` removed.
4. **Grandchild inheritance:** a child with stdin as a pipe spawned
   `python -c "sys.stdin.read()"` with `stdout=PIPE`. The grandchild blocked (3 s
   timeout). After `os.dup2(nul, 0)`: non-frozen, it read `''`; frozen, the next
   `subprocess.run` raised `OSError [WinError 6]`.
5. **Interpreter exit with a blocked reader:** 5/5 clean `rc=0` exits each on CPython
   3.13.14 and 3.14.2.
6. **Scan cost** (psutil 5.9.8, 375 processes):
   - `psutil.pids()` takes about 0 s;
   - each `process_iter()` takes 0.73–0.84 s, and the cost repeats on every call;
   - reading `name`/`cmdline`/`exe` for all processes takes about 5 ms.
7. **Windows bind coexistence:**
   - `0.0.0.0` held, then `127.0.0.1` binds: OK;
   - `0.0.0.0` held, then a LAN IP binds: OK;
   - `::1` held, then `127.0.0.1` binds: OK;
   - identical `127.0.0.1:P`: refused (10048).

**Revision 2 (C2-D1)**

8. **Blocking read vs peek, non-frozen** (Python driver, `stdin=PIPE`,
   `CREATE_NO_WINDOW`). The child starts its watcher, waits 0.2 s, then runs
   `subprocess.run([python, "-I", "-c", "print(1)"], capture_output=True, timeout=8)`.
   - **Blocking `os.read(0)` watcher:** the Python grandchild **hung** (timed out
     after 8 s). After stdin was closed, the child still saw EOF and exited 0.
   - **ctypes `PeekNamedPipe` poll (0.5 s):** the grandchild returned `rc=0`,
     `b'1'`, in **0.04 s**. After `stdin.close()`, `PeekNamedPipe` failed with
     **error 109** (`ERROR_BROKEN_PIPE`), the graceful stand-in ran, and the child
     exited `rc=0` 0.77 s after the close.
9. **Peek watcher in a PyInstaller 6.21.0 onefile, Rust driver** (production
   creation flags `0x08000000 | 0x01000000`, `TASKPAW_STDIN_LIFELINE=1` set), 3/3
   runs:
   - the frozen child's Python grandchild ran in 0.04 s (`rc=0`);
   - after `drop(child.stdin)`, `PeekNamedPipe` reported **error 109** about 0.26 s
     later;
   - `try_wait` returned `ExitStatus(0)` at **1.10 s** (0.5 s of that is the
     simulated stop);
   - all three `_MEI*` folders were removed, and no `_MEI*` remained in `%TEMP%`
     afterwards.

**Revision 3 (C2-D4, C2-D5)**

10. **Hidden pipe, frozen PyInstaller 6.21.0 onefile, Rust driver** (production
    creation flags, `TASKPAW_STDIN_LIFELINE=1`). The child:
    - resolves the stdin handle (`GetFileType` = 3);
    - in `hide` mode, opens NUL and calls `SetStdHandle(STD_INPUT_HANDLE, NUL)`
      (returned ok);
    - starts the peek poller, which records any peek slower than 50 ms;
    - spawns a stdin-reading grandchild
      (`Popen([python, "-c", "sys.stdin.read()…"], stdout=PIPE)`, no `stdin=`);
    - while that runs, runs `print(1)` with `capture_output`;
    - then makes three later `stdout=PIPE` spawns.

    **`hide`, 3/3 runs:**
    - (a) the reader got `''` at once (0.0 s);
    - (b) `print(1)` returned `rc=0` in **0.04 s**;
    - (c) the poller saw **error 109** after `drop(child.stdin)`, with no slow peeks;
    - (d) `ExitStatus(0)` at 1.20 s, and `_MEI` was removed;
    - (e) the later spawns raised no errors (no `WinError 6`).

    **`nohide` control:**
    - the reader blocked;
    - `print(1)` hung for more than 8 s;
    - all three later spawns timed out at 8 s;
    - one peek stalled for **35.07 s**, returning only after the drop;
    - `_MEI` was still removed, and no processes were left behind.
11. **Gate checks (frozen child via a Python driver):**
    - stdin = NUL: `GetFileType` = 2, watcher not started, normal exit;
    - stdin = a regular file: `GetFileType` = 1, watcher not started, normal exit;
    - non-frozen: `msvcrt.get_osfhandle(0)` after `os.close(0)` raised
      `OSError(9, 'Bad file descriptor')`.
