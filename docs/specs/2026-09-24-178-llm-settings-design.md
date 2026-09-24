# #178 — Global LLM API setting (agent-level) + terminable `llm-worker` sidecar; version 3.3.0

Date: 2026-09-24 (design v4 after adversarial debate rounds 1–3 — D1–D17 folded in)
Issue: #178 (dependency order #178 → #177 → #179)
Driver: `/afk` (Claude Fable 5.1 leads; Opus subagents implement → internal review → Codex 外门
`gpt-6-astra` effort high → Kimi 终审).
Merge policy: leave-open (owner merges).
Upstream spec: `docs/specs/2026-09-24-jasna-av-translate-spec-review.md` §4.2 / §5.A / §11
(four Codex Astra design-review rounds already folded into the issue body).

## Spec review

The owner wants one place to configure an LLM API for the whole agent — base URL, model,
API key — so that the Jasna "AV 翻译" tick box (#177) and the standalone `avsubs` task
(#179) never carry their own credentials. Default provider is Grok through OpenRouter
(`x-ai/grok-4.1-fast`), the combination the old `macsubs.py` proved acceptable for adult
ja→zh subtitles. The key must follow the constitution: environment variable first, gitignored
`agent.yaml` second, masked in every UI/API surface, never in argv or logs.

Two things make this more than three config fields:

1. **Live-apply.** Monitors read the setting at translation time, so a key added or changed in
   Settings must reach them without a restart. Today `MonitorAdmin` treats every editable field
   except `api_token` as restart-required and live-applies only `api_token`; the process-wide
   holder must be initialised before the supervisor starts (today `run_agent()` starts the
   supervisor before `MonitorAdmin` exists).
2. **A terminable request.** #177's translator thread must exit inside the supervisor's shared
   5 s stop budget (constitution §4). In-thread HTTP cannot be cancelled during DNS/TCP
   connect and a slow-drip body defeats a between-operations deadline (Codex round 3–4). So the
   HTTP call runs in a child process. #178 ships that worker (`taskpaw_v3/core/llm_worker.py`
   + a `llm-worker` role in the bundled backend); #177 spawns and drives it. **The cancel
   contract is "close stdin, then kill the tree"** (see Approach): the worker's own stdin
   watcher exits the real Python process on EOF, which is what covers the PyInstaller onefile
   case where `kill()` on the spawned handle reaches only the bootloader (D6).

Ambiguities settled here (see Assumptions for the unverifiable ones): a missing
`finish_reason` is treated as `stop` (some OpenAI-compatible servers omit it); the "test
connection" button tests the **current form values** without persisting and accepts a
truncated reply as proof of connectivity (D4); the env key is never written back to YAML; a
stored key can be **cleared** explicitly (PATCH `llm_api_key: null`), while blank/`***`
keeps it (D12); the Job Object is a best-effort extra layer, never the guarantee (D6).

## Acceptance criteria

- [ ] AC1 `AgentConfig` has `llm_api_base` (default `https://openrouter.ai/api/v1`), `llm_model`
  (default `x-ai/grok-4.1-fast`), `llm_api_key` (default `""`); base is stripped, trailing `/`
  removed, must start with `http://` or `https://` when non-empty; model **and key** are
  stripped (a key with CR/LF or surrounding whitespace is normalised, D1).
  `agent.example.yaml` documents the three keys.
- [ ] AC2 `taskpaw_v3/core/llm.py`: `LLMSettings`, `resolve_llm_settings()` (env
  `TASKPAW_LLM_API_KEY` wins when non-blank after strip), process-wide `set_/get_llm_settings()`,
  `ChatResult`, synchronous `chat()` with full envelope validation, a no-redirect opener, and
  `LLMError(kind ∈ auth|rate_limit|refusal|network|bad_response)` whose messages are fixed
  strings; **no exception other than `LLMError` escapes `chat()`** (D1, D2); logs carry
  kind/status/latency only.
- [ ] AC3 `taskpaw_v3/core/llm_worker.py` + `backend_main` role `llm-worker`: stdin/stdout
  JSON-lines protocol, settings from the child's environment, `worker_argv()`/`worker_env()`,
  a stdin watcher thread that calls `os._exit(0)` **unconditionally** on EOF (D5), a
  catch-all in request handling (D2), and a parent-side Windows kill-on-close Job Object
  helper documented as best-effort (D6).
- [ ] AC4 Control API: the three fields are editable and **live-safe** (no
  `restart_required`); `update_config` live-applies them and refreshes the holder only after a
  successful save; PATCH with blank/`***` keeps the stored key and `null` clears it (D12); GET
  `/control/config` masks `llm_api_key` to `***` and adds `llm_api_key_source: env|config|none`
  computed by the same resolver (D1); new `POST /control/llm-test` (and `llm_test` command)
  tests candidate values without persisting, calls `chat()` outside the admin lock (D11), and
  never returns any exception text (D1).
- [ ] AC5 Launcher initialises the holder before the stale-port reclaim, any socket claim, and
  the supervisor.
- [ ] AC6 Settings page gains an "LLM API" card (agent role): base URL, model, key (password,
  placeholder `***`, disabled with a hint when the source is `env`), "Save LLM settings",
  "Clear key", and "Test connection" against the current form values; zh + en strings.
- [ ] AC7 Version 3.2.1 → 3.3.0 in all six files (`test_version.py` guards); CHANGELOG section;
  README settings line.
- [ ] AC8 Tests per the test plan (hermetic: no default ports, no network, holder + env reset
  fixture, D7/D8); `uv run pytest`, `ruff check`, `ruff format --check`, `mypy`, `npm run
  lint`, `vitest` green.
- [ ] AC9 Secrets: the key never appears in argv, logs, events, HTTP error strings, exception
  text, redirect targets (D10), or the YAML when it came from the environment; tests assert
  this.

## Frozen issue contract

**In scope:** AC1–AC9. Allowed user-visible changes: three new keys in `agent.yaml`; a new
Settings card; two new control routes (`GET /control/config` gains two fields, `POST
/control/llm-test`), PATCH `llm_api_key: null` semantics; a new bundled-backend role
`llm-worker`; version label 3.3.0.

**Invariants:** constitution §2 (secrets from env or gitignored config only; never in
argv/logs/git; no `shell=True`), §4 (no silent except; every thread the worker starts is a
daemon whose only side effect is exit; the worker leaves no orphan), §5 (every behavioural
change has a test), existing `api_token` semantics untouched (masking, keep-on-blank,
live-apply, exposure guard), the agent→hub wire shape unchanged, `config_view()` still
unmasked (the route masks), `restart_required` semantics for the other editable fields
unchanged, tests never touch the default ports 5680/5681 or the network.

**Frozen-contract corrections from repository evidence:**

| # | Issue text | Evidence | Correction |
|---|-----------|----------|------------|
| C1 | "GET 打码在路由层… `admin.config_view()` 保持不打码" | `app.py` `get_config` masks `api_token`; `admin.config_view()` merges desired scalars unmasked | Confirmed; `llm_api_key_source` is computed in the route with `resolve_llm_settings` (needs `os.environ`). |
| C2 | "holder 在 `supervisor.start()` 之前" | `launcher.run_agent()`: guard → reclaim → claim ports → queue → supervisor | Holder init goes right after `guard_bind_exposure`, before `reclaim_ports_from_stale_instance`; the hermetic test stops at the reclaim call (D7). |
| C3 | "`llm_test` … 20 s" | `chat()` has one socket-level `timeout`; no wall-clock deadline exists | `llm_test` uses `timeout=20`; a drip response can hold the request thread longer; `chat()` runs outside the admin lock so nothing else blocks (D11); operator-clicked. |
| C4 | "`chat()` 的 `timeout` 是 socket 级超时" | `urllib.request.urlopen(timeout=)` sets the socket timeout for connect and each read | Confirmed. Total wall time is bounded by the worker being terminable, not by `chat()`. |
| C5 | Issue §3: "stdin EOF 即退出 0" and the packaged smoke "喂一行请求得到一行响应、关闭 stdin 后退出" | With unconditional exit on EOF (D5) a one-shot `echo req | worker` loses its reply | Smoke is interactive: write the line, read the reply, then close stdin → exit 0. |
| C6 | Issue §3: "父进程侧再加两层兜底：`terminate_tree()`…Job Object" | PyInstaller onefile = bootloader parent + Python child; `kill()` on the handle reaches the bootloader only; a Job assigned after `Popen` misses an already-spawned grandchild | The guarantee is the worker's stdin watcher (`os._exit` in the real process) followed by `taskkill /T` on Windows; the Job Object stays as best-effort (D6). |

**Non-goals:** per-monitor overrides; multiple named providers; Hub-side LLM settings; any
translation logic, retry policy, spawn/respawn policy (those are #177); streaming; SDKs;
worker concurrency; Ollama-specific handling beyond "empty key → no Authorization header";
following HTTP redirects.

**Causal boundary:** `taskpaw_v3/core/config.py`, `taskpaw_v3/core/llm.py` (new),
`taskpaw_v3/core/llm_worker.py` (new), `taskpaw_v3/agent/server/admin.py`,
`taskpaw_v3/agent/server/app.py`, `taskpaw_v3/agent/server/launcher.py`,
`taskpaw_v3/packaging/backend_main.py`, `taskpaw_v3/examples/agent.example.yaml`, UI
(`views/Settings.tsx`, `api.ts`, `i18n.ts`), six version files, tests (+ `tests/conftest.py`
fixture), CHANGELOG, README, this doc.

## Assumptions (unverified claims are listed as such)

| # | Claim | Basis / verification | Risk if wrong |
|---|-------|----------------------|---------------|
| A1 | OpenRouter and xAI accept OpenAI-style `POST /chat/completions` with `Authorization: Bearer` and return `choices[0].message.content` + `finish_reason`. | Partly verified: `macsubs.py:38,360-378` used exactly this endpoint/shape against OpenRouter (worked); `finish_reason` handling unverified. | Owner smoke ("Test connection") catches it; `bad_response` surfaces the shape. |
| A2 | `response_format={"type":"json_object"}` is passed through by OpenRouter for Grok. | **Unverified** (needs network). | Only #177 depends on it; its JSON validation catches a non-JSON reply. |
| A3 | Some OpenAI-compatible servers omit `finish_reason`. | **Unverified**. | Missing → `stop` could accept a truncated reply from such a server; #177's id-set check rejects a truncated batch. |
| A4 | `os._exit(0)` from a daemon thread terminates the whole process immediately even while the main thread is blocked in a socket read. | **Verified** 2026-09-24 (driver: Python 3.12.9, Windows 11 26200, exit in 0.63 s; critic E4: 0.007 s with `urlopen`). | — |
| A5 | Windows Job Object `KILL_ON_JOB_CLOSE` kills members when the last handle closes; assignment works inside a nested job. | **Verified** (driver: child exited 0.1 s after `CloseHandle`; critic exp3: nested jobs on Windows 11 26200). **Caveat (D6):** a child assigned after `Popen` does not bring along a grandchild it already spawned; the PyInstaller onefile bootloader spawns its Python child within ~50 ms. | Best-effort layer only; the stdin watcher + `taskkill /T` are the guarantee. |
| A6 | `sys.frozen` is set inside the PyInstaller bundle and `sys.executable` is the `taskpaw-backend` sidecar path; onefile runs as bootloader → Python child. | PyInstaller documented behaviour; critic grep of the 6.21 bootloader (`CreateProcessW`/`TerminateProcess`, no Job API). Build not run. | `worker_argv()` pointing at the wrong binary fails visibly in #177's spawn; the two-process tree is why the cancel contract is close-stdin-then-tree-kill. |
| A7 | `collect_submodules("taskpaw_v3")` bundles `taskpaw_v3.core.llm_worker`. | **Verified from spec source** (`taskpaw-backend.spec`); build not run. | Packaged `llm-worker` role import failure is caught by the owner's packaged smoke. |
| A8 | The Tauri-spawned sidecar inherits the user's environment. | Partly verified: `src-tauri/src/main.rs:362` builds the command without `env_clear`/`.env`; whether a `setx` value is visible depends on how the shell was launched. | Stored `llm_api_key` in `agent.yaml` is the fallback. |
| A9 | CPython `http.client.putheader` raises `ValueError` whose message includes the header value for CR/LF; `IncompleteRead`/`BadStatusLine` are `HTTPException`, not `OSError`; `urllib` follows 3xx by resending `Authorization` and turning POST into GET. | **Verified** by critic E1/E2/E3 (CPython 3.13.14). | Handled by design (D1, D2, D10). |

## Approach

**Why env-first + stored key, not a secrets store.** Constitution §2 names exactly these two
paths; `agent.yaml` is already gitignored and already holds `api_token` with a masking and
keep-on-blank contract in `admin.py`/`app.py`. Reusing that contract keeps one behaviour for
all secrets, plus one addition: an explicit clear (`null`) so a stored key is never sent to a
LAN host after the operator switches the base URL (D12).

**Why a process-wide holder instead of passing settings into monitors.** Monitor instances are
created by `plugin.create(instance_id, config)` with only their own config; the supervisor has
no settings-injection hook. A small holder in `core/llm.py` (`set_llm_settings` at boot and
after each successful save; `get_llm_settings` at call time) gives live-apply with zero
changes to the plugin protocol. It is a module global behind a lock returning an immutable
snapshot — no async, no listeners. Tests reset it through an autouse fixture (D8).

**Why a synchronous `urllib` `chat()` plus a worker process, not a cancellable in-thread
client.** Codex rounds 3–4 showed that socket-level cancellation cannot cover DNS/connect and
a slow-drip body, and that an `http.client` design still left an un-joinable thread. A child
process is the only mechanism whose cancellation covers every phase, and it matches how this
codebase already manages `jasna.exe`/`lada-cli`. `chat()` therefore stays simple and is called
from exactly two places: `llm_test` and the worker. Rejected alternative: a per-batch throwaway
process (1–2 s PyInstaller onefile start-up per batch); the worker is long-lived per run.

**The cancel contract (D5, D6).** The worker owns its own exit: a daemon thread reads stdin;
EOF → `os._exit(0)` immediately, whatever the main thread is doing. The parent cancels by
**closing the worker's stdin** and waiting ≤ 1 s for exit; only then does it fall back to a
tree kill (`taskkill /PID <pid> /T /F` on Windows; `kill()` elsewhere). This order matters
because in the packaged onefile build the spawned handle is the PyInstaller bootloader, whose
Python child does the HTTP work: killing the handle first leaves the child alive, while EOF
reaches the child directly (its stdin is the same pipe). The Windows Job Object is assigned
after `Popen` as an extra layer for the dev/`python.exe` case and for grandchildren spawned
after assignment; it is documented as best-effort and never tested as the guarantee.

**Why `llm_test` tests candidates and accepts truncation.** The owner types a key and wants to
know it works before saving. `llm_test` merges the candidate over the effective settings,
validates it through `AgentConfig` (same normalisation as a save), calls `chat()` with
`strict=False` so a reply cut off by `max_tokens` still counts (auth, connectivity, model and
envelope are all proven at that point, D4), and persists nothing.

### Module design

**`taskpaw_v3/core/llm.py`**

```
DEFAULT_LLM_API_BASE = "https://openrouter.ai/api/v1"
DEFAULT_LLM_MODEL = "x-ai/grok-4.1-fast"
LLM_KEY_ENV = "TASKPAW_LLM_API_KEY"
KeySource = Literal["env", "config", "none"]

@dataclass(frozen=True)
class LLMSettings: api_base: str; model: str; api_key: str; key_source: KeySource

def resolve_llm_settings(api_base: str, model: str, api_key: str, *, environ=None) -> LLMSettings
    # env = environ.get(LLM_KEY_ENV, "").strip(); env → (env, "env"); elif api_key.strip() → (stripped, "config"); else ("", "none")
def llm_settings_from_config(config: AgentConfig, *, environ=None) -> LLMSettings
def set_llm_settings(s: LLMSettings) -> None      # lock-protected module global
def get_llm_settings() -> LLMSettings             # before init: defaults with key_source "none"
def reset_llm_settings() -> None                  # test hook (autouse fixture)

class LLMError(Exception): kind: Literal[...]; status: Optional[int]; message: str   # message is a fixed string
@dataclass(frozen=True)
class ChatResult: content: str; finish_reason: str; model: str; latency_ms: int

def chat(settings, messages, *, temperature=0.3, max_tokens=None, json_mode=False,
         timeout=30.0, strict=True, opener=None) -> ChatResult
```

`chat()` builds `POST {api_base}/chat/completions` with body `{model, messages, temperature,
[max_tokens], [response_format]}`, headers `Content-Type: application/json`,
`User-Agent: TaskPaw/<__version__>`, and `Authorization: Bearer <key>` added with
`Request.add_unredirected_header` only when the key is non-empty. `opener` is an
`OpenerDirector`-like object used as `opener.open(request, timeout=timeout)` (D16); it
defaults to a module-level `urllib.request.build_opener(_NoRedirect())` where `_NoRedirect`
is an `HTTPRedirectHandler` whose `redirect_request` returns `None` (so any 3xx surfaces as
`HTTPError`, verified by the critic's E9); tests inject a fake with the same `.open`
signature.

Exception → `LLMError` mapping, in this order, and **nothing else escapes**:
- `ValueError`/`UnicodeEncodeError` raised while building or sending the request (illegal
  header characters, D1) → `auth`, message `"API key or URL contains invalid characters"`.
- `HTTPError` 401/403 → `auth`; 429 → `rate_limit`; 3xx → `bad_response` (`"HTTP <code>
  redirect not followed"`); any other → `bad_response` (`"HTTP <code>"`).
- `URLError`, `socket.timeout`, `OSError`, `http.client.HTTPException` (`IncompleteRead`,
  `BadStatusLine`, `LineTooLong`, `RemoteDisconnected`, D2) → `network` (`"network error"` /
  `"timeout"`).
- Body not JSON / not an object / `choices` missing or empty / `choices[0].message` missing →
  `bad_response` (`"invalid response"`).
- Any other `Exception` → `bad_response` (`"unexpected error: <ExceptionTypeName>"`).

Envelope checks, in this order (D3): `finish_reason = choice.get("finish_reason") or "stop"`;
`content_filter` → `refusal`; `message.refusal` non-empty → `refusal`; `length` → `bad_response`
(`"finish_reason=length"`) **unless `strict=False`**, in which case the reply is accepted as
proof of connectivity: the content checks below are **skipped**, a non-string content is
normalised to `""`, and `ChatResult(content, "length", …)` is returned (D14 — a reasoning
model that spends the whole budget thinking returns `null`/empty content with `length`);
any other value ≠ `stop` (e.g. `tool_calls`) → `bad_response` (`"finish_reason=<value>"`);
`content` not a `str` (e.g. `null` with tool calls) → `bad_response`; `content.strip() == ""`
→ `refusal` (`"empty reply"`). Success returns `ChatResult(content, finish_reason,
model=body.get("model") or settings.model, latency_ms)`.

Logging: exactly one `log.info` per call — `"llm chat: ok model=%s latency_ms=%d"` or
`"llm chat: %s status=%s latency_ms=%d"` (kind/status). Never the key, prompt, headers, or body.

**`taskpaw_v3/core/llm_worker.py`**

```
ENV_BASE = "TASKPAW_LLM_API_BASE"; ENV_MODEL = "TASKPAW_LLM_MODEL"; ENV_KEY = LLM_KEY_ENV
def worker_argv() -> list[str]        # sys.frozen → [sys.executable, "llm-worker"]; else [sys.executable, "-m", "taskpaw_v3.core.llm_worker"]
def worker_env(settings: LLMSettings, base: Mapping[str,str] | None = None) -> dict[str,str]
    # copy of base (default os.environ) with ENV_BASE/ENV_MODEL set; ENV_KEY set iff key non-empty, else removed
def settings_from_env(environ) -> LLMSettings
def handle_request(line: str, settings: LLMSettings, chat_fn=chat) -> str   # pure; never raises
def serve(stdin: BinaryIO, stdout: TextIO, settings, chat_fn=chat, exit_fn=os._exit) -> int   # returns 0 only under an injected exit_fn (D17)
def main(argv=None) -> int
def assign_kill_on_close_job(proc: subprocess.Popen) -> Optional[JobKeeper]   # Windows only; best-effort
```

Request line: `{"id": <any JSON scalar>, "messages": [...], "temperature"?: float,
"max_tokens"?: int, "json_mode"?: bool, "timeout"?: float, "api_base"?: str, "model"?: str}`.
`api_base`/`model` override the env values for that request (live-apply); the key cannot be
overridden. Response line: `{"id", "ok": true, "content", "finish_reason", "model",
"latency_ms"}` or `{"id", "ok": false, "kind", "status", "message"}`. `handle_request` never
raises (D2): an unparseable line or a missing `messages` list answers `{"id": <id or null>,
"ok": false, "kind": "bad_response", "status": null, "message": "invalid request"}`; any
non-`LLMError` exception from `chat_fn` answers the same shape with `"message": "unexpected
error: <TypeName>"`. Lines are UTF-8, `\n`-terminated (stdout opened/reconfigured with
`newline="\n"`), flushed after each write; the parent strips `\r\n` defensively.

`serve()` starts the watcher thread (daemon, name `llm-worker-stdin`) which reads `stdin` line
by line into a `queue.Queue`; **on EOF it puts a `None` sentinel on the queue and then calls
`exit_fn(0)` immediately and unconditionally** (D5, D15) — there is no idle/busy distinction.
In production `exit_fn` is `os._exit`, so the process ends before the sentinel matters; the
sentinel exists so that an injected `exit_fn` in tests (which merely records the call) lets the
main loop return 0 instead of blocking in `queue.get()` forever. The main loop pops a line,
handles it, writes the reply, repeats; on the sentinel it returns 0. stderr gets
`logging.basicConfig(level=INFO)`; log lines are the same kind/status/latency lines only.
`main()` reads settings from the environment and runs `serve()` on
`sys.stdin.buffer`/`sys.stdout`; its return value is `serve()`'s (reached only under an
injected `exit_fn`).

`assign_kill_on_close_job(proc)`: on `sys.platform == "win32"`, `ctypes.windll.kernel32`
`CreateJobObjectW(None, None)` → `SetInformationJobObject(job, 9, &ext, sizeof)` with
`BasicLimitInformation.LimitFlags = 0x2000` → `AssignProcessToJobObject(job,
int(proc._handle))`. Returns a `JobKeeper` whose `close()` (and `__del__`) calls
`CloseHandle`; any failure logs one warning and returns `None`; non-Windows returns `None`.
Documented limitation (D6): a grandchild that the child spawned before the assignment is not
in the job. The caller keeps the keeper alive for the worker's lifetime.

**Parent-side cancel recipe (consumed by #177's `ChildProcess`, stated here as the contract):**
1. close the worker's stdin; 2. wait up to 1 s for exit; 3. if still alive, tree-kill
(`taskkill /PID <pid> /T /F` on Windows, `kill()` elsewhere); 4. close the `JobKeeper`.
Writing to a dead worker's stdin raises `OSError` (errno 22 on Windows, `BrokenPipeError`
elsewhere) — #177 must treat both as "worker gone".

**`backend_main.py`**: role `llm-worker` → `from taskpaw_v3.core.llm_worker import main`;
the unknown-role message lists `agent`, `hub`, `llm-worker`.

**`config.py`**: three fields with validators `_norm_llm_base` (strip, `rstrip("/")`, scheme
check when non-empty), `_strip_llm_model`, `_strip_llm_key` (strip; D1).

**`admin.py`**: `_EDITABLE_CONFIG += ("llm_api_base", "llm_model", "llm_api_key")`;
`_LIVE_CONFIG = ("api_token", "llm_api_base", "llm_model", "llm_api_key")`;
`_NON_LIVE_CONFIG = tuple(f for f in _EDITABLE_CONFIG if f not in _LIVE_CONFIG)`.
`update_config`: `llm_api_key` in the patch: `None` → explicit clear (`""`); a string that
is blank or `***` after strip → dropped from the patch (keep); otherwise the stripped value.
After `_save` succeeds and `_desired` is committed, set the three fields on `self._config`
and call `set_llm_settings(llm_settings_from_config(validated))`. New `llm_test(candidate:
dict) -> dict`: under `self._lock` build `overrides` from candidate keys `llm_api_base`,
`llm_model`, `llm_api_key` (each dropped when blank/`***`; `None` treated as absent) and
validate `AgentConfig(**{**self._config.model_dump(), **self._desired, **overrides})` (→
`ValueError` for a bad base → 400 via the route), resolve settings (env-first), then **release
the lock** (D11) and call `chat(settings, [{"role": "user", "content": "Reply with the single
word OK."}], max_tokens=16, timeout=20, strict=False)`; return `{"ok": True, "model":
r.model, "latency_ms": r.latency_ms, "truncated": r.finish_reason == "length"}`; on `LLMError`
return `{"ok": False, "error": f"{e.kind}: {e.message}" + (f" (HTTP {e.status})" if e.status
else "")}`; on any other exception return `{"ok": False, "error": "unexpected error:
<TypeName>"}` (never `str(e)`, D1). It touches neither `_desired`, `_config`, disk, nor the
holder. `handle()` gains `if command == "llm_test": return self.llm_test(dict(body.get("candidate") or body))`.

**`app.py`** `get_config`: after the `api_token` mask, `src = llm_settings_from_config(
AgentConfig(**{**data_without_extras}))`… — simpler and equivalent: `s = resolve_llm_settings(
data.get("llm_api_base", ""), data.get("llm_model", ""), str(data.get("llm_api_key") or ""))`;
`data["llm_api_key_source"] = s.key_source`; `if s.key_source != "none": data["llm_api_key"] =
"***"` else `""`. New route under `if admin is not None:`: `@app.post("/control/llm-test")
def llm_test(body: dict): return _guard(admin.llm_test, body)` (400 only for the validation
`ValueError`; `LLMError` never reaches the route).

**`launcher.py`** `run_agent`: immediately after `guard_bind_exposure(...)`:
`set_llm_settings(llm_settings_from_config(config))` — before `reclaim_ports_from_stale_instance`,
any socket claim, and the supervisor, so the first `check()` of any monitor reads real settings.

**UI**: `api.ts` — config type gains `llm_api_key_source?: "env" | "config" | "none"`;
`llmTest: (candidate) => send<{ok: boolean; model?: string; latency_ms?: number; truncated?:
boolean; error?: string}>("agent", "POST", "/control/llm-test", candidate)`; `updateConfig`
already passes arbitrary patches (a `null` key is sent as JSON `null`). `Settings.tsx` — new
`LlmSection()` rendered for the agent role between the config card and About: `TextField`s
for base URL and model (seeded from `/control/config`), password `TextField` for the key
(placeholder `***` when the config reports a key, `disabled` + helper text when
`llm_api_key_source === "env"`), buttons **Save LLM settings** (PATCH the three fields; blank
key omitted), **Clear key** (PATCH `llm_api_key: null`; hidden when source is `env`) and
**Test connection** (POST current form values; blank key omitted). Result in an `Alert`
(success: model + latency, plus a "reply truncated" note when `truncated`; error: the
backend `error`). Follows the existing card/TextField/Button pattern (MASTER.md: no emoji;
Test and Clear are secondary buttons). i18n keys under `settings.`: `llm`, `llmHint`,
`llmApiBase`, `llmModel`, `llmApiKey`, `llmApiKeyHint`, `llmApiKeyEnv`, `llmSave`,
`llmClear`, `llmTest`, `llmTesting`, `llmTestOk`, `llmTestTruncated`, `llmTestFail`,
`llmSaved`, `llmCleared`.

## Files to change

| Path | Change | Reason |
|------|--------|--------|
| `taskpaw_v3/core/config.py` | modify | three fields + validators (key stripped) |
| `taskpaw_v3/core/llm.py` | create | settings, holder (+ reset), `chat()`, no-redirect opener, `LLMError`, `ChatResult` |
| `taskpaw_v3/core/llm_worker.py` | create | worker protocol, `worker_argv/env`, stdin watcher (unconditional exit), Job Object helper |
| `taskpaw_v3/packaging/backend_main.py` | modify | `llm-worker` role |
| `taskpaw_v3/agent/server/admin.py` | modify | live set, live-apply, clear-on-null, `llm_test` (lock scope), dispatch |
| `taskpaw_v3/agent/server/app.py` | modify | mask + source via resolver in GET; `/control/llm-test` route |
| `taskpaw_v3/agent/server/launcher.py` | modify | holder init before reclaim/ports/supervisor |
| `taskpaw_v3/examples/agent.example.yaml` | modify | documented keys |
| `taskpaw_v3/ui/src/api.ts` | modify | `llmTest`, config type |
| `taskpaw_v3/ui/src/views/Settings.tsx` | modify | LLM card (save / clear / test) |
| `taskpaw_v3/ui/src/i18n.ts` | modify | zh+en strings |
| `taskpaw_v3/__init__.py`, `src-tauri/tauri.conf.json`, `src-tauri/Cargo.toml`, `src-tauri/Cargo.lock`, `ui/package.json`, `ui/package-lock.json` | modify | 3.3.0 |
| `taskpaw_v3/tests/conftest.py` | modify | autouse fixture: reset holder, delenv `TASKPAW_LLM_*` (D8) |
| `taskpaw_v3/tests/test_llm.py` | create | llm + worker + job object |
| `taskpaw_v3/tests/test_admin.py`, `test_agent.py`, `test_launcher.py`, `test_packaging.py`, `test_core.py` | modify | per test plan |
| `taskpaw_v3/ui/src/test/settings.test.tsx` | modify | scope existing Save query; LLM card tests (D9) |
| `CHANGELOG.md`, `README.md` | modify | 3.3.0 section; settings line |
| `docs/specs/2026-09-24-178-llm-settings-design.md` | create | this doc |

## Execution surface

Writes: the files above only. Reads/executes: `uv run pytest`, `uv run ruff check .`,
`uv run ruff format --check .`, `uv run mypy`, `cd taskpaw_v3/ui && npm run lint && npx
vitest run`. Tests spawn `sys.executable -m taskpaw_v3.core.llm_worker` as a real child and a
local `http.server` on an ephemeral loopback port; no network egress; no real API key is ever
read (the autouse fixture deletes `TASKPAW_LLM_API_KEY`/`_BASE`/`_MODEL`; tests set them
explicitly via `monkeypatch`). `run_agent` is never allowed to reach
`reclaim_ports_from_stale_instance` or a socket claim in tests (D7). PyInstaller is not run
(A6/A7 are the owner's packaged smoke). No version-file generator: the six files are edited
by hand and `test_version.py` asserts agreement.

## Key implementation notes

- `chat()` must send `Authorization` only when the key is non-empty (local Ollama) and via
  `add_unredirected_header`; assert absence/presence in tests.
- `LLMError.message` values are the fixed strings listed above; never interpolate exception
  text, headers, or body content.
- `update_config` ordering: validate → guard → save → commit desired → live-apply (`_config`
  fields + holder). A failed `save_yaml` must leave the holder untouched (test).
- `llm_test` must not touch `_desired`, `_config`, disk, or the holder, and must not hold
  `self._lock` during `chat()`.
- GET masking: when the key comes from env and the stored key is empty, the UI still sees `***`
  with `llm_api_key_source: "env"`; a whitespace-only stored key reports `none` (resolver).
- Worker: `sys.stdout.reconfigure(newline="\n")` at start of `main()`; read `sys.stdin.buffer`;
  decode UTF-8 with `errors="replace"`.
- Worker `main()`: `logging.basicConfig(level=logging.INFO, stream=sys.stderr)`; never log
  request content.
- `worker_env()` must not mutate `os.environ`.
- `assign_kill_on_close_job` keeps the job handle in the returned keeper; closing the keeper
  kills the child — the caller keeps it alive while the worker should live.
- `backend_main` imports the worker lazily (same pattern as agent/hub).
- Six version files: `package-lock.json` has two occurrences (root `version` and
  `packages[""].version`).
- Existing `settings.test.tsx` uses `getByRole("button", {name: /Save|保存/})`; with two Save
  buttons on the page it must be scoped with `within(...)`; the LLM card's button has its own
  label (`settings.llmSave`).

## Risk assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Provider ignores `response_format` (A2) | low | #177 batch rejected by JSON validation | out of this issue; documented |
| Holder read before init in some entry path | low | defaults with no key | `get_llm_settings()` returns explicit `key_source="none"`; #177 alerts once |
| Job Object misses the onefile grandchild (A5/D6) | expected | none, if the recipe is followed | cancel = close stdin → EOF watcher exits the real process → tree kill fallback; T-W5 tests the EOF path |
| `llm_test` holds the control request thread on a drip response | low | UI waits | bounded socket timeout; runs outside the admin lock; operator-initiated |
| Key leaks via exception text | low | high | fixed-string messages; catch-alls; tests grep caplog, HTTP responses and worker stderr |
| Windows pipe newline mangling | low | protocol breaks | explicit `newline="\n"` + binary stdin + defensive `rstrip("\r\n")`; worker integration test |
| Stored key sent to a LAN host after switching base (D12) | low | key disclosure to a LAN box | explicit clear (`null`) + UI button |

## Out of scope

See non-goals. Also: any change to `hub.yaml`; a UI for choosing among providers; storing the
key in an OS keychain; a wall-clock deadline inside `chat()` (the worker is the deadline).

## Test plan

**Fixture — `taskpaw_v3/tests/conftest.py`**: autouse `_llm_isolation(monkeypatch)`:
`reset_llm_settings()` before and after each test; `monkeypatch.delenv` for
`TASKPAW_LLM_API_KEY`, `TASKPAW_LLM_API_BASE`, `TASKPAW_LLM_MODEL` (raising=False) (D8).

**Unit — `taskpaw_v3/tests/test_llm.py`**
- T-S1 `resolve_llm_settings`: env non-blank wins (`"env"`, stripped); whitespace-only env falls
  to config (`"config"`, stripped); both blank → `""`/`"none"`.
- T-S2 holder: before init → defaults with `"none"`; `set_` then `get_` returns the same
  frozen snapshot; `reset_` restores the default.
- T-C1 `chat()` request shape with an injected opener: URL `<base>/chat/completions`, method
  POST, JSON body (`model`, `messages`, `temperature`, `max_tokens` only when given,
  `response_format` only when `json_mode`), `Authorization` present only with a key **and
  registered as unredirected**, `User-Agent` set, `timeout` forwarded.
- T-C2 success → `ChatResult`; missing `finish_reason` → `"stop"`; `strict=False` returns a
  `length` result, `strict=True` raises `bad_response` with `"finish_reason=length"`;
  `strict=False` + `length` + `content: null` (and + `content: ""`) → `ChatResult(content="",
  finish_reason="length")` (D14).
- T-C3 mapping table (each row one test or parametrised, through an injected fake opener
  exposing `.open(request, timeout=)`): 401/403 → `auth`; 429 → `rate_limit`; an `HTTPError`
  302 → `bad_response` (`redirect not followed`); 500 → `bad_response` with `status`;
  `URLError`/`socket.timeout`/
  `http.client.IncompleteRead`/`RemoteDisconnected` → `network`; invalid JSON / non-object /
  empty `choices` / missing `message` → `bad_response`; `content_filter` / `refusal` field /
  empty string → `refusal`; `tool_calls` with `content: null` → `bad_response`; a `ValueError`
  raised by the opener (illegal header) → `auth` with the fixed message; a `RuntimeError`
  from the opener → `bad_response` `"unexpected error: RuntimeError"`.
- T-C4 secrets: with a key containing a marker string and a `\n` (CR/LF) — `chat()` raises
  `auth` whose `str()` does not contain the marker; caplog never contains the key marker,
  the prompt marker, or a response-body marker across success and every error path.
- T-C5 default opener, loopback (D16): a local `http.server` on `127.0.0.1:0` whose `/v1/chat/
  completions` answers `302 Location: http://127.0.0.1:<second port>/leak`, with a second
  server recording every request; `chat()` with the **default** opener → `bad_response`
  `"HTTP 302 redirect not followed"`, the second server received nothing, and the first
  server's recorded request carried `Authorization` (the unredirected header was sent to the
  intended origin only).
- T-W1 `worker_argv()` frozen vs not (`sys.frozen` monkeypatched); `worker_env()` sets the three
  vars, removes `TASKPAW_LLM_API_KEY` when the key is empty, does not mutate `os.environ`;
  `settings_from_env()` round-trips.
- T-W2 `handle_request()` pure: success line; `LLMError` → error line with the same `id`
  (string and integer ids echoed unchanged); per-request `api_base`/`model` override reaches
  `chat_fn`; a `TASKPAW_LLM_API_KEY`-like key in the request is ignored; garbage line →
  `id: null` error line; `chat_fn` raising `RuntimeError` → error line `unexpected error:
  RuntimeError`; output is single-line JSON with `\n` only.
- T-W3 `serve()` with in-memory streams and an injected `exit_fn` that only records its
  argument (D15): two requests then EOF → two responses in order, `exit_fn(0)` called once,
  `serve()` returns 0 on the sentinel; EOF while a request is in flight (`chat_fn` blocks on
  an `Event`) → `exit_fn(0)` is called within 1 s **before** the request completes (assert the
  call while the event is still unset), then the test sets the event and `serve()` returns 0.
- T-W4 (Windows only) `assign_kill_on_close_job`: spawn `python -c "import time;
  time.sleep(60)"`, assign, close the keeper → child exits within 2 s; non-Windows → `None`.
- T-W5 integration (real subprocess via `worker_argv()` + local `http.server` in a thread on
  `127.0.0.1:0`, env from `worker_env()`): (a) a normal request round-trips through the real
  `chat()`; (b) a 500 maps to an error line; (c) argv contains no key while the env does;
  (d) **close-stdin cancel**: with the fake server holding the connection open, closing the
  worker's stdin makes the worker exit within 1 s; (e) (Windows only) with the server dripping
  one byte per second, `taskkill /PID <pid> /T /F` ends the tree and the parent's pipe read
  returns within 1 s; (f) worker stderr contains no key marker.

**Unit — config / admin / app / launcher / packaging**
- T-K1 `test_core.py`: defaults; base normalisation (`" https://x/v1/ "` → `https://x/v1`);
  `ftp://` rejected; empty base allowed; model and key stripped (key with trailing `\r\n`);
  `agent.example.yaml` loads and contains the three keys.
- T-A1 `update_config` with the three fields → `restart_required False`, values persisted,
  `_config` updated, holder updated.
- T-A2 blank/`***` `llm_api_key` keeps the stored key; `null` clears it (persisted `""`, holder
  `"none"`); with the env var set, a PATCH never writes the env value into the YAML.
- T-A3 `save_yaml` raising → holder and `_config` untouched.
- T-A4 `llm_test` with a monkeypatched `chat` in the admin module: candidate base/model reach
  `chat` with `strict=False`; blank key → effective key (env first); `***` → stored; nothing
  persisted (`_desired`, YAML, holder unchanged); `LLMError` → `{ok: False, error}` containing
  the kind and no key; `RuntimeError` → `unexpected error: RuntimeError`; a `length` result →
  `ok: True, truncated: True`; bad base → `ValueError`; the admin lock is **not** held while
  `chat` runs (the fake `chat` asserts `admin._lock.acquire(blocking=False)` succeeds).
- T-A5 `handle("llm_test", ...)` dispatches.
- T-G1 `test_agent.py`: GET masks the stored key and reports `config`; env set and no stored key
  → `***` + `env`; whitespace-only stored key → `""` + `none`; neither → `""` + `none`; the
  response never carries the key. Both construction paths (with `admin` and without).
- T-G2 route `POST /control/llm-test` → 200 with the admin's dict; bad base → 400 whose
  `detail` contains no key.
- T-L1 `test_launcher.py`: `run_agent` with non-default ports and
  `launcher.reclaim_ports_from_stale_instance` monkeypatched to raise a sentinel → the sentinel
  propagates and `get_llm_settings()` already reflects the config (holder init precedes the
  reclaim, hence every socket claim and the supervisor). Nothing is bound.
- T-L2 `test_launcher.py` (D13): non-default ports; `launcher.reclaim_ports_from_stale_instance`
  monkeypatched to a no-op; `claim_port` monkeypatched to return unbound dummy sockets;
  `build_supervisor` monkeypatched to a fake whose `start()` records `get_llm_settings()`;
  `uvicorn.Server`/`uvicorn.Config` stubbed so `run()` returns at once; a `GracefulShutdown`
  passed in with `install_signal_handlers` stubbed (pytest's SIGINT/SIGTERM handlers must
  survive); `block=False` → the fake records the configured model at `start()` (ordering by
  observation, as the issue asks); `shutdown.shutdown()` afterwards. Implementer note:
  `run_agent` imports `build_supervisor` and `MonitorAdmin` inside the function body, so the
  stub must target `taskpaw_v3.monitors.runtime.build_supervisor`, not a `launcher`
  attribute (`reclaim_ports_from_stale_instance` and `claim_port` ARE launcher attributes).
- T-P1 `test_packaging.py`: `backend_main.main(["llm-worker"])` dispatches to the worker's
  `main` (monkeypatched); unknown-role message names all three roles.
- T-V1 `test_version.py` passes at 3.3.0.

**UI — `settings.test.tsx`**
- T-U0 the existing save test scopes its button query to the agent-config card (`within`).
- T-U1 the LLM card renders with base/model seeded from the mocked config; the key input is
  `type="password"`, disabled with the env hint when `llm_api_key_source === "env"`, and the
  Clear button is hidden then.
- T-U2 Save LLM settings calls `api.updateConfig` with the three fields (blank key omitted)
  and invalidates `["agentConfig"]`; Clear key calls it with `llm_api_key: null`.
- T-U3 Test connection calls `api.llmTest` with the current form values (blank key omitted)
  and renders the success (model + latency), truncated, and error alerts.

**Edge cases**: `api_base` with a trailing slash; key of spaces; env var set to spaces; key with
CR/LF; provider returning 200 with `choices: []`; 3xx to another origin; worker request `id`
as string vs number; non-ASCII content through the worker pipe.

**Regression risk areas**: `api_token` masking/keep/live-apply; `restart_required` for
machine/ports; `config_view` pending semantics; `/control/config` `auth_disabled`; existing
`backend_main` roles; `test_version.py`; the existing Settings save test (D9).

**Manual smoke (owner, after merge)**: Settings → LLM API → fill an OpenRouter key → Test
connection → success with latency; refresh → key shows `***`; change model → no restart
prompt; Clear key → source `none`; `%APPDATA%\TaskPaw\agent.yaml` shows the three keys;
`setx TASKPAW_LLM_API_KEY …`, restart TaskPaw → card shows "provided by environment"; packaged
build: start `taskpaw-backend llm-worker` with a pipe (e.g. a 5-line Python snippet), write
one request line, read one reply line, close stdin → exit 0 within 1 s.

## Handoff notes

- Implement in this order: conftest fixture → config → `llm.py` → `llm_worker.py` →
  `backend_main` → admin/app → launcher → UI → version/docs; write each RED test before its
  slice (tests-first).
- Do not add retries, respawn, or any policy to `chat()`/worker: #177 owns policy.
- The worker integration test must bind the fake server to `127.0.0.1:0` and pass the port via
  `worker_env()`; never rely on a default port; never let `run_agent` reach the port reclaim.
- Keep `chat()` free of `requests`/`httpx`: runtime deps are fixed (`pyproject.toml`).
- mypy is scoped to `taskpaw_v3/` (tests excluded); the ctypes code needs an `if sys.platform
  == "win32":` block that mypy narrows (or `# type: ignore[attr-defined]` for `windll`).
- On Windows the dev `python.exe` inside the uv venv is itself a launcher (critic E6): the
  process doing HTTP is its child. That is one more reason the tests assert the close-stdin
  path, not `kill()`.
