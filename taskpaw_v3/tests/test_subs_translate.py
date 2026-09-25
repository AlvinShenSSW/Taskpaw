"""`Translator`: the llm-worker client thread (#177, subs/translate.py) and its
resumable, multi-provider engine (#192/#190).

Unit tests drive fake workers injected through `spawn` that speak the
llm-worker JSON-lines protocol, a fake monotonic clock and a fake `wait_fn`
(waits advance the fake clock at once, so a 2 h pause runs in milliseconds);
the integration tests run the REAL `llm_worker` (real `worker_argv()`) against
a loopback `http.server` on 127.0.0.1:0. No network, no default ports, no real
key (the autouse conftest fixture strips TASKPAW_LLM_*), checkpoints only under
`tmp_path`."""

from __future__ import annotations

import itertools
import json
import logging
import math
import os
import queue
import subprocess
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import pytest

from taskpaw_v3.core.llm import LLMSettings
from taskpaw_v3.core.llm_worker import ENV_BASE, ENV_KEY, ENV_MODEL
from taskpaw_v3.monitors.subs import srt
from taskpaw_v3.monitors.subs.checkpoint import (
    CHECKPOINT_MAX_AGE_S,
    CheckpointStore,
    SavedCue,
)
from taskpaw_v3.monitors.subs.child import Eof
from taskpaw_v3.monitors.subs.srt import Cue
from taskpaw_v3.monitors.subs.translate import (
    BATCH_SIZE,
    BREAKER_S,
    CANCELLED,
    CONTEXT_SIZE,
    LEAF_RETRY_S,
    MIN_BATCH_SIZE,
    MODEL_LABEL_CHARS,
    NO_LLM_KEY,
    NOTICE_CHECKPOINT,
    NOTICE_PROVIDER,
    PAUSE_AFTER_S,
    PROBE_JA,
    PROBE_TTL_S,
    REQUEST_TIMEOUT_S,
    RESPONSE_DEADLINE_S,
    RETRY_AFTER_CAP_S,
    RETRY_SCHEDULE_S,
    SYSTEM_PROMPT,
    TRANSLATION_PAUSED,
    WAIT_SLICE_S,
    Notice,
    TranslateRequest,
    TranslateResult,
    Translator,
    model_label,
    needs_llm_key,
    probe_messages,
    probe_ok,
)
from taskpaw_v3.monitors.subs.util import step_numbers

REPO_ROOT = Path(__file__).resolve().parents[2]
KEY = "sk-KEYMARKER-subs-5d1e"
KEY2 = "sk-KEYMARKER-subs-other-77aa"
KEY_DS = "sk-KEYMARKER-subs-ds-31c0"
KEY_MI = "sk-KEYMARKER-subs-mimo-8e2b"
RUN = ("inst", 3)

GROK = LLMSettings("https://api.x.ai/v1", "grok", KEY, "config")
DS = LLMSettings("https://api.deepseek.com/v1", "ds", KEY_DS, "config")
MIMO = LLMSettings("https://api.mimo.example/v1", "mimo", KEY_MI, "config")
L_DEFAULT = "m/x · llm.example"
L_GROK = "grok · api.x.ai"
L_DS = "ds · api.deepseek.com"
L_MIMO = "mimo · api.mimo.example"


def _cues(n: int, prefix: str = "ja") -> tuple[Cue, ...]:
    return tuple(Cue(i + 1, i * 1000, i * 1000 + 500, f"{prefix}{i}") for i in range(n))


def _settings(key: str = KEY, base: str = "https://llm.example/v1") -> LLMSettings:
    return LLMSettings(api_base=base, model="m/x", api_key=key, key_source="config")


# ── fake clock + wait (#192: every wait goes through `wait_fn`) ──────────
class FakeClock:
    """A monotonic clock that only moves when the translator waits. `hook`
    (optional) runs inside every wait, BEFORE the clock advances — a test's
    window into the translator while it sleeps."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start
        self.waits: list[float] = []
        self.hook: Optional[Callable[[float], None]] = None
        self.cancelled: Callable[[], bool] = lambda: False

    def __call__(self) -> float:
        return self.t

    def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        hook = self.hook
        if hook is not None:
            hook(seconds)
        self.t += seconds
        return self.cancelled()


# ── fake worker speaking the protocol ────────────────────────────────────
_pids = itertools.count(50000)


class FakeProc:
    def __init__(self, w: "FakeWorker") -> None:
        self._w = w
        self.pid = w.pid

    def wait(self, timeout: Optional[float] = None) -> int:
        if self._w.dead.wait(30 if timeout is None else timeout):
            return 0
        raise subprocess.TimeoutExpired("fake-worker", timeout or 0)

    def poll(self) -> Optional[int]:
        return 0 if self._w.dead.is_set() else None


Responder = Callable[[dict, "FakeWorker"], Optional[dict]]


class FakeWorker:
    def __init__(
        self,
        argv: list[str],
        env: Optional[dict],
        sink: Any,
        responder: Responder,
        log: list[str],
        *,
        ignore_close: bool = False,
        write_error: bool = False,
        silent_die: bool = False,
        record: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self.argv = argv
        self.env = env
        self.sink = sink
        self.responder = responder
        self.log = log
        self.pid = next(_pids)
        self.proc = FakeProc(self)
        self.dead = threading.Event()
        self.stdin_closed = False
        self.requests: list[dict] = []
        self.ignore_close = ignore_close
        self.write_error = write_error
        # silent_die: dying emits NO `Eof`, so only cancel()'s CANCELLED sentinel
        # on this worker's response queue can wake a waiting thread (D6).
        self.silent_die = silent_die
        self.record = record

    def _event(self, what: str) -> None:
        self.log.append(f"{self.pid}:{what}")

    def poll(self) -> Optional[int]:
        return self.proc.poll()

    def write_line(self, line: str) -> None:
        if self.write_error or self.dead.is_set() or self.stdin_closed:
            raise OSError(22, "Invalid argument")
        req = json.loads(line)
        self.requests.append(req)
        if self.record is not None:
            self.record(req)
        reply = self.responder(req, self)
        if reply is not None:
            self.sink.put(json.dumps(reply))

    def close_stdin(self) -> None:
        self._event("close_stdin")
        self.stdin_closed = True
        if not self.ignore_close:
            self.die()

    def terminate_tree(self, timeout: float = 5.0) -> None:
        self._event("terminate_tree")
        self.die()

    def join_readers(self, timeout: float = 2.0) -> None:
        self._event("join_readers")

    def tail(self, lines: int = 10, max_chars: int = 800) -> str:
        return ""

    def die(self) -> None:
        if not self.dead.is_set():
            self.dead.set()
            if not self.silent_die:
                self.sink.put(Eof(self.pid))


class Spawner:
    def __init__(self, responder: Responder, **worker_kw: Any) -> None:
        self.responder = responder
        self.worker_kw = worker_kw
        self.workers: list[FakeWorker] = []
        self.calls: list[dict] = []
        self.log: list[str] = []
        self.fail_times = 0
        self.per_worker_kw: list[dict] = []  # overrides for the Nth spawn
        # every request of every worker, in order, with the fake clock's time
        self.requests: list[dict] = []
        self.times: list[float] = []
        self.clock: Optional[Callable[[], float]] = None

    def _record(self, req: dict) -> None:
        self.requests.append(req)
        self.times.append(self.clock() if self.clock is not None else 0.0)

    def __call__(self, argv, *, env=None, stdin_pipe=False, line_sink=None, **kw):
        self.calls.append(
            {"argv": argv, "env": env, "stdin_pipe": stdin_pipe, "kw": kw}
        )
        if self.fail_times:
            self.fail_times -= 1
            raise OSError(f"spawn failed {KEY}")  # text must never surface
        wkw = dict(self.worker_kw)
        n = len(self.workers)
        if n < len(self.per_worker_kw):
            wkw.update(self.per_worker_kw[n])
        w = FakeWorker(
            argv, env, line_sink, self.responder, self.log, record=self._record, **wkw
        )
        self.workers.append(w)
        return w


def _user(req: dict) -> dict:
    return json.loads(req["messages"][1]["content"])


def _ids(req: dict) -> list[str]:
    return [c["id"] for c in _user(req)["cues"]]


def _jas(req: dict) -> list[str]:
    return [c["ja"] for c in _user(req)["cues"]]


def _is_probe(req: dict) -> bool:
    u = _user(req)
    return u["context"] == [] and [c["ja"] for c in u["cues"]] == [PROBE_JA]


def _seq(req: dict) -> str:
    """A request as a short string: "P" for the probe, else its cue ids as
    ranges ("1-40", "3", "14-20,22-45")."""
    if _is_probe(req):
        return "P"
    ids = [int(i) for i in _ids(req)]
    parts: list[str] = []
    start = prev = ids[0]
    for i in ids[1:] + [None]:  # type: ignore[list-item]
        if i is not None and i == prev + 1:
            prev = i
            continue
        parts.append(str(start) if start == prev else f"{start}-{prev}")
        if i is not None:
            start = prev = i
    return ",".join(parts)


def _of(sp: Spawner, model: str) -> list[str]:
    return [_seq(q) for q in sp.requests if q["model"] == model]


def _probe_times(sp: Spawner) -> list[float]:
    return [t for q, t in zip(sp.requests, sp.times) if _is_probe(q)]


def _ok(content: str, req: dict) -> dict:
    return {
        "id": req["id"],
        "ok": True,
        "content": content,
        "finish_reason": "stop",
        "model": "served",
        "latency_ms": 3,
    }


def _err(
    kind: str,
    req: dict,
    status: Optional[int] = None,
    message: str = "x",
    retry_after: Optional[int] = None,
) -> dict:
    return {
        "id": req["id"],
        "ok": False,
        "kind": kind,
        "status": status,
        "message": message,
        "retry_after": retry_after,
    }


def good(req: dict, w: FakeWorker) -> dict:
    return _ok(json.dumps({c["id"]: f"中{c['ja']}" for c in _user(req)["cues"]}), req)


def good_as(tag: str) -> Responder:
    def responder(req: dict, w: FakeWorker) -> dict:
        body = {c["id"]: f"{tag}{c['ja']}" for c in _user(req)["cues"]}
        return _ok(json.dumps(body, ensure_ascii=False), req)

    return responder


def forbid(req: dict, w: FakeWorker) -> dict:
    """xAI's content refusal (FC2): HTTP 403 for one batch, the key is fine."""
    return _err("auth", req, status=403, message="authentication failed")


def down(req: dict, w: FakeWorker) -> dict:
    return _err("network", req, message="network error")


def timeout(req: dict, w: FakeWorker) -> dict:
    return _err("network", req, message="timeout")


def empty_reply(req: dict, w: FakeWorker) -> dict:
    """SNOS: xAI's silent refusal — an empty reply."""
    return _err("refusal", req, message="empty reply")


def fail_when(texts: Iterable[str], reply: Responder, base: Responder = good):
    """Batches holding any of `texts` get `reply`; everything else (the
    probe included) gets `base`."""
    bad = set(texts)

    def responder(req: dict, w: FakeWorker) -> Optional[dict]:
        if not _is_probe(req) and bad & set(_jas(req)):
            return reply(req, w)
        return base(req, w)

    return responder


def by_model(**routes: Responder) -> Responder:
    def responder(req: dict, w: FakeWorker) -> Optional[dict]:
        return routes[req["model"]](req, w)

    return responder


def scripted(*steps: Any) -> Responder:
    """Per-call behaviour: a callable(req, w) → reply, or "good"."""
    it = iter(steps)

    def responder(req: dict, w: FakeWorker) -> Optional[dict]:
        step = next(it, "good")
        if step == "good":
            return good(req, w)
        return step(req, w)

    return responder


