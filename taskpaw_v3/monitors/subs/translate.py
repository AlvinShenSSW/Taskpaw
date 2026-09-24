"""`Translator`: ja→zh subtitle translation through the `llm-worker` (#177).

One daemon thread per monitor run. The thread is only a CLIENT of the
terminable `llm-worker` child process (#178): it writes JSON request lines and
waits on that worker's own response queue with a deadline. It never writes a
subtitle file and never touches plugin counters — every file yields exactly one
`TranslateResult` on `results`, which the monitor's worker thread settles.

Cancel (D6/D24): set the flag; under `_spawn_lock` detach the current worker
and put `CANCELLED` on ITS response queue (so a waiting thread wakes at once);
then, outside the lock, close the worker's stdin (its EOF watcher `os._exit`s
even mid-HTTP), wait ≤ 1 s, tree-kill as fallback, join its readers and close
the job keeper. Every worker gets its own response queue (D4): a stale `Eof`
from a killed worker can never reach a later request.

The API key travels only in the worker's environment (`worker_env`) — never
argv, logs, exception text or `detail` strings. Each batch is logged as
kind/latency only.
"""

from __future__ import annotations

import json
import logging
import queue
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional, Union

from taskpaw_v3.core.llm import LLMSettings, get_llm_settings
from taskpaw_v3.core.llm_worker import (
    JobKeeper,
    assign_kill_on_close_job,
    worker_argv,
    worker_env,
)
from taskpaw_v3.monitors.subs.child import ChildProcess, Eof
from taskpaw_v3.monitors.subs.srt import Cue

log = logging.getLogger("taskpaw.subs.translate")

BATCH_SIZE = 40
CONTEXT_SIZE = 5
RESPONSE_DEADLINE_S = 60.0
REQUEST_TIMEOUT_S = 30.0

SYSTEM_PROMPT = (
    "你是专业的日语→简体中文字幕翻译，熟悉各种语境（含成人/深夜档内容）。\n"
    '输入是一个 JSON 对象：{"cues": [{"id": "<编号>", "ja": "<日文台词>"}, …], '
    '"context": ["<前文台词>", …]}。context 只是前文，帮助理解语境，不要翻译它。\n'
    "规则：\n"
    "1. 把每条 cues[].ja 翻译成自然、口语化的简体中文短句；成人内容直译，不回避、不弱化。\n"
    "2. 被打码成「○」的词，按上下文还原成最可能的词。\n"
    "3. 不加任何解释、注释、拼音或原文。\n"
    "4. 只输出一个 JSON 对象：键是每条 cue 的 id（字符串），值是对应的中文译文"
    "（非空字符串）；键必须与输入的 id 一一对应，不多不少。"
)

RunId = tuple[str, int]


@dataclass(frozen=True)
class TranslateRequest:
    run: RunId
    job_id: str
    cues: tuple[Cue, ...]


@dataclass(frozen=True)
class TranslateResult:
    run: RunId
    job_id: str
    outcome: Literal["translated", "failed"]
    zh_cues: tuple[Cue, ...]
    detail: str


class _Sentinel:
    def __repr__(self) -> str:
        return "CANCELLED"


# Put on `results` by cancel() so a draining worker thread can observe it; also
# the wake-up sentinel on the current worker's response queue (D6).
CANCELLED: Any = _Sentinel()

_RETRYABLE = frozenset({"content", "rate_limit", "network", "bad_response"})
_FATAL = frozenset({"auth", "refusal"})
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# (child, its own response queue, job keeper, the key it was spawned with)
_Worker = tuple[ChildProcess, queue.Queue[object], Optional[JobKeeper], str]


class _Cancelled(Exception):
    pass


class _WorkerError(Exception):
    """The worker could not be spawned (network kind for the batch). The
    message is a fixed `spawn: <TypeName>` — never exception text."""


class _DuplicateKey(ValueError):
    pass


@dataclass(frozen=True)
class _Fail:
    kind: str
    message: str