class Harness:
    def __init__(
        self,
        spawner: Any,
        settings: Optional[LLMSettings] = None,
        deadline_s: float = 5.0,
        job_fn: Optional[Callable] = None,
        *,
        chain: Optional[list[LLMSettings]] = None,
        failover: bool = True,
        checkpoint_dir: Optional[Path] = None,
        clock: Optional[FakeClock] = None,
    ) -> None:
        self.spawner = spawner
        self.chain: list[LLMSettings] = (
            list(chain) if chain is not None else [settings or _settings()]
        )
        self.failover = failover
        self.clock = clock if clock is not None else FakeClock()
        if isinstance(spawner, Spawner):
            spawner.clock = self.clock
        self.keepers: list[Any] = []
        self.tr = Translator(
            RUN,
            name="t",
            spawn=spawner,
            chain_fn=lambda: tuple(self.chain),
            failover_fn=lambda: self.failover,
            checkpoint_dir=checkpoint_dir,
            clock=self.clock,
            wait_fn=self.clock.wait,
            worker_argv_fn=lambda: ["worker-argv", "llm-worker"],
            job_fn=job_fn or (lambda proc: None),
            deadline_s=deadline_s,
        )
        self.clock.cancelled = self.tr._cancel.is_set
        self.tr.start()

    @property
    def settings(self) -> LLMSettings:
        return self.chain[0]

    @settings.setter
    def settings(self, s: LLMSettings) -> None:
        self.chain = [s]  # a Settings save: the chain is re-read at once

    def run(self, cues: tuple[Cue, ...], job_id: str = "a.mp4") -> TranslateResult:
        self.tr.submit(TranslateRequest(RUN, job_id, cues))
        return self.result()

    def result(self, timeout: float = 10.0) -> TranslateResult:
        r = self.tr.results.get(timeout=timeout)
        assert isinstance(r, TranslateResult), r
        return r

    def close(self) -> None:
        self.tr.cancel()
        self.tr.join(5)
        assert not self.tr.is_alive()


@pytest.fixture
def harness_factory():
    made: list[Harness] = []

    def make(*a: Any, **kw: Any) -> Harness:
        h = Harness(*a, **kw)
        made.append(h)
        return h

    yield make
    for h in made:
        h.close()


def _deltas(ts: list[float]) -> list[float]:
    return [b - a for a, b in zip(ts, ts[1:])]


def test_constants():
    assert (BATCH_SIZE, CONTEXT_SIZE, MIN_BATCH_SIZE) == (40, 5, 5)
    assert RESPONSE_DEADLINE_S == 60.0 and REQUEST_TIMEOUT_S == 30.0
    assert "JSON" in SYSTEM_PROMPT or "json" in SYSTEM_PROMPT
    assert "○" in SYSTEM_PROMPT
    # #192 AC5–AC8
    assert RETRY_SCHEDULE_S == (10.0, 30.0, 90.0) and RETRY_AFTER_CAP_S == 300.0
    assert LEAF_RETRY_S == 10.0 and PROBE_TTL_S == 60.0
    assert BREAKER_S == (300.0, 900.0, 1800.0)
    assert PAUSE_AFTER_S == 7200.0 and WAIT_SLICE_S == 60.0


def test_translate_result_keeps_positional_construction():
    # The plugins' fakes build it positionally: new fields have defaults.
    r = TranslateResult(RUN, "a", "translated", (), "")
    assert (r.resumed, r.fallback, r.kept_ja, r.checkpoint_key) == (0, 0, 0, "")