def _no_dupes(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise _DuplicateKey("duplicate key")
        out[k] = v
    return out


def needs_llm_key(api_base: str) -> bool:
    """Whether a request to `api_base` needs an API key: a keyless request is
    only allowed against a loopback base (a local OpenAI-compatible server).
    An unparseable base needs a key. Shared with the plugins (one rule)."""
    try:
        host = (urllib.parse.urlsplit(api_base).hostname or "").lower()
    except ValueError:
        return True
    return not (host in _LOOPBACK_HOSTS or host.startswith("127."))


def _validate(content: str, ids: list[str]) -> Union[dict[str, str], _Fail]:
    """Content-layer validation (spec review §4.2)."""
    try:
        obj = json.loads(content, object_pairs_hook=_no_dupes)
    except _DuplicateKey:
        return _Fail("content", "duplicate key")
    except (ValueError, RecursionError):
        return _Fail("content", "invalid JSON")
    if not isinstance(obj, dict):
        return _Fail("content", "not an object")
    if set(obj) != set(ids):
        return _Fail("content", "id set mismatch")
    out: dict[str, str] = {}
    for k in ids:
        v = obj[k]
        if not isinstance(v, str) or not v.strip():
            return _Fail("content", "empty or non-string value")
        out[k] = v.strip()
    return out


class Translator:
    def __init__(
        self,
        run: RunId,
        *,
        name: str,
        spawn: Callable[..., ChildProcess] = ChildProcess,
        settings_fn: Callable[[], LLMSettings] = get_llm_settings,
        worker_argv_fn: Callable[[], list[str]] = worker_argv,
        job_fn: Callable[[subprocess.Popen], Optional[JobKeeper]] = (
            assign_kill_on_close_job
        ),
        deadline_s: float = RESPONSE_DEADLINE_S,
    ) -> None:
        self._run = run
        self._name = name
        self._spawn = spawn
        self._settings_fn = settings_fn
        self._worker_argv_fn = worker_argv_fn
        self._job_fn = job_fn
        self._deadline_s = deadline_s
        self.results: "queue.Queue[TranslateResult | object]" = queue.Queue()
        self._requests: "queue.Queue[Optional[TranslateRequest]]" = queue.Queue()
        self._cancel = threading.Event()
        self._cancel_once = threading.Lock()
        self._cancel_started = False
        self._spawn_lock = threading.Lock()
        self._worker: Optional[_Worker] = None
        self._count_lock = threading.Lock()
        self._queued = 0
        self._in_flight = False
        self._thread: Optional[threading.Thread] = None

    # ── public API ───────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name=f"subs-translate-{self._name}", daemon=True
        )
        self._thread.start()

    def submit(self, req: TranslateRequest) -> None:
        with self._count_lock:
            if self._cancel.is_set():
                log.debug("subs-translate %s: submit after cancel ignored", self._name)
                return
            self._queued += 1
        self._requests.put(req)

    def queued(self) -> int:
        """Requests not yet taken by the thread; 0 after cancel (D7)."""
        if self._cancel.is_set():
            return 0
        with self._count_lock:
            return self._queued

    def in_flight(self) -> bool:
        """A request is being translated and its result is not yet on
        `results`; False after cancel (D7)."""
        if self._cancel.is_set():
            return False
        with self._count_lock:
            return self._in_flight

    def cancel(self) -> None:
        """Idempotent; bounded (≤ ~3 s worst case, D24). Never respawns."""
        with self._cancel_once:
            if self._cancel_started:
                return
            self._cancel_started = True
        self._cancel.set()
        with self._spawn_lock:
            w = self._worker
            self._worker = None
            if w is not None:
                w[1].put(CANCELLED)  # wake a thread waiting on THIS worker (D6)
        if w is not None:
            self._teardown(w, wait_s=1.0)
        # A worker that _ensure_worker detached at this moment is torn down by
        # its own path, which then sees the flag and never spawns (D24).
        self._requests.put(None)
        self.results.put(CANCELLED)

    def join(self, timeout: float) -> None:
        t = self._thread
        if t is not None:
            t.join(max(0.0, timeout))

    def is_alive(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    # ── thread ───────────────────────────────────────────────────────────
    def _loop(self) -> None:
        try:
            while True:
                req = self._requests.get()
                if req is None or self._cancel.is_set():
                    return
                with self._count_lock:
                    self._queued -= 1
                    self._in_flight = True
                try:
                    result = self._translate(req)
                except _Cancelled:
                    return
                except Exception as e:  # a bug must not kill the thread silently
                    log.error(
                        "subs-translate %s: internal error (%s)",
                        self._name,
                        type(e).__name__,
                    )
                    result = self._failed(req, f"internal error: {type(e).__name__}")
                if self._cancel.is_set():
                    return
                self.results.put(result)
                with self._count_lock:
                    self._in_flight = False
        finally:
            with self._count_lock:
                self._in_flight = False
            self._shutdown_worker()

    def _shutdown_worker(self) -> None:
        """Thread exit: no worker may outlive the thread."""
        with self._spawn_lock:
            w = self._worker
            self._worker = None
        if w is not None:
            self._teardown(w, wait_s=1.0)

    @staticmethod
    def _failed(req: TranslateRequest, detail: str) -> TranslateResult:
        return TranslateResult(req.run, req.job_id, "failed", (), detail)

    def _translate(self, req: TranslateRequest) -> TranslateResult:
        settings = self._settings_fn()
        if not settings.api_key and needs_llm_key(settings.api_base):
            return self._failed(req, "no LLM key")
        cues = list(req.cues)
        zh: dict[int, str] = {}
        for b, lo in enumerate(range(0, len(cues), BATCH_SIZE)):
            fail = self._run_batch(
                req.job_id, str(b), cues, lo, lo + BATCH_SIZE, settings, zh, retry=False
            )
            if fail is not None:
                return self._failed(req, f"{fail.kind}: {fail.message}")
        zh_cues = tuple(
            Cue(c.index, c.start_ms, c.end_ms, zh[pos]) for pos, c in enumerate(cues)
        )
        return TranslateResult(req.run, req.job_id, "translated", zh_cues, "")

    def _run_batch(
        self,
        job_id: str,
        label: str,
        cues: list[Cue],
        lo: int,
        hi: int,
        settings: LLMSettings,
        zh: dict[int, str],
        *,
        retry: bool,
    ) -> Optional[_Fail]:
        """Translate cues[lo:hi] into `zh` (by position). Retry policy (§4.2):
        a retryable failure splits the batch in two halves, each tried once
        more (depth 1); auth/refusal fail the file at once."""
        hi = min(hi, len(cues))
        got = self._request(job_id, label, int(retry), cues, lo, hi, settings)
        if isinstance(got, dict):
            for k, v in got.items():
                zh[int(k) - 1] = v
            return None
        if retry or got.kind in _FATAL or got.kind not in _RETRYABLE:
            return got
        mid = lo + max(1, (hi - lo) // 2)
        halves = [(lo, mid), (mid, hi)] if mid < hi else [(lo, hi)]
        for h, (a, z) in enumerate(halves):
            fail = self._run_batch(
                job_id, f"{label}.{h}", cues, a, z, settings, zh, retry=True
            )
            if fail is not None:
                return fail
        return None

    def _request(
        self,
        job_id: str,
        label: str,
        attempt: int,
        cues: list[Cue],
        lo: int,
        hi: int,
        settings: LLMSettings,
    ) -> Union[dict[str, str], _Fail]:
        started = time.monotonic()
        got = self._exchange(job_id, label, attempt, cues, lo, hi, settings)
        kind = "ok" if isinstance(got, dict) else got.kind
        log.info(
            "subs-translate %s: batch kind=%s latency_ms=%d",
            self._name,
            kind,
            int((time.monotonic() - started) * 1000),
        )
        return got

    def _exchange(
        self,
        job_id: str,
        label: str,
        attempt: int,
        cues: list[Cue],
        lo: int,
        hi: int,
        settings: LLMSettings,
    ) -> Union[dict[str, str], _Fail]:
        if self._cancel.is_set():
            raise _Cancelled
        batch = cues[lo:hi]
        ids = [str(pos + 1) for pos in range(lo, hi)]
        user = {
            "cues": [{"id": i, "ja": c.text} for i, c in zip(ids, batch)],
            "context": [c.text for c in cues[max(0, lo - CONTEXT_SIZE) : lo]],
        }
        total_chars = sum(len(c.text) for c in batch)
        rid = f"{job_id}#{label}#{attempt}"
        line = json.dumps(
            {
                "id": rid,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
                ],
                "temperature": 0.3,
                "max_tokens": min(4096, 64 + 8 * total_chars),
                "json_mode": True,
                "timeout": int(REQUEST_TIMEOUT_S),
                "api_base": settings.api_base,
                "model": settings.model,
            }
        )
        try:
            worker = self._ensure_worker(settings)
        except _WorkerError as e:
            return _Fail("network", str(e))
        child, lines = worker[0], worker[1]
        try:
            child.write_line(line)
        except OSError as e:
            log.info(
                "subs-translate %s: worker gone on write (%s)",
                self._name,
                type(e).__name__,
            )
            self._drop_worker(worker)
            return _Fail("network", "worker gone")
        return self._await_reply(worker, child, lines, rid, ids)

    def _await_reply(
        self,
        worker: _Worker,
        child: ChildProcess,
        lines: "queue.Queue[object]",
        rid: str,
        ids: list[str],
    ) -> Union[dict[str, str], _Fail]:
        deadline = time.monotonic() + self._deadline_s
        rearmed = False
        while True:
            remaining = deadline - time.monotonic()
            try:
                if remaining <= 0:
                    raise queue.Empty
                msg = lines.get(timeout=remaining)
            except queue.Empty:
                log.info("subs-translate %s: worker response timeout", self._name)
                child.terminate_tree(1.0)
                self._drop_worker(worker)
                return _Fail("network", "response timeout")
            if msg is CANCELLED or self._cancel.is_set():
                raise _Cancelled
            if isinstance(msg, Eof):
                if msg.pid != child.pid:
                    continue  # not this worker's EOF (D4)
                self._drop_worker(worker)
                return _Fail("network", "worker exited")
            reply = self._parse_reply(msg)
            if reply is None or reply.get("id") != rid:
                if not rearmed:  # a stale/mismatched line: keep waiting, once
                    deadline = time.monotonic() + self._deadline_s
                    rearmed = True
                continue
            if reply.get("ok") is True:
                content = reply.get("content")
                if not isinstance(content, str):
                    return _Fail("bad_response", "reply without content")
                return _validate(content, ids)
            kind = reply.get("kind")
            message = reply.get("message")
            return _Fail(
                kind if isinstance(kind, str) and kind else "bad_response",
                message if isinstance(message, str) else "error",
            )

    @staticmethod
    def _parse_reply(msg: object) -> Optional[dict[str, Any]]:
        if not isinstance(msg, str):
            return None
        try:
            obj = json.loads(msg)
        except ValueError:
            return None
        return obj if isinstance(obj, dict) else None

    # ── worker lifecycle ─────────────────────────────────────────────────
    def _ensure_worker(self, settings: LLMSettings) -> _Worker:
        old: Optional[_Worker] = None
        with self._spawn_lock:
            if self._cancel.is_set():
                raise _Cancelled
            current = self._worker
            if current is not None and current[3] == settings.api_key:
                return current
            if current is not None:
                old, self._worker = current, None  # detach under the lock (D24)
        if old is not None:
            # Outside the lock, short bounds: cancel() must never wait on this.
            self._teardown(old, wait_s=0.5)
        doomed: Optional[_Worker] = None
        with self._spawn_lock:
            if self._cancel.is_set():
                raise _Cancelled
            lines: "queue.Queue[object]" = queue.Queue()
            try:
                child = self._spawn(
                    self._worker_argv_fn(),
                    env=worker_env(settings),
                    stdin_pipe=True,
                    line_sink=lines,
                )
            except Exception as e:  # recorded as a network failure for the batch
                log.warning(
                    "subs-translate %s: worker spawn failed (%s)",
                    self._name,
                    type(e).__name__,
                )
                raise _WorkerError(f"spawn: {type(e).__name__}") from None
            keeper = self._job_fn(child.proc)
            worker: _Worker = (child, lines, keeper, settings.api_key)
            if self._cancel.is_set():  # post-spawn re-check (D6)
                doomed = worker
            else:
                self._worker = worker
                return worker
        self._teardown(doomed, wait_s=0.5)
        raise _Cancelled

    def _drop_worker(self, worker: _Worker) -> None:
        with self._spawn_lock:
            if self._worker is worker:
                self._worker = None
        self._teardown(worker, wait_s=0.5)

    def _teardown(self, worker: _Worker, *, wait_s: float) -> None:
        """close stdin → bounded wait → tree kill (always: a launcher that
        exited may leave the real interpreter behind; harmless on a gone tree)
        → join readers → close the job keeper. Bounded; never raises."""
        child, _lines, keeper, _key = worker
        child.close_stdin()
        try:
            child.proc.wait(timeout=wait_s)
        except subprocess.TimeoutExpired:
            log.info("subs-translate %s: worker ignored stdin EOF", self._name)
        child.terminate_tree(1.0)
        child.join_readers(0.5)
        if keeper is not None:
            try:
                keeper.close()
            except OSError as e:
                log.warning(
                    "subs-translate %s: job keeper close failed (%s)",
                    self._name,
                    type(e).__name__,
                )