def test_batch_shaping_and_success(harness_factory):
    sp = Spawner(good)
    h = harness_factory(sp)
    cues = _cues(90)
    r = h.run(cues)
    assert r.run == RUN and r.job_id == "a.mp4" and r.outcome == "translated"
    assert r.zh_cues == tuple(
        Cue(c.index, c.start_ms, c.end_ms, f"中{c.text}") for c in cues
    )
    assert (r.resumed, r.fallback, r.kept_ja) == (0, 0, 0)
    assert len(r.checkpoint_key) == 64
    reqs = sp.workers[0].requests
    assert len(reqs) == 3 and len(sp.workers) == 1
    for b, req in enumerate(reqs):
        lo = b * BATCH_SIZE
        batch = cues[lo : lo + BATCH_SIZE]
        user = _user(req)
        assert [c["id"] for c in user["cues"]] == [
            str(lo + i + 1) for i in range(len(batch))
        ]
        assert all(isinstance(c["id"], str) for c in user["cues"])
        assert [c["ja"] for c in user["cues"]] == [c.text for c in batch]
        ctx = cues[max(0, lo - CONTEXT_SIZE) : lo]
        assert user["context"] == [c.text for c in ctx]
        assert req["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
        assert req["messages"][1]["role"] == "user"
        assert req["json_mode"] is True
        assert req["temperature"] == 0.3
        assert req["timeout"] == 30
        assert req["api_base"] == "https://llm.example/v1" and req["model"] == "m/x"
        chars = sum(len(c.text) for c in batch)
        assert req["max_tokens"] == min(4096, 64 + 8 * chars)
        assert req["id"].startswith("a.mp4#")
    assert len({req["id"] for req in reqs}) == 3  # unique request ids (D4)
    assert KEY not in json.dumps(reqs)
    assert h.clock.waits == []


def test_max_tokens_is_bounded(harness_factory):
    sp = Spawner(good)
    h = harness_factory(sp)
    assert h.run(_cues(3, prefix="長" * 400)).outcome == "translated"
    assert sp.workers[0].requests[0]["max_tokens"] == 4096


def test_spawn_arguments_env_and_job_keeper(harness_factory):
    sp = Spawner(good)
    procs: list[Any] = []
    h = harness_factory(sp, job_fn=lambda proc: procs.append(proc))
    assert h.run(_cues(2)).outcome == "translated"
    call = sp.calls[0]
    assert call["argv"] == ["worker-argv", "llm-worker"]
    assert call["stdin_pipe"] is True
    assert call["env"][ENV_KEY] == KEY
    assert all(KEY not in a for a in call["argv"])
    assert procs == [sp.workers[0].proc]


def test_key_change_respawns_and_stale_eof_never_hits_next_request(harness_factory):
    sp = Spawner(good)
    h = harness_factory(sp)
    assert h.run(_cues(2), "a.mp4").outcome == "translated"
    old = sp.workers[0]
    h.settings = _settings(KEY2)
    # D4: the old worker keeps emitting a stale Eof after it was killed; it
    # lands on ITS OWN queue and must never end the next request.
    real_die = old.die

    def noisy_die() -> None:
        real_die()
        old.sink.put(Eof(old.pid))
        threading.Timer(0.05, lambda: old.sink.put(Eof(old.pid))).start()

    old.die = noisy_die  # type: ignore[method-assign]
    r = h.run(_cues(3), "b.mp4")
    assert r.outcome == "translated" and r.job_id == "b.mp4"
    assert len(sp.workers) == 2
    new = sp.workers[1]
    assert sp.calls[1]["env"][ENV_KEY] == KEY2
    assert new.requests and old.dead.is_set()
    old_events = [e.split(":", 1)[1] for e in sp.log if e.startswith(f"{old.pid}:")]
    assert old_events[0] == "close_stdin"  # stdin closed before any kill
    assert "join_readers" in old_events
    # Same key again → the worker is reused.
    assert h.run(_cues(1), "c.mp4").outcome == "translated"
    assert len(sp.workers) == 2


def test_key_change_old_worker_ignoring_close_is_killed_after_close(harness_factory):
    sp = Spawner(good)
    sp.per_worker_kw = [{"ignore_close": True}]
    h = harness_factory(sp)
    assert h.run(_cues(2)).outcome == "translated"
    old = sp.workers[0]
    h.settings = _settings(KEY2)
    assert h.run(_cues(2)).outcome == "translated"
    old_events = [e.split(":", 1)[1] for e in sp.log if e.startswith(f"{old.pid}:")]
    assert old_events[:2] == ["close_stdin", "terminate_tree"]


def _dupe(req: dict, w: FakeWorker) -> dict:
    ids = [c["id"] for c in _user(req)["cues"]]
    body = "{" + ", ".join(f'"{i}": "x"' for i in ids) + f', "{ids[0]}": "y"' + "}"
    return _ok(body, req)


def _missing(req: dict, w: FakeWorker) -> dict:
    ids = [c["id"] for c in _user(req)["cues"]]
    return _ok(json.dumps({i: "x" for i in ids[1:]}), req)


def _extra(req: dict, w: FakeWorker) -> dict:
    ids = [c["id"] for c in _user(req)["cues"]]
    return _ok(json.dumps({**{i: "x" for i in ids}, "999": "x"}), req)


def _empty_value(req: dict, w: FakeWorker) -> dict:
    ids = [c["id"] for c in _user(req)["cues"]]
    return _ok(
        json.dumps({i: ("  " if n == 0 else "x") for n, i in enumerate(ids)}), req
    )


def _non_string(req: dict, w: FakeWorker) -> dict:
    ids = [c["id"] for c in _user(req)["cues"]]
    return _ok(json.dumps({i: 5 for i in ids}), req)


def _not_object(req: dict, w: FakeWorker) -> dict:
    return _ok("[1, 2]", req)


def _not_json(req: dict, w: FakeWorker) -> dict:
    return _ok("here you go: {", req)


def _blank_only(req: dict, w: FakeWorker) -> dict:
    ids = [c["id"] for c in _user(req)["cues"]]
    blank = " \n\n \n"  # only blank/whitespace lines
    return _ok(
        json.dumps({i: (blank if n == 0 else "x") for n, i in enumerate(ids)}), req
    )


CONTENT_FAILURES = [
    _blank_only,
    _dupe,
    _missing,
    _extra,
    _empty_value,
    _non_string,
    _not_object,
    _not_json,
]


@pytest.mark.parametrize("bad", CONTENT_FAILURES)
def test_content_failure_half_batch_retry_then_success(harness_factory, bad):
    # AC5: invalid output → bisect at once (no schedule, no probe).
    sp = Spawner(scripted(bad))
    h = harness_factory(sp)
    cues = _cues(4)
    r = h.run(cues)
    assert r.outcome == "translated", r.detail
    assert [c.text for c in r.zh_cues] == [f"中{c.text}" for c in cues]
    reqs = sp.workers[0].requests
    assert len(reqs) == 3
    assert [c["id"] for c in _user(reqs[1])["cues"]] == ["1", "2"]
    assert [c["id"] for c in _user(reqs[2])["cues"]] == ["3", "4"]
    assert _user(reqs[2])["context"] == ["ja0", "ja1"]
    assert len({r["id"] for r in reqs}) == 3
    assert h.clock.waits == []


@pytest.mark.parametrize("bad", CONTENT_FAILURES)
def test_content_failure_twice_bisects_deeper(harness_factory, bad):
    # #192 (was: the second failure failed the file): each bisection node is
    # ONE request; a failed node splits again, down to single cues.
    sp = Spawner(scripted(bad, bad))
    h = harness_factory(sp)
    r = h.run(_cues(4))
    assert r.outcome == "translated" and r.kept_ja == 0
    assert [_seq(q) for q in sp.requests] == ["1-4", "1-2", "1", "2", "3-4"]


def test_content_failure_everywhere_is_kept_japanese_after_one_confirming_probe(
    harness_factory, tmp_path
):
    sp = Spawner(fail_when({"ja1"}, _not_json))
    h = harness_factory(sp, checkpoint_dir=tmp_path)
    r = h.run(_cues(3))
    assert r.outcome == "translated"
    assert [c.text for c in r.zh_cues] == ["中ja0", "ja1", "中ja2"]
    assert r.kept_ja == 1
    assert [_seq(q) for q in sp.requests] == ["1-3", "1", "2-3", "2", "3", "P"]
    saved = CheckpointStore(tmp_path).load(r.checkpoint_key, 3)
    assert saved is not None and saved[1] == SavedCue(refused_by=(L_DEFAULT,))


@pytest.mark.parametrize("kind", ["rate_limit", "network", "bad_response"])
def test_retryable_error_kinds_follow_the_schedule(harness_factory, kind):
    e = lambda req, w: _err(kind, req)  # noqa: E731
    sp = Spawner(scripted(e, e))
    h = harness_factory(sp)
    r = h.run(_cues(4))
    assert r.outcome == "translated" and kind not in r.detail
    assert [_seq(q) for q in sp.requests] == ["1-4"] * 3
    assert _deltas(sp.times) == [10.0, 30.0]


def test_single_cue_batch_retried_once(harness_factory):
    e = lambda req, w: _err("network", req)  # noqa: E731
    sp = Spawner(scripted(e))
    h = harness_factory(sp)
    assert h.run(_cues(1)).outcome == "translated"
    assert len(sp.workers[0].requests) == 2


def test_response_timeout_kills_worker_and_respawns(harness_factory):
    silent = lambda req, w: None  # noqa: E731
    sp = Spawner(scripted(silent))
    sp.per_worker_kw = [{"ignore_close": True}]
    h = harness_factory(sp, deadline_s=0.3)
    r = h.run(_cues(4))
    assert r.outcome == "translated"
    assert len(sp.workers) == 2
    first = sp.workers[0]
    assert f"{first.pid}:terminate_tree" in sp.log
    assert first.dead.is_set()


def test_eof_is_network_and_respawns(harness_factory):
    def crash(req: dict, w: FakeWorker) -> None:
        w.die()
        return None

    sp = Spawner(scripted(crash))
    h = harness_factory(sp)
    assert h.run(_cues(4)).outcome == "translated"
    assert len(sp.workers) == 2


def test_a_worker_crashing_on_every_request_pauses_with_one_notice(harness_factory):
    # #192 (was: the second EOF failed the file): an EOF is a network failure
    # → the schedule → the probe fails → the provider opens → the film is
    # deferred and, with no provider left, paused at 2 h (checkpoint kept).
    def crash(req: dict, w: FakeWorker) -> None:
        w.die()
        return None

    sp = Spawner(crash)
    h = harness_factory(sp)
    r = h.run(_cues(4))
    assert (r.outcome, r.detail, r.zh_cues) == ("paused", TRANSLATION_PAUSED, ())
    assert len(sp.workers) == len(sp.requests)  # a fresh worker per request
    notices = h.tr.drain_notices()
    assert len(notices) == 1 and "unreachable" in notices[0].message


def test_write_line_oserror_is_network(harness_factory):
    sp = Spawner(good)
    sp.per_worker_kw = [{"write_error": True}]
    h = harness_factory(sp)
    assert h.run(_cues(4)).outcome == "translated"
    assert len(sp.workers) == 2


def test_mismatched_reply_id_is_ignored(harness_factory):
    def mismatch(req: dict, w: FakeWorker) -> dict:
        w.sink.put(json.dumps({**good(req, w), "id": "stale#9#0"}))
        w.sink.put("not json at all")
        return good(req, w)

    sp = Spawner(scripted(mismatch))
    h = harness_factory(sp)
    assert h.run(_cues(3)).outcome == "translated"
    assert len(sp.workers[0].requests) == 1


def test_mismatched_reply_only_is_a_response_timeout_then_retried(harness_factory):
    # #192 (was: failed the file): the timeout is a network failure → the
    # schedule retries it after 10 s.
    def mismatch_only(req: dict, w: FakeWorker) -> dict:
        return {**good(req, w), "id": "stale#9#0"}

    sp = Spawner(scripted(mismatch_only))
    h = harness_factory(sp, deadline_s=0.2)
    t0 = time.monotonic()
    r = h.run(_cues(2))
    assert r.outcome == "translated"
    assert len(sp.requests) == 2 and _deltas(sp.times) == [10.0]
    assert time.monotonic() - t0 < 5


def test_spawn_raising_is_network_then_retry(harness_factory):
    sp = Spawner(good)
    sp.fail_times = 1
    h = harness_factory(sp)
    assert h.run(_cues(4)).outcome == "translated"


def test_spawn_always_raising_pauses_without_leaking(harness_factory, caplog):
    # #192 (was: failed with "spawn: OSError"): the provider is unreachable —
    # breaker + one notice, the film pauses; the exception text (with the
    # key in it) surfaces nowhere.
    caplog.set_level(logging.DEBUG)
    sp = Spawner(good)
    sp.fail_times = 10_000
    h = harness_factory(sp)
    r = h.run(_cues(4))
    assert r.outcome == "paused"
    notices = h.tr.drain_notices()
    assert len(notices) == 1 and "unreachable" in notices[0].message
    for text in (r.detail, caplog.text, repr(notices)):
        assert KEY not in text


def test_no_key_non_loopback_is_the_no_key_result(harness_factory):
    sp = Spawner(good)
    h = harness_factory(sp, settings=_settings(key=""))
    r = h.run(_cues(2))
    assert (r.outcome, r.detail, r.zh_cues) == ("no_key", NO_LLM_KEY, ())
    assert r.detail == "no LLM key"  # the avsubs check (C8) keeps working
    assert sp.calls == []


def test_no_key_loopback_base_is_allowed(harness_factory):
    sp = Spawner(good)
    h = harness_factory(sp, settings=_settings(key="", base="http://127.0.0.1:9/v1"))
    assert h.run(_cues(2)).outcome == "translated"
    assert ENV_KEY not in sp.calls[0]["env"]


def test_key_appears_later_live_apply(harness_factory):
    sp = Spawner(good)
    h = harness_factory(sp, settings=_settings(key=""))
    assert h.run(_cues(2)).detail == "no LLM key"
    h.settings = _settings()
    assert h.run(_cues(2)).outcome == "translated"


def test_empty_cue_list_translates_to_empty(harness_factory):
    sp = Spawner(good)
    h = harness_factory(sp)
    r = h.run(())
    assert r.outcome == "translated" and r.zh_cues == ()


def test_results_in_submission_order_with_run_ids(harness_factory):
    sp = Spawner(good)
    h = harness_factory(sp)
    for name in ("a", "b", "c"):
        h.tr.submit(TranslateRequest(("other", 99), name, _cues(1)))
    got = [h.result() for _ in range(3)]
    assert [r.job_id for r in got] == ["a", "b", "c"]
    assert all(r.run == ("other", 99) for r in got)


def test_logs_carry_kind_status_latency_and_label_only(harness_factory, caplog):
    caplog.set_level(logging.DEBUG)
    sp = Spawner(
        scripted(lambda req, w: _err("rate_limit", req, status=429, retry_after=3))
    )
    h = harness_factory(sp)
    cues = _cues(4, prefix="秘密のセリフ")
    assert h.run(cues).outcome == "translated"
    text = caplog.text
    assert "latency_ms" in text and "rate_limit" in text and "status=429" in text
    assert L_DEFAULT in text
    for marker in (KEY, "秘密のセリフ", "中秘密", "Bearer"):
        assert marker not in text


def test_a_film_named_probe_is_logged_as_batches(harness_factory, caplog):
    # B-2: the probe is flagged, never recognised by a name — a film called
    # "probe" logs its requests as batches; the real probe still logs "probe".
    caplog.set_level(logging.INFO, logger="taskpaw.subs.translate")
    sp = Spawner(scripted(forbid))  # 403 → the probe → bisect
    h = harness_factory(sp)
    assert h.run(_cues(4), job_id="probe").outcome == "translated"
    assert [_seq(q) for q in sp.requests] == ["1-4", "P", "1-2", "3-4"]
    kinds = [
        m.split(": ", 1)[1].split(" ", 1)[0]
        for m in caplog.messages
        if "latency_ms=" in m
    ]
    assert kinds == ["batch", "probe", "batch", "batch"]


def test_queued_and_in_flight_track_work(harness_factory):
    release = threading.Event()

    def slow(req: dict, w: FakeWorker) -> None:
        threading.Thread(
            target=lambda: (release.wait(10), w.sink.put(json.dumps(good(req, w)))),
            daemon=True,
        ).start()
        return None

    sp = Spawner(slow)
    h = harness_factory(sp)
    assert h.tr.queued() == 0 and h.tr.in_flight() is False
    h.tr.submit(TranslateRequest(RUN, "a", _cues(1)))
    h.tr.submit(TranslateRequest(RUN, "b", _cues(1)))
    deadline = time.monotonic() + 5
    while not sp.workers or not sp.workers[0].requests:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert h.tr.in_flight() is True and h.tr.queued() == 1
    release.set()
    assert [h.result().job_id, h.result().job_id] == ["a", "b"]
    deadline = time.monotonic() + 5
    while h.tr.in_flight():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert h.tr.queued() == 0


def test_cancel_while_waiting(harness_factory):
    sp = Spawner(lambda req, w: None)
    # silent_die: neither close_stdin nor terminate_tree produces an `Eof`, so the
    # waiting thread can only be woken by the D6 CANCELLED sentinel.
    sp.per_worker_kw = [{"ignore_close": True, "silent_die": True}]
    h = harness_factory(sp, deadline_s=30)
    tr = h.tr
    tr.submit(TranslateRequest(RUN, "a", _cues(2)))
    tr.submit(TranslateRequest(RUN, "b", _cues(2)))
    deadline = time.monotonic() + 5
    while not sp.workers or not sp.workers[0].requests:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert tr.queued() == 1 and tr.in_flight()
    t0 = time.monotonic()
    tr.cancel()
    assert time.monotonic() - t0 < 3.0
    t1 = time.monotonic()
    tr.join(1.0)
    assert not tr.is_alive()
    assert time.monotonic() - t1 < 1.0  # woken by the sentinel, not the deadline
    assert tr.queued() == 0 and tr.in_flight() is False
    assert len(sp.workers) == 1  # no respawn on the cancel path
    w = sp.workers[0]
    ev = [e.split(":", 1)[1] for e in sp.log if e.startswith(f"{w.pid}:")]
    assert ev.index("close_stdin") < ev.index("terminate_tree")
    assert "join_readers" in ev
    items = []
    while not tr.results.empty():
        items.append(tr.results.get_nowait())
    assert CANCELLED in items
    assert not [i for i in items if isinstance(i, TranslateResult)]
    tr.cancel()  # idempotent
    tr.submit(TranslateRequest(RUN, "late", _cues(1)))  # ignored after cancel
    assert tr.queued() == 0


def test_cancel_before_start_of_work_and_idle(harness_factory):
    sp = Spawner(good)
    h = harness_factory(sp)
    h.tr.cancel()
    h.tr.join(1.0)
    assert not h.tr.is_alive() and sp.calls == []


def test_cancel_during_spawn_kills_new_child(harness_factory):
    entered = threading.Event()
    release = threading.Event()
    sp = Spawner(good)
    real_call = sp.__call__

    def blocking_spawn(argv, **kw):
        entered.set()
        release.wait(10)
        return real_call(argv, **kw)

    h = harness_factory(blocking_spawn)
    tr = h.tr
    tr.submit(TranslateRequest(RUN, "a", _cues(2)))
    assert entered.wait(5)
    done = threading.Event()
    threading.Thread(target=lambda: (tr.cancel(), done.set()), daemon=True).start()
    deadline = time.monotonic() + 5
    while not tr._cancel.is_set():  # the flag is set before the lock is taken
        assert time.monotonic() < deadline
        time.sleep(0.005)
    release.set()
    assert done.wait(3)
    tr.join(1.0)
    assert not tr.is_alive()
    assert len(sp.workers) == 1
    w = sp.workers[0]
    assert w.dead.is_set() and w.requests == []
    assert f"{w.pid}:close_stdin" in sp.log
    assert tr.queued() == 0 and tr.in_flight() is False


def test_cancel_during_old_worker_teardown_on_key_change(harness_factory):
    # D24: the old worker ignores close_stdin and only dies on terminate_tree;
    # cancel() arrives while the thread retires it (H2, outside the lock) →
    # cancel returns fast, the thread joins, and no new worker spawns.
    sp = Spawner(good)
    sp.per_worker_kw = [{"ignore_close": True}]
    h = harness_factory(sp)
    tr = h.tr
    assert h.run(_cues(2), "a").outcome == "translated"
    old = sp.workers[0]
    h.settings = _settings(KEY2)
    tr.submit(TranslateRequest(RUN, "b", _cues(2)))
    deadline = time.monotonic() + 5
    while f"{old.pid}:close_stdin" not in sp.log:
        assert time.monotonic() < deadline
        time.sleep(0.005)
    t0 = time.monotonic()
    tr.cancel()
    assert time.monotonic() - t0 < 3.0
    tr.join(3.0)
    assert not tr.is_alive()
    assert len(sp.workers) == 1  # never spawned a new worker
    assert old.dead.is_set()
    assert f"{old.pid}:join_readers" in sp.log
    assert tr.queued() == 0 and tr.in_flight() is False


def test_thread_is_named_daemon(harness_factory):
    harness_factory(Spawner(good))
    t = [t for t in threading.enumerate() if t.name == "subs-translate-t"]
    assert t and t[0].daemon


# ── #192 AC5: the transient schedule (top-level requests only) ───────────
def test_transient_schedule_is_10_30_90_on_the_same_batch(harness_factory):
    sp = Spawner(scripted(timeout, timeout, timeout))
    h = harness_factory(sp)
    r = h.run(_cues(4))
    assert r.outcome == "translated" and r.kept_ja == 0
    assert [_seq(q) for q in sp.requests] == ["1-4"] * 4  # no probe needed
    assert _deltas(sp.times) == [10.0, 30.0, 90.0]
    assert max(h.clock.waits) <= WAIT_SLICE_S  # every wait is sliced (H2)


@pytest.mark.parametrize(
    "kind,status,retry_after,want",
    [
        ("rate_limit", 429, 7, 7.0),
        ("rate_limit", 429, 1000, 300.0),  # capped at 300 s
        ("bad_response", 503, 20, 20.0),
        ("rate_limit", 429, None, 10.0),  # none given → the schedule
        ("bad_response", 500, 20, 10.0),  # only 429/503 honour it
        ("bad_response", 408, None, 10.0),
        ("bad_response", None, None, 10.0),  # e.g. finish_reason=length
    ],
)
def test_retry_after_for_429_and_503_capped(
    harness_factory, kind, status, retry_after, want
):
    e = lambda req, w: _err(kind, req, status=status, retry_after=retry_after)  # noqa: E731
    sp = Spawner(scripted(e))
    h = harness_factory(sp)
    assert h.run(_cues(2)).outcome == "translated"
    assert _deltas(sp.times) == [want]
    assert max(h.clock.waits) <= WAIT_SLICE_S


def test_snos_empty_reply_is_retried_at_top_level_only_then_kept_japanese(
    harness_factory, tmp_path
):
    # The owner's LMNO-005: one line always gets an empty reply. It is retried
    # on the schedule at top level; the probe is fine → bisect; the single cue
    # gets NO leaf retry (H5: not transient there) → one confirming probe →
    # refused (persisted) → with no other provider, it keeps its Japanese.
    sp = Spawner(fail_when({"ja1"}, empty_reply))
    h = harness_factory(sp, checkpoint_dir=tmp_path)
    r = h.run(_cues(3))
    assert r.outcome == "translated"
    assert [c.text for c in r.zh_cues] == ["中ja0", "ja1", "中ja2"]
    assert (r.kept_ja, r.fallback) == (1, 0)
    assert [_seq(q) for q in sp.requests] == ["1-3"] * 4 + [
        "P",
        "1",
        "2-3",
        "2",
        "3",
        "P",
    ]
    assert _deltas(sp.times)[:3] == [10.0, 30.0, 90.0]
    assert sum(_deltas(sp.times)[3:]) == 0.0  # no leaf retry wait
    saved = CheckpointStore(tmp_path).load(r.checkpoint_key, 3)
    assert saved is not None
    assert saved[1] == SavedCue(refused_by=(L_DEFAULT,))
    assert saved[0] == SavedCue(zh="中ja0", by=L_DEFAULT)


# ── #192 AC6: probe → bisect → refused → fail over ───────────────────────
def test_fc2_content_refusal_only_the_refused_cue_fails_over(harness_factory, tmp_path):
    # The owner's ABC-3620789: grok answers one batch with HTTP 403 (a content
    # refusal; the key works). The probe passes → bisect → exactly that cue is
    # refused by grok and goes to the fallback; the rest stay on grok.
    sp = Spawner(by_model(grok=fail_when({"ja3"}, forbid), ds=good_as("DS")))
    h = harness_factory(sp, chain=[GROK, DS], checkpoint_dir=tmp_path)
    r = h.run(_cues(8))
    assert r.outcome == "translated"
    assert [c.text for c in r.zh_cues] == (
        [f"中ja{i}" for i in range(3)] + ["DSja3"] + [f"中ja{i}" for i in range(4, 8)]
    )
    assert (r.fallback, r.kept_ja, r.resumed) == (1, 0, 0)
    assert _of(sp, "grok") == ["1-8", "P", "1-4", "1-2", "3-4", "3", "4", "5-8", "P"]
    assert _of(sp, "ds") == ["4"]
    ds_req = next(q for q in sp.requests if q["model"] == "ds")
    assert _user(ds_req)["context"] == ["ja0", "ja1", "ja2"]
    assert h.clock.waits == []  # no schedule for a probe-class failure
    saved = CheckpointStore(tmp_path).load(r.checkpoint_key, 8)
    assert saved is not None
    assert saved[3] == SavedCue(zh="DSja3", by=L_DS, refused_by=(L_GROK,))
    assert saved[0] == SavedCue(zh="中ja0", by=L_GROK)
    assert h.tr.drain_notices() == []  # a content refusal is no outage


@pytest.mark.parametrize(
    "kind,status,message",
    [
        ("auth", 401, "authentication failed"),
        ("auth", 403, "authentication failed"),
        ("bad_response", 402, "HTTP 402"),
        ("bad_response", 404, "HTTP 404"),
        ("bad_response", 422, "HTTP 422"),
        ("refusal", None, "finish_reason=content_filter"),
        ("refusal", None, "model refused"),
    ],
)
def test_refusal_auth_and_4xx_probe_at_once(harness_factory, kind, status, message):
    # #192 (was: auth/refusal failed the file at once). The probe decides:
    # here it passes → content-specific → bisect; the halves succeed.
    once = lambda req, w: _err(kind, req, status=status, message=message)  # noqa: E731
    sp = Spawner(scripted(once))
    h = harness_factory(sp)
    r = h.run(_cues(4))
    assert r.outcome == "translated" and r.kept_ja == 0
    assert [_seq(q) for q in sp.requests] == ["1-4", "P", "1-2", "3-4"]
    assert h.clock.waits == []


def test_key_dying_mid_bisection_is_provider_level_not_refused(
    harness_factory, tmp_path
):
    # G6: the confirming probe fails → no leaf is recorded (the cue is not
    # "refused" by grok), grok opens, everything open goes to the fallback.
    state = {"dead": False}

    def grok(req: dict, w: FakeWorker) -> dict:
        if state["dead"]:
            return _err("auth", req, status=401)
        if not _is_probe(req) and "ja3" in _jas(req):
            if len(_ids(req)) == 1:
                state["dead"] = True
            return _err("auth", req, status=403)
        return good(req, w)

    sp = Spawner(by_model(grok=grok, ds=good_as("DS")))
    h = harness_factory(sp, chain=[GROK, DS], checkpoint_dir=tmp_path)
    r = h.run(_cues(8))
    assert r.outcome == "translated" and r.kept_ja == 0
    assert [c.text for c in r.zh_cues] == (
        [f"中ja{i}" for i in range(3)] + [f"DSja{i}" for i in range(3, 8)]
    )
    assert r.fallback == 5
    assert _of(sp, "ds") == ["4-8"]
    saved = CheckpointStore(tmp_path).load(r.checkpoint_key, 8)
    assert saved is not None and all(s.refused_by == () for s in saved)
    notices = h.tr.drain_notices()
    assert len(notices) == 1
    assert notices[0].key == f"{NOTICE_PROVIDER}{L_GROK}"
    assert "key or credit" in notices[0].message


def test_refused_by_every_provider_keeps_japanese(harness_factory, tmp_path):
    sp = Spawner(
        by_model(grok=fail_when({"ja1"}, forbid), ds=fail_when({"ja1"}, empty_reply))
    )
    h = harness_factory(sp, chain=[GROK, DS], checkpoint_dir=tmp_path)
    r = h.run(_cues(3))
    assert r.outcome == "translated"
    assert [c.text for c in r.zh_cues] == ["中ja0", "ja1", "中ja2"]
    assert (r.kept_ja, r.fallback) == (1, 0)
    saved = CheckpointStore(tmp_path).load(r.checkpoint_key, 3)
    assert saved is not None
    assert set(saved[1].refused_by) == {L_GROK, L_DS} and saved[1].zh is None


def test_h8_one_confirming_probe_for_all_failed_leaves(harness_factory):
    sp = Spawner(by_model(grok=fail_when({"ja1", "ja2", "ja5"}, forbid), ds=good))
    h = harness_factory(sp, chain=[GROK, DS])
    r = h.run(_cues(8))
    assert r.outcome == "translated" and r.fallback == 3
    grok = _of(sp, "grok")
    assert grok.count("P") == 2  # the top-level probe + ONE confirming probe
    assert len(grok) - 2 <= 1 + (2 * 8 - 2)  # the top request + 2n−2 nodes
    assert _of(sp, "ds") == ["2-3,6"]  # consecutive OPEN cues share a batch


def test_h8_timeouts_above_20_cues_halve_the_batch_size(harness_factory):
    slow = lambda req, w: timeout(req, w) if len(_ids(req)) > 20 else good(req, w)  # noqa: E731
    sp = Spawner(slow)
    h = harness_factory(sp)
    r = h.run(_cues(90))
    assert r.outcome == "translated" and r.kept_ja == 0
    assert [_seq(q) for q in sp.requests] == (
        ["1-40"] * 4 + ["P", "1-20", "21-40", "41-60", "61-80", "81-90"]
    )


def test_h8_adaptive_batch_size_floor_is_5(harness_factory):
    slow = lambda req, w: timeout(req, w) if len(_ids(req)) > 3 else good(req, w)  # noqa: E731
    sp = Spawner(slow)
    h = harness_factory(sp)
    r = h.run(_cues(120))
    assert r.outcome == "translated" and r.kept_ja == 0
    (p,) = h.tr._providers.values()
    assert p.batch_size == MIN_BATCH_SIZE


def test_a_length_cut_off_bisection_also_halves_the_batch(harness_factory):
    cut = lambda req, w: (  # noqa: E731
        _err("bad_response", req, message="finish_reason=length")
        if len(_ids(req)) > 20
        else good(req, w)
    )
    sp = Spawner(cut)
    h = harness_factory(sp)
    assert h.run(_cues(60)).outcome == "translated"
    assert [_seq(q) for q in sp.requests] == (
        ["1-40"] * 4 + ["P", "1-20", "21-40", "41-60"]
    )


def test_400_retries_without_json_mode_and_turns_it_off(harness_factory):
    def resp(req: dict, w: FakeWorker) -> dict:
        if req["json_mode"]:
            return _err("bad_response", req, status=400, message="HTTP 400")
        return good(req, w)

    sp = Spawner(resp)
    h = harness_factory(sp)
    assert h.run(_cues(90)).outcome == "translated"
    assert [q["json_mode"] for q in sp.requests] == [True, False, False, False]
    assert h.clock.waits == []


def test_probe_uses_the_same_no_json_retry(harness_factory):
    def resp(req: dict, w: FakeWorker) -> dict:
        if _is_probe(req):
            if req["json_mode"]:
                return _err("bad_response", req, status=400, message="HTTP 400")
            return good(req, w)
        if "ja1" in _jas(req):
            return forbid(req, w)
        return good(req, w)

    sp = Spawner(resp)
    h = harness_factory(sp)
    r = h.run(_cues(2))
    assert r.outcome == "translated" and r.kept_ja == 1
    assert [(_seq(q), q["json_mode"]) for q in sp.requests] == [
        ("1-2", True),
        ("P", True),
        ("P", False),  # 400 → once without json_mode → OK → off for the run
        ("1", False),
        ("2", False),
        ("P", False),
    ]


def test_400_without_json_mode_is_a_4xx_rejection(harness_factory):
    sp = Spawner(lambda req, w: _err("bad_response", req, status=400))
    h = harness_factory(sp)
    r = h.run(_cues(2))
    assert r.outcome == "paused"
    (n,) = h.tr.drain_notices()
    assert "rejected the request (HTTP 400)" in n.message


# ── H4: a failed probe → breaker + one notice whose text follows it ─────
PROBE_FAILURES = [
    ("auth401", lambda q: _err("auth", q, status=401), "key or credit", 1800.0),
    ("auth403", lambda q: _err("auth", q, status=403), "key or credit", 1800.0),
    (
        "credit402",
        lambda q: _err("bad_response", q, status=402),
        "key or credit",
        1800.0,
    ),
    (
        "missing404",
        lambda q: _err("bad_response", q, status=404),
        "model or URL not found",
        300.0,
    ),
    (
        "quota429",
        lambda q: _err("rate_limit", q, status=429),
        "rate limit or quota",
        300.0,
    ),
    (
        "teapot418",
        lambda q: _err("bad_response", q, status=418),
        "rejected the request (HTTP 418)",
        300.0,
    ),
    (
        "policy",
        lambda q: _err("refusal", q, message="finish_reason=content_filter"),
        "content policy rejected the translation prompt",
        1800.0,
    ),
    (
        "invalid",
        lambda q: _ok('{"1": ""}', q),  # envelope OK, content unusable (H4)
        "returns unusable output",
        300.0,
    ),
    (
        "network",
        lambda q: _err("network", q, message="network error"),
        "unreachable",
        300.0,
    ),
    ("server500", lambda q: _err("bad_response", q, status=500), "unreachable", 300.0),
]


@pytest.mark.parametrize(
    "reply,text,first_open",
    [p[1:] for p in PROBE_FAILURES],
    ids=[p[0] for p in PROBE_FAILURES],
)
def test_failed_probe_opens_the_breaker_with_one_notice(
    harness_factory, caplog, reply, text, first_open
):
    caplog.set_level(logging.DEBUG)
    sp = Spawner(lambda req, w: reply(req))
    h = harness_factory(sp)
    r = h.run(_cues(2))
    assert (r.outcome, r.detail, r.zh_cues) == ("paused", TRANSLATION_PAUSED, ())
    gaps = _deltas(_probe_times(sp))
    # auth / 402 / content policy start at 30 min; the rest at 5 → 15 → 30
    assert gaps[:3] == (
        [1800.0, 1800.0, 1800.0] if first_open == 1800.0 else [300.0, 900.0, 1800.0]
    )
    notices = h.tr.drain_notices()
    assert len(notices) == 1  # once per provider per run
    n = notices[0]
    assert isinstance(n, Notice)
    assert n.key == f"{NOTICE_PROVIDER}{L_DEFAULT}"
    assert L_DEFAULT in n.title and text in n.message
    for blob in (repr(n), caplog.text):
        assert KEY not in blob
    assert h.tr.drain_notices() == []


def test_breaker_escalates_5_15_30_and_closes_on_probe_ok(harness_factory):
    state = {"probes": 0}

    def resp(req: dict, w: FakeWorker) -> dict:
        if _is_probe(req):
            state["probes"] += 1
        return down(req, w) if state["probes"] <= 4 else good(req, w)

    sp = Spawner(resp)
    h = harness_factory(sp)
    r = h.run(_cues(2))
    assert r.outcome == "translated"  # 300 + 900 + 1800 + 1800 s < 2 h deferred
    assert _deltas(_probe_times(sp)) == [300.0, 900.0, 1800.0, 1800.0]
    assert max(h.clock.waits) <= WAIT_SLICE_S


def test_breaker_level_resets_after_a_probe_ok(harness_factory):
    state = {"probes": 0, "down": True}

    def resp(req: dict, w: FakeWorker) -> dict:
        if _is_probe(req):
            state["probes"] += 1
            if state["probes"] in (1, 2, 4):
                return down(req, w)
            state["down"] = False
            return good(req, w)
        return down(req, w) if state["down"] else good(req, w)

    sp = Spawner(resp)
    h = harness_factory(sp)
    assert h.run(_cues(2), "a").outcome == "translated"
    state["down"] = True
    assert h.run(_cues(2, "b"), "b").outcome == "translated"
    gaps = _deltas(_probe_times(sp))
    assert gaps[0] == 300.0 and gaps[1] == 900.0
    assert gaps[3] == 300.0  # level 1 again, not 30 min


# ── AC7: failover on/off (H7) ────────────────────────────────────────────
@pytest.mark.parametrize("failover", [True, False])
def test_failover_switch(harness_factory, failover):
    state = {"probes": 0}

    def grok(req: dict, w: FakeWorker) -> dict:
        if _is_probe(req):
            state["probes"] += 1
            return down(req, w) if state["probes"] == 1 else good(req, w)
        return down(req, w) if state["probes"] < 2 else good(req, w)

    sp = Spawner(by_model(grok=grok, ds=good_as("DS")))
    h = harness_factory(sp, chain=[GROK, DS], failover=failover)
    r = h.run(_cues(3))
    assert r.outcome == "translated"
    if failover:
        assert [c.text for c in r.zh_cues] == ["DSja0", "DSja1", "DSja2"]
        assert r.fallback == 3
    else:
        assert _of(sp, "ds") == []  # waits for chain[0]: its cool-down + a probe
        assert [c.text for c in r.zh_cues] == ["中ja0", "中ja1", "中ja2"]
        assert r.fallback == 0
        assert _deltas(_probe_times(sp)) == [300.0]


def test_failover_off_still_sends_a_refused_cue_onwards(harness_factory):
    sp = Spawner(by_model(grok=fail_when({"ja1"}, forbid), ds=good_as("DS")))
    h = harness_factory(sp, chain=[GROK, DS], failover=False)
    r = h.run(_cues(3))
    assert [c.text for c in r.zh_cues] == ["中ja0", "DSja1", "中ja2"]
    assert r.fallback == 1


def test_h7_failover_off_with_an_unusable_primary_uses_chain0(harness_factory):
    unusable = LLMSettings("https://api.x.ai/v1", "grok", "", "none")
    sp = Spawner(by_model(ds=good_as("DS")))
    h = harness_factory(sp, chain=[unusable, DS], failover=False)
    r = h.run(_cues(2))
    assert [c.text for c in r.zh_cues] == ["DSja0", "DSja1"]
    assert r.fallback == 0 and [c["env"][ENV_KEY] for c in sp.calls] == [KEY_DS]


# ── G1: bounded — a line failing everywhere ends kept-Japanese ───────────
def test_g1_a_cue_failing_transiently_everywhere_ends_kept_japanese_bounded(
    harness_factory, tmp_path
):
    resp = fail_when({"ja1"}, timeout)
    sp = Spawner(by_model(grok=resp, ds=resp))
    h = harness_factory(sp, chain=[GROK, DS], checkpoint_dir=tmp_path)
    r = h.run(_cues(2))
    assert r.outcome == "translated"
    assert [c.text for c in r.zh_cues] == ["中ja0", "ja1"] and r.kept_ja == 1
    n = 2
    bound = 4 + 1 + (2 * n - 1) + n + n + 1  # AC6 termination
    for model in ("grok", "ds"):
        assert len(_of(sp, model)) <= bound
    # grok: schedule, probe, bisect, H5 leaf retry, I1 probe, confirming probe
    assert _of(sp, "grok") == ["1-2"] * 4 + ["P", "1", "2", "2", "P", "P"]
    assert _of(sp, "ds") == ["2"] * 4 + ["P", "P"]
    # H5: `failed` stays in memory — the checkpoint records no refusal
    saved = CheckpointStore(tmp_path).load(r.checkpoint_key, 2)
    assert saved is not None and saved[1] == SavedCue()


def test_h5_transient_leaf_one_retry_and_a_resume_asks_again(harness_factory, tmp_path):
    sp = Spawner(fail_when({"ja1"}, timeout))
    h = harness_factory(sp, checkpoint_dir=tmp_path)
    cues = _cues(2)
    r = h.run(cues)
    assert r.outcome == "translated" and r.kept_ja == 1
    seq = [_seq(q) for q in sp.requests]
    assert seq == ["1-2"] * 4 + ["P", "1", "2", "2", "P", "P"]
    leaf = [t for q, t in zip(sp.requests, sp.times) if _seq(q) == "2"]
    assert _deltas(leaf) == [LEAF_RETRY_S]
    saved = CheckpointStore(tmp_path).load(r.checkpoint_key, 2)
    assert saved is not None and saved[1] == SavedCue()  # not persisted
    sp2 = Spawner(good)
    h2 = harness_factory(sp2, checkpoint_dir=tmp_path)
    r2 = h2.run(cues)
    assert [_seq(q) for q in sp2.requests] == ["2"]
    assert (r2.resumed, r2.kept_ja) == (1, 0)
    assert [c.text for c in r2.zh_cues] == ["中ja0", "中ja1"]


# ── I1: an outage beginning mid-bisection ───────────────────────────────
def test_i1_outage_mid_bisection_opens_within_log2n_nodes_and_fails_over(
    harness_factory, tmp_path
):
    state = {"n": 0}

    def grok(req: dict, w: FakeWorker) -> dict:
        state["n"] += 1
        if state["n"] == 1:
            return forbid(req, w)  # a content refusal …
        if state["n"] == 2:
            return good(req, w)  # … its probe passes …
        return timeout(req, w)  # … and then grok hangs

    sp = Spawner(by_model(grok=grok, ds=good_as("DS")))
    h = harness_factory(sp, chain=[GROK, DS], checkpoint_dir=tmp_path)
    h.tr.submit(TranslateRequest(RUN, "a", _cues(40, "a")))
    h.tr.submit(TranslateRequest(RUN, "b", _cues(40, "b")))
    ra, rb = h.result(), h.result()
    assert (ra.job_id, rb.job_id) == ("a", "b")
    assert [c.text for c in ra.zh_cues] == [f"DSa{i}" for i in range(40)]
    assert [c.text for c in rb.zh_cues] == [f"DSb{i}" for i in range(40)]
    grok_seq = _of(sp, "grok")
    assert grok_seq == ["1-40", "P", "1-20", "1-10", "1-5", "1-2", "1", "1", "P"]
    after_outage = len(grok_seq) - 2
    assert after_outage <= math.ceil(math.log2(40)) + 3
    saved = CheckpointStore(tmp_path).load(ra.checkpoint_key, 40)
    assert saved is not None and all(s.refused_by == () for s in saved)
    (n,) = h.tr.drain_notices()
    assert "unreachable" in n.message


# ── G2 / AC8: defer, never block; pause after 2 h ────────────────────────
@pytest.mark.parametrize("recover", [True, False])
def test_g2_a_film_needing_a_down_fallback_defers_and_the_next_goes_first(
    harness_factory, recover
):
    during_b: list[tuple[int, bool]] = []
    holder: dict[str, Harness] = {}

    def grok(req: dict, w: FakeWorker) -> dict:
        if "b0" in _jas(req):
            tr = holder["h"].tr
            during_b.append((tr.queued(), tr.in_flight()))
        return fail_when({"a3"}, forbid)(req, w)

    def ds(req: dict, w: FakeWorker) -> dict:
        if recover and holder["h"].clock() >= 1400.0:
            return good_as("DS")(req, w)
        return down(req, w)

    sp = Spawner(by_model(grok=grok, ds=ds, mimo=down))
    h = harness_factory(sp, chain=[GROK, DS, MIMO])
    holder["h"] = h
    h.tr.submit(TranslateRequest(RUN, "a", _cues(8, "a")))
    h.tr.submit(TranslateRequest(RUN, "b", _cues(4, "b")))
    rb = h.result(timeout=30)
    assert (rb.job_id, rb.outcome) == ("b", "translated")  # B never waited on A
    assert [c.text for c in rb.zh_cues] == [f"中b{i}" for i in range(4)]
    assert during_b == [(1, True)]  # H3: A (deferred) counts as queued
    ra = h.result(timeout=30)
    assert ra.job_id == "a"
    if recover:
        assert ra.outcome == "translated"
        assert ra.zh_cues[3].text == "DSa3" and ra.fallback == 1
    else:
        assert (ra.outcome, ra.detail) == ("paused", TRANSLATION_PAUSED)
    kinds = sorted(n.key for n in h.tr.drain_notices())
    assert kinds == [f"{NOTICE_PROVIDER}{L_DS}", f"{NOTICE_PROVIDER}{L_MIMO}"]


def test_all_providers_down_every_film_deferred_then_paused_at_2h(
    harness_factory, tmp_path
):
    sp = Spawner(down)
    h = harness_factory(sp, checkpoint_dir=tmp_path)
    h.tr.submit(TranslateRequest(RUN, "a", _cues(2, "a")))
    h.tr.submit(TranslateRequest(RUN, "b", _cues(2, "b")))
    ra, rb = h.result(), h.result()
    assert [(r.job_id, r.outcome) for r in (ra, rb)] == [
        ("a", "paused"),
        ("b", "paused"),
    ]
    # b never sent a batch: grok was open when b was dequeued
    assert not [q for q in sp.requests if "b0" in _jas(q)]
    assert h.clock() - 1000.0 >= PAUSE_AFTER_S
    deadline = time.monotonic() + 5
    while h.tr.in_flight():  # the result is out before the film is dropped
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert h.tr.queued() == 0


def test_h6_deferral_accumulates_across_intervals(harness_factory):
    state = {"probes": 0, "up": False}

    def resp(req: dict, w: FakeWorker) -> dict:
        if _is_probe(req):
            state["probes"] += 1
            if state["probes"] == 3:
                state["up"] = True
                return good(req, w)
            return down(req, w)
        if state["up"] and "ja0" in _jas(req):  # batch 1 only, then down again
            state["up"] = False
            return good(req, w)
        return down(req, w)

    sp = Spawner(resp)
    h = harness_factory(sp)
    r = h.run(_cues(41))
    assert r.outcome == "paused"
    t_pause = h.clock()
    probes = _probe_times(sp)
    t_defer1, t_resume, t_defer2 = probes[0], probes[2], probes[3]
    first = t_resume - t_defer1
    second = t_pause - t_defer2
    assert first == 1200.0  # 300 + 900
    assert first + second == pytest.approx(PAUSE_AFTER_S, abs=0.01)
    assert second < PAUSE_AFTER_S  # the sum, not a fresh 2 h


def test_h3_only_deferred_films_in_flight_queued_and_progress(harness_factory):
    sp = Spawner(down)
    h = harness_factory(sp)
    snap: dict[str, Any] = {}

    def hook(seconds: float) -> None:
        tr = h.tr
        if not snap and len(tr._deferred) == 3 and tr._working is None:
            snap.update(q=tr.queued(), f=tr.in_flight(), p=tr.progress())

    h.clock.hook = hook
    for name in ("a", "b", "c"):
        h.tr.submit(TranslateRequest(RUN, name, _cues(4, name)))
    results = [h.result() for _ in range(3)]
    assert [r.outcome for r in results] == ["paused"] * 3
    assert snap["q"] == 2 and snap["f"] is True  # queued() + in_flight() == 3
    p = snap["p"]
    assert set(p) == PROGRESS_KEYS
    assert (p["job_id"], p["paused"], p["deferred"]) == ("a", True, 3)
    assert p["eta_s"] is None  # C-F3: no ETA while paused
    nums = step_numbers(p)
    assert "eta_s" not in nums and nums["paused"] is True and nums["deferred"] == 3


# ── H1: the empty chain / a removed provider ────────────────────────────
def test_chain_emptied_mid_film_gives_no_key_and_keeps_the_checkpoint(
    harness_factory, tmp_path
):
    sp = Spawner(good)
    h = harness_factory(sp, checkpoint_dir=tmp_path)

    def resp(req: dict, w: FakeWorker) -> dict:
        out = good(req, w)
        h.chain = []  # a Save left no usable provider
        return out

    sp.responder = resp
    r = h.run(_cues(90))
    assert (r.outcome, r.detail, r.zh_cues) == ("no_key", NO_LLM_KEY, ())
    assert len(sp.requests) == 1
    saved = CheckpointStore(tmp_path).load(r.checkpoint_key, 90)
    assert saved is not None and sum(1 for s in saved if s.zh) == 40


def test_chain_emptied_while_films_are_deferred_gives_no_key(harness_factory):
    sp = Spawner(down)
    h = harness_factory(sp)

    def hook(seconds: float) -> None:
        if len(h.tr._deferred) == 2 and h.tr._working is None:
            h.chain = []

    h.clock.hook = hook
    h.tr.submit(TranslateRequest(RUN, "a", _cues(2, "a")))
    h.tr.submit(TranslateRequest(RUN, "b", _cues(2, "b")))
    got = sorted((r.job_id, r.outcome, r.detail) for r in (h.result(), h.result()))
    assert got == [("a", "no_key", NO_LLM_KEY), ("b", "no_key", NO_LLM_KEY)]


def test_a_provider_removed_mid_film_defers_never_kept_japanese(harness_factory):
    sp = Spawner(
        by_model(
            grok=fail_when({"ja1"}, forbid), ds=fail_when({"ja1"}, forbid), mimo=down
        )
    )
    h = harness_factory(sp, chain=[GROK, DS, MIMO])

    def hook(seconds: float) -> None:
        if h.tr._deferred and h.tr._working is None:
            h.chain = [GROK, DS]  # MiMo removed while the film waits for it

    h.clock.hook = hook
    r = h.run(_cues(3))
    assert r.outcome == "paused"  # never "translated" with ja1 kept by removal


# ── H2: Settings changes (fingerprints) ─────────────────────────────────
def test_settings_change_wakes_a_deferred_wait_within_60s_and_retires(
    harness_factory,
):
    def resp(req: dict, w: FakeWorker) -> dict:
        if w.env is not None and w.env.get(ENV_KEY) == KEY:
            return _err("auth", req, status=401)
        return good(req, w)

    sp = Spawner(resp)
    h = harness_factory(sp, chain=[GROK])
    changed: dict[str, float] = {}
    idle = {"n": 0}

    def hook(seconds: float) -> None:
        if h.tr._working is None and h.tr._deferred and "t" not in changed:
            idle["n"] += 1
            if idle["n"] == 3:
                changed["t"] = h.clock()
                h.chain = [replace(GROK, api_key=KEY2)]  # the owner fixes the key

    h.clock.hook = hook
    r = h.run(_cues(2))
    assert r.outcome == "translated"
    old, new = sp.workers[0], sp.workers[-1]
    assert old.env[ENV_KEY] == KEY and new.env[ENV_KEY] == KEY2
    assert f"{old.pid}:close_stdin" in sp.log  # the departed fingerprint's worker
    first_new = next(t for q, t in zip(sp.requests, sp.times) if q in new.requests)
    assert first_new - changed["t"] <= WAIT_SLICE_S
    assert max(h.clock.waits) <= WAIT_SLICE_S


def test_h2_a_provider_retired_mid_attempt_ends_it_without_respawn(harness_factory):
    sp = Spawner(by_model(grok=down, ds=good_as("DS")))
    h = harness_factory(sp, chain=[GROK, DS])

    def hook(seconds: float) -> None:
        if h.chain[0] is GROK:
            h.chain = [DS]  # the primary removed during its schedule wait

    h.clock.hook = hook
    r = h.run(_cues(3))
    assert [c.text for c in r.zh_cues] == ["DSja0", "DSja1", "DSja2"]
    assert len(_of(sp, "grok")) == 1
    assert [c["env"][ENV_KEY] for c in sp.calls] == [KEY, KEY_DS]  # no respawn
    old = sp.workers[0]
    assert f"{old.pid}:close_stdin" in sp.log and old.dead.is_set()
    assert h.tr.drain_notices() == []


def test_duplicate_labels_in_the_chain_first_wins(harness_factory):
    dup = replace(GROK, api_key=KEY2)
    sp = Spawner(
        by_model(grok=lambda req, w: _err("auth", req, status=401), ds=good_as("DS"))
    )
    h = harness_factory(sp, chain=[GROK, dup, DS])
    r = h.run(_cues(2))
    assert [c.text for c in r.zh_cues] == ["DSja0", "DSja1"]
    assert [c["env"][ENV_KEY] for c in sp.calls] == [KEY, KEY_DS]


# ── G10 blank cues; workers per provider (C7); cancel (C6) ───────────────
def test_blank_cues_are_done_at_load_and_never_sent(harness_factory):
    cues = (
        Cue(1, 0, 1, ""),
        Cue(2, 1, 2, "ja1"),
        Cue(3, 2, 3, "  \n "),
        Cue(4, 3, 4, "ja3"),
    )
    sp = Spawner(good)
    h = harness_factory(sp)
    r = h.run(cues)
    assert [c.text for c in r.zh_cues] == ["", "中ja1", "  \n ", "中ja3"]
    assert [_ids(q) for q in sp.requests] == [["2", "4"]]
    assert (r.resumed, r.kept_ja) == (0, 0)


def test_an_all_blank_film_needs_no_request(harness_factory):
    sp = Spawner(good)
    h = harness_factory(sp)
    r = h.run((Cue(1, 0, 1, " "),))
    assert r.outcome == "translated" and [c.text for c in r.zh_cues] == [" "]
    assert sp.calls == []


def test_each_worker_gets_only_its_own_provider_env(harness_factory, monkeypatch):
    monkeypatch.setenv("TASKPAW_LLM_API_KEY", "sk-KEYMARKER-env-primary")
    monkeypatch.setenv("TASKPAW_LLM_FALLBACK1_API_KEY", "sk-KEYMARKER-env-fb1")
    monkeypatch.setenv("TASKPAW_LLM_MODEL", "stale")
    sp = Spawner(by_model(grok=fail_when({"ja1"}, forbid), ds=good_as("DS")))
    h = harness_factory(sp, chain=[GROK, DS])
    assert h.run(_cues(2)).outcome == "translated"
    envs = [c["env"] for c in sp.calls]
    assert len(envs) == 2
    for env, s in zip(envs, (GROK, DS)):
        llm = {k: v for k, v in env.items() if k.upper().startswith("TASKPAW_LLM_")}
        assert llm == {ENV_BASE: s.api_base, ENV_MODEL: s.model, ENV_KEY: s.api_key}


def test_cancel_with_three_workers_shares_one_wait_within_budget(harness_factory):
    held = threading.Event()

    def mimo(req: dict, w: FakeWorker) -> None:
        held.set()  # never answers
        return None

    sp = Spawner(
        by_model(
            grok=fail_when({"ja0"}, forbid), ds=fail_when({"ja0"}, forbid), mimo=mimo
        ),
        ignore_close=True,  # every worker survives its stdin EOF
    )
    h = harness_factory(sp, chain=[GROK, DS, MIMO], deadline_s=30)
    h.tr.submit(TranslateRequest(RUN, "a", _cues(1)))
    assert held.wait(5)
    assert len(sp.workers) == 3
    t0 = time.monotonic()
    h.tr.cancel()
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0, elapsed  # one shared ≤ 1 s wait, not 3 × 1 s
    h.tr.join(2.0)
    assert not h.tr.is_alive()
    events = [e.split(":", 1)[1] for e in sp.log]
    closes = [i for i, ev in enumerate(events) if ev == "close_stdin"]
    kills = [i for i, ev in enumerate(events) if ev == "terminate_tree"]
    assert len(closes) == 3 and len(kills) == 3 and max(closes) < min(kills)
    assert all(w.dead.is_set() for w in sp.workers)


# ── AC3/AC4: checkpoint + resume ─────────────────────────────────────────
def test_stop_mid_film_then_a_new_translator_resumes_only_open_cues(
    harness_factory, tmp_path
):
    responder, held = _stepper()
    h1 = harness_factory(Spawner(responder), checkpoint_dir=tmp_path)
    cues = _cues(90)
    h1.tr.submit(TranslateRequest(RUN, "film.mp4", cues))
    _reply(held.get(timeout=5))  # batch 1 answered → checkpointed
    second = held.get(timeout=5)  # batch 2 in flight when Stop lands
    assert _ids(second[0])[0] == "41"
    h1.close()
    sp2 = Spawner(good)
    h2 = harness_factory(sp2, checkpoint_dir=tmp_path)
    r = h2.run(cues, "film.mp4")
    assert r.outcome == "translated" and r.resumed == 40
    assert [_seq(q) for q in sp2.requests] == ["41-80", "81-90"]
    assert [c.text for c in r.zh_cues] == [f"中{c.text}" for c in cues]
    assert _user(sp2.requests[0])["context"] == [f"ja{i}" for i in range(35, 40)]


def test_resume_never_asks_a_provider_that_refused_the_cue(harness_factory, tmp_path):
    cues = _cues(3)
    store = CheckpointStore(tmp_path)
    key = store.key(cues)
    store.save(key, "f", [SavedCue(), SavedCue(refused_by=(L_GROK,)), SavedCue()])
    sp = Spawner(by_model(grok=good, ds=good_as("DS")))
    h = harness_factory(sp, chain=[GROK, DS], checkpoint_dir=tmp_path)
    r = h.run(cues)
    assert [c.text for c in r.zh_cues] == ["中ja0", "DSja1", "中ja2"]
    assert _of(sp, "grok") == ["1", "3"] and _of(sp, "ds") == ["2"]


def test_crash_between_a_request_and_its_write_repeats_at_most_that_batch(
    harness_factory, tmp_path
):
    sp1 = Spawner(good)
    h1 = harness_factory(sp1, checkpoint_dir=tmp_path)
    store = h1.tr._store
    real_save = store.save
    calls: list[int] = []

    def crashing_save(key: str, film: str, states: Any) -> bool:
        calls.append(1)
        if len(calls) == 2:  # the process dies before batch 2 is written
            h1.tr.cancel()
            return True
        return real_save(key, film, states)

    store.save = crashing_save  # type: ignore[method-assign]
    cues = _cues(90)
    h1.tr.submit(TranslateRequest(RUN, "film.mp4", cues))
    h1.tr.join(5)
    assert not h1.tr.is_alive() and len(calls) == 2
    sp2 = Spawner(good)
    h2 = harness_factory(sp2, checkpoint_dir=tmp_path)
    r = h2.run(cues, "film.mp4")
    assert r.resumed == 40
    assert [_seq(q) for q in sp2.requests] == ["41-80", "81-90"]  # one batch repeated


def test_checkpoint_write_failure_continues_with_one_notice(harness_factory, tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    h = harness_factory(Spawner(good), checkpoint_dir=blocker / "ckpt")
    r = h.run(_cues(90))
    assert r.outcome == "translated"
    notices = h.tr.drain_notices()
    assert [n.key for n in notices] == [NOTICE_CHECKPOINT]
    assert "checkpoint" in notices[0].title
    assert h.run(_cues(3, "b"), "b").outcome == "translated"
    assert h.tr.drain_notices() == []  # once per run


def test_checkpoint_holds_labels_only(harness_factory, tmp_path):
    base = f"https://u:{KEY}@api.x.ai:443/v1"
    s = LLMSettings(base, "grok", KEY, "config")
    h = harness_factory(
        Spawner(fail_when({"ja1"}, forbid)), chain=[s], checkpoint_dir=tmp_path
    )
    r = h.run(_cues(3))
    text = (tmp_path / f"{r.checkpoint_key}.json").read_text(encoding="utf-8")
    assert KEY not in text and "u:" not in text and "443" not in text
    assert L_GROK in text


def test_discard_checkpoint_removes_the_file_and_never_raises(
    harness_factory, tmp_path
):
    h = harness_factory(Spawner(good), checkpoint_dir=tmp_path)
    r = h.run(_cues(3))
    path = tmp_path / f"{r.checkpoint_key}.json"
    assert path.is_file()
    h.tr.discard_checkpoint(r.checkpoint_key)
    assert not path.exists()
    h.tr.discard_checkpoint(r.checkpoint_key)  # gone already
    h.tr.discard_checkpoint("../../etc/passwd")  # never a path


def test_old_checkpoints_are_pruned_at_translator_start(harness_factory, tmp_path):
    old = tmp_path / f"{'a' * 64}.json"
    old.write_text("{}", encoding="utf-8")
    ancient = time.time() - CHECKPOINT_MAX_AGE_S - 10
    os.utime(old, (ancient, ancient))
    fresh = tmp_path / f"{'b' * 64}.json"
    fresh.write_text("{}", encoding="utf-8")
    h = harness_factory(Spawner(good), checkpoint_dir=tmp_path)
    assert h.run(_cues(1)).outcome == "translated"  # pruned before its first film
    assert not old.exists() and fresh.exists()


def test_no_checkpoint_dir_is_memory_only(harness_factory, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    h = harness_factory(Spawner(good))
    assert h.run(_cues(3)).outcome == "translated"
    assert list(tmp_path.iterdir()) == []


def test_interior_blank_lines_in_a_reply_are_collapsed(harness_factory):
    # F1: "甲\n\n乙" must never reach a cue verbatim — a blank line inside a
    # cue would corrupt the published .srt.
    def blank_inside(req: dict, w: FakeWorker) -> dict:
        ids = [c["id"] for c in _user(req)["cues"]]
        return _ok(json.dumps({i: " 甲\n\n  \n乙 " for i in ids}), req)

    sp = Spawner(blank_inside)
    h = harness_factory(sp)
    r = h.run(_cues(3))
    assert r.outcome == "translated"
    assert [c.text for c in r.zh_cues] == ["甲\n乙"] * 3
    assert len(sp.workers[0].requests) == 1  # accepted as is: no retry needed
    text = srt.serialize(r.zh_cues)
    again = srt.parse(text)
    assert len(again) == 3 and [c.text for c in again] == ["甲\n乙"] * 3


def test_blank_only_value_is_a_retryable_content_failure(harness_factory):
    sp = Spawner(scripted(_blank_only))
    h = harness_factory(sp)
    r = h.run(_cues(4))
    assert r.outcome == "translated"
    reqs = sp.workers[0].requests
    assert len(reqs) == 3  # the failed batch was split into two halves
    assert [c["id"] for c in _user(reqs[1])["cues"]] == ["1", "2"]


# ── #189/#192: Translator.progress() — counters, percent/ETA, model label ─
PROGRESS_KEYS = {
    "job_id",
    "model",
    "batches_done",
    "batches_total",
    "cues_done",
    "cues_total",
    "started_at",
    "elapsed_s",
    "percent",
    "eta_s",
    # #192 AC12
    "cues_resumed",
    "cues_fallback",
    "cues_kept_ja",
    "paused",
    "deferred",
}


def _stepper() -> tuple[Responder, "queue.Queue[tuple[dict, FakeWorker]]"]:
    """A responder that holds every request until the test replies to it."""
    held: "queue.Queue[tuple[dict, FakeWorker]]" = queue.Queue()

    def responder(req: dict, w: FakeWorker) -> None:
        held.put((req, w))
        return None

    return responder, held


def _reply(item: tuple[dict, FakeWorker], fn: Responder = good) -> None:
    req, w = item
    w.sink.put(json.dumps(fn(req, w)))


def _counts(tr: Translator) -> tuple[int, int, int]:
    p = tr.progress()
    assert p is not None
    return p["batches_done"], p["cues_done"], p["percent"]


def test_progress_counts_batches_and_a_split_batch_once(harness_factory):
    responder, held = _stepper()
    h = harness_factory(Spawner(responder))
    tr = h.tr
    assert tr.progress() is None  # idle
    tr.submit(TranslateRequest(RUN, "a.mp4", _cues(90)))
    item = held.get(timeout=5)
    p = tr.progress()
    assert p is not None and set(p) == PROGRESS_KEYS
    assert (p["job_id"], p["model"]) == ("a.mp4", "m/x · llm.example")
    assert (p["batches_total"], p["cues_total"]) == (3, 90)
    assert (p["paused"], p["deferred"], p["cues_resumed"]) == (False, 0, 0)
    assert _counts(tr) == (0, 0, 0)
    _reply(item)
    item = held.get(timeout=5)
    assert _counts(tr) == (1, 40, 44)
    _reply(item, _not_json)  # the 2nd batch is invalid → bisected at once
    item = held.get(timeout=5)
    assert len(_user(item[0])["cues"]) == 20
    assert _counts(tr) == (1, 40, 44)  # a split batch counts once …
    _reply(item)
    item = held.get(timeout=5)
    # #192: each answered request is done (and checkpointed) at once
    assert _counts(tr) == (1, 60, 66)  # … the batch only when both halves finished
    _reply(item)
    item = held.get(timeout=5)
    assert _counts(tr) == (2, 80, 88)
    assert tr.progress()["batches_total"] == 3
    _reply(item)
    assert h.result().outcome == "translated"
    assert tr.progress() is None  # cleared when the result is out


def test_progress_elapsed_percent_and_eta(harness_factory):
    responder, held = _stepper()
    h = harness_factory(Spawner(responder))
    h.tr.submit(TranslateRequest(RUN, "a.mp4", _cues(90)))
    first = held.get(timeout=5)
    t0 = h.tr.progress()["started_at"]
    assert h.tr.progress(now=t0 + 30)["eta_s"] is None  # no cue done yet
    _reply(first)
    held.get(timeout=5)  # the 2nd batch is in flight: 40/90 done
    early = h.tr.progress(now=t0 + 5.5)
    assert (early["elapsed_s"], early["eta_s"]) == (5, None)  # < 10 s
    late = h.tr.progress(now=t0 + 30.5)
    # floor(100 × 40/90); ceil(30.5/40 × (90 − 40))
    assert (late["elapsed_s"], late["percent"], late["eta_s"]) == (30, 44, 39)
    late["cues_done"] = 999
    assert h.tr.progress(now=t0 + 30)["cues_done"] == 40  # a fresh dict


def test_progress_none_after_cancel_mid_batch(harness_factory):
    responder, held = _stepper()
    h = harness_factory(Spawner(responder))
    tr = h.tr
    tr.submit(TranslateRequest(RUN, "a.mp4", _cues(90)))
    _reply(held.get(timeout=5))
    held.get(timeout=5)
    assert _counts(tr) == (1, 40, 44)
    tr.cancel()
    assert tr.progress() is None  # D5: at once, not after the thread ends
    tr.join(2.0)
    assert not tr.is_alive() and tr.progress() is None


def test_progress_none_for_a_request_that_fails_before_any_batch(harness_factory):
    sp = Spawner(good)
    h = harness_factory(sp, settings=_settings(key=""))
    assert h.run(_cues(2)).detail == "no LLM key"
    assert h.tr.progress() is None


def test_progress_reports_resumed_fallback_kept_and_the_model_in_use(
    harness_factory, tmp_path
):
    cues = _cues(45)
    store = CheckpointStore(tmp_path)
    key = store.key(cues)
    states = [SavedCue(zh=f"旧{i}", by=L_GROK) for i in range(10)]
    states += [SavedCue() for _ in range(10, 45)]
    states[12] = SavedCue(refused_by=(L_GROK,))  # goes to the fallback
    states[20] = SavedCue(refused_by=(L_GROK, L_DS))  # kept in Japanese
    assert store.save(key, "film", states)
    responder, held = _stepper()
    h = harness_factory(Spawner(responder), chain=[GROK, DS], checkpoint_dir=tmp_path)
    h.tr.submit(TranslateRequest(RUN, "film", cues))
    item = held.get(timeout=5)
    assert _seq(item[0]) == "11-12" and item[0]["model"] == "grok"
    p = h.tr.progress()
    assert p is not None
    assert (p["cues_resumed"], p["cues_kept_ja"], p["cues_fallback"]) == (10, 1, 0)
    assert (p["cues_done"], p["cues_total"], p["model"]) == (11, 45, L_GROK)
    _reply(item)
    item = held.get(timeout=5)
    assert _seq(item[0]) == "13" and item[0]["model"] == "ds"
    assert h.tr.progress()["model"] == L_DS  # the provider in use right now
    _reply(item)
    item = held.get(timeout=5)
    assert _seq(item[0]) == "14-20,22-45"
    p = h.tr.progress()
    assert (p["cues_fallback"], p["cues_done"], p["model"]) == (1, 14, L_GROK)
    _reply(item)
    r = h.result()
    assert (r.outcome, r.resumed, r.fallback, r.kept_ja) == ("translated", 10, 1, 1)
    assert r.zh_cues[0].text == "旧0" and r.zh_cues[20].text == "ja20"


def test_progress_model_label_never_carries_the_key_userinfo_or_port(
    harness_factory,
):
    responder, held = _stepper()
    base = f"https://u:{KEY}@api.x.ai:443/v1"
    h = harness_factory(Spawner(responder), settings=_settings(base=base))
    h.tr.submit(TranslateRequest(RUN, "a.mp4", _cues(3)))
    held.get(timeout=5)
    p = h.tr.progress()
    assert p is not None and p["model"] == "m/x · api.x.ai"
    text = json.dumps(p, ensure_ascii=False)
    assert KEY not in text and "u:" not in text and "443" not in text


@pytest.mark.parametrize(
    "model,base,label",
    [
        ("grok-4.3", "https://api.x.ai/v1", "grok-4.3 · api.x.ai"),
        ("grok-4.3", "https://u:k@api.x.ai:443/v1", "grok-4.3 · api.x.ai"),
        ("grok-4.3", "api.x.ai/v1", "grok-4.3"),  # no scheme: no host
        ("grok-4.3", "http://[::1", "grok-4.3"),  # malformed: urlsplit raises
        ("grok-4.3", "http://[::1]:8080/v1", "grok-4.3 · ::1"),
        ("grok-4.3", "", "grok-4.3"),
        ("grok-4.3", None, "grok-4.3"),
        ("", "https://API.X.AI/v1", "api.x.ai"),
        (None, None, ""),
    ],
)
def test_model_label(model, base, label):
    got = model_label(model, base)
    assert got == label
    assert "u:" not in got and "k@" not in got and ":443" not in got


def test_model_label_is_bounded_and_keeps_the_host():
    assert MODEL_LABEL_CHARS == 80
    got = model_label("m" * 200, "https://api.x.ai/v1")
    assert len(got) <= MODEL_LABEL_CHARS
    assert got.startswith("mmm") and got.endswith("api.x.ai")


# ── #192/#190 G5/H4: the provider probe (shared by Settings Test + engine) ──
def test_probe_messages_are_the_real_translation_prompt_with_one_cue():
    msgs = probe_messages()
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert msgs[0]["content"] == SYSTEM_PROMPT
    assert json.loads(msgs[1]["content"]) == {
        "cues": [{"id": "1", "ja": "こんにちは"}],
        "context": [],
    }
    assert "こんにちは" in msgs[1]["content"]  # sent as UTF-8, not \u-escaped
    assert probe_messages() is not msgs  # a fresh list each call


@pytest.mark.parametrize(
    "content,ok",
    [
        ('{"1": "你好"}', True),
        ('{"1": " 你好 "}', True),
        ('{"1": ""}', False),  # empty value
        ('{"1": "   "}', False),
        ('{"2": "你好"}', False),  # id mismatch
        ('{"1": "你好", "2": "x"}', False),
        ('{"1": "a", "1": "b"}', False),  # duplicate key
        ('{"1": 5}', False),
        ('["你好"]', False),
        ("你好", False),  # not JSON
        ("", False),
    ],
)
def test_probe_ok_is_the_translator_validation(content, ok):
    assert probe_ok(content) is ok


def test_the_engine_probe_is_the_settings_probe(harness_factory):
    sp = Spawner(scripted(forbid))
    h = harness_factory(sp)
    assert h.run(_cues(2)).outcome == "translated"
    probe = next(q for q in sp.requests if _is_probe(q))
    assert probe["messages"] == probe_messages()
    assert probe["max_tokens"] == 4096 and probe["json_mode"] is True


@pytest.mark.parametrize(
    "base, needs",
    [
        ("https://api.x.ai/v1", True),
        ("http://localhost:11434/v1", False),
        ("http://127.0.0.1:8080/v1", False),
        ("http://127.5.0.1/v1", False),
        ("http://[::1]:8080/v1", False),
        ("", True),
        ("http://[bad/v1", True),  # unparseable → needs a key
    ],
)
def test_needs_llm_key_only_loopback_is_keyless(base, needs):
    # S4: the one shared rule (the Jasna plugin imports it too).
    assert needs_llm_key(base) is needs
    from taskpaw_v3.monitors import subs

    assert subs.needs_llm_key is needs_llm_key


# ── integration: the REAL llm_worker against a loopback fake LLM ─────────
class _FakeLLM(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a: Any) -> None:
        pass

    def do_POST(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        srv: Any = self.server
        srv.seen.append((dict(self.headers), body))
        srv.arrived.set()
        self.close_connection = True
        if srv.mode == "ok":
            payload = json.loads(body)
            user = json.loads(payload["messages"][1]["content"])
            content = json.dumps(
                {c["id"]: f"中{c['ja']}" for c in user["cues"]}, ensure_ascii=False
            )
            reply = json.dumps(
                {
                    "model": "served",
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)
        elif srv.mode == "hang":
            srv.release.wait(30)
        elif srv.mode == "drip":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "100000")
            self.end_headers()
            try:
                while not srv.release.is_set():
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    srv.release.wait(0.2)
            except OSError:
                return  # the worker was killed — the point of the test


@pytest.fixture
def fake_llm(monkeypatch):
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.chdir(REPO_ROOT)  # `python -m taskpaw_v3.core.llm_worker`
    servers: list[ThreadingHTTPServer] = []

    def make(mode: str) -> ThreadingHTTPServer:
        srv: Any = ThreadingHTTPServer(("127.0.0.1", 0), _FakeLLM)
        srv.daemon_threads = True
        srv.seen = []
        srv.mode = mode
        srv.arrived = threading.Event()
        srv.release = threading.Event()
        threading.Thread(target=srv.serve_forever, name="fake-llm", daemon=True).start()
        servers.append(srv)
        return srv

    yield make
    for srv in servers:
        srv.release.set()  # type: ignore[attr-defined]
        srv.shutdown()
        srv.server_close()


def _real_translator(srv: ThreadingHTTPServer, **kw: Any) -> Translator:
    s = _settings(base=f"http://127.0.0.1:{srv.server_address[1]}/v1")
    tr = Translator(RUN, name="real", chain_fn=lambda: (s,), **kw)
    tr.start()
    return tr


def _no_child_threads() -> bool:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if not [t for t in threading.enumerate() if t.name.startswith("subs-")]:
            return True
        time.sleep(0.02)
    return False


def test_real_worker_end_to_end_translated(fake_llm, caplog, tmp_path):
    caplog.set_level(logging.DEBUG)
    srv = fake_llm("ok")
    tr = _real_translator(srv, checkpoint_dir=tmp_path)
    try:
        cues = _cues(45, prefix="せりふ")
        tr.submit(TranslateRequest(RUN, "film.mp4", cues))
        r = tr.results.get(timeout=60)
        assert isinstance(r, TranslateResult), r
        assert r.outcome == "translated", r.detail
        assert r.run == RUN
        assert [c.text for c in r.zh_cues] == [f"中{c.text}" for c in cues]
        assert [(c.index, c.start_ms, c.end_ms) for c in r.zh_cues] == [
            (c.index, c.start_ms, c.end_ms) for c in cues
        ]
        headers, _body = srv.seen[0]  # type: ignore[attr-defined]
        assert headers["Authorization"] == f"Bearer {KEY}"
        assert len(srv.seen) == 2  # type: ignore[attr-defined]
        assert (tmp_path / f"{r.checkpoint_key}.json").is_file()
    finally:
        tr.cancel()
        tr.join(2)
    assert not tr.is_alive()
    assert _no_child_threads()
    assert KEY not in caplog.text


@pytest.mark.parametrize("mode", ["hang", "drip"])
def test_real_worker_stop_while_request_hangs_or_drips(fake_llm, mode):
    srv = fake_llm(mode)
    tr = _real_translator(srv)
    try:
        tr.submit(TranslateRequest(RUN, "film.mp4", _cues(3)))
        assert srv.arrived.wait(30)  # type: ignore[attr-defined]
        time.sleep(0.3)  # the worker is inside the HTTP read now
        assert tr.in_flight()
        t0 = time.monotonic()
        tr.cancel()
        tr.join(2.0)
        elapsed = time.monotonic() - t0
        assert not tr.is_alive()
        assert elapsed < 2.0, elapsed
        assert tr.queued() == 0 and tr.in_flight() is False
    finally:
        tr.cancel()
        tr.join(2)
    assert _no_child_threads()


def test_the_default_wait_ends_at_once_on_cancel():
    # No wait_fn injected, the real clock: a 10 s schedule wait ends at once
    # on cancel() (the stop budget).
    sp = Spawner(down)
    tr = Translator(
        RUN,
        name="real-wait",
        spawn=sp,
        chain_fn=lambda: (_settings(),),
        worker_argv_fn=lambda: ["worker-argv"],
        job_fn=lambda proc: None,
    )
    tr.start()
    try:
        tr.submit(TranslateRequest(RUN, "film.mp4", _cues(2)))
        deadline = time.monotonic() + 5
        while not sp.requests:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        time.sleep(0.2)  # inside the 10 s schedule wait now
        assert len(sp.requests) == 1
        t0 = time.monotonic()
        tr.cancel()
        tr.join(2.0)
        assert not tr.is_alive() and time.monotonic() - t0 < 2.0
    finally:
        tr.cancel()
        tr.join(2)
