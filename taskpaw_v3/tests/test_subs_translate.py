"""`Translator`: the llm-worker client thread (#177, subs/translate.py).

Unit tests drive a fake worker injected through `spawn` that speaks the
llm-worker JSON-lines protocol; the integration tests run the REAL
`llm_worker` (real `worker_argv()`) against a loopback `http.server` on
127.0.0.1:0. No network, no default ports, no real key (the autouse conftest
fixture strips TASKPAW_LLM_*)."""

from __future__ import annotations

import itertools
import json
import logging
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

from taskpaw_v3.core.llm import LLMSettings
from taskpaw_v3.core.llm_worker import ENV_KEY
from taskpaw_v3.monitors.subs import srt
from taskpaw_v3.monitors.subs.child import Eof
from taskpaw_v3.monitors.subs.srt import Cue
from taskpaw_v3.monitors.subs.translate import (
    BATCH_SIZE,
    CANCELLED,
    CONTEXT_SIZE,
    REQUEST_TIMEOUT_S,
    RESPONSE_DEADLINE_S,
    SYSTEM_PROMPT,
    TranslateRequest,
    TranslateResult,
    Translator,
    needs_llm_key,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
KEY = "sk-KEYMARKER-subs-5d1e"
KEY2 = "sk-KEYMARKER-subs-other-77aa"
RUN = ("inst", 3)


def _cues(n: int, prefix: str = "ja") -> tuple[Cue, ...]:
    return tuple(Cue(i + 1, i * 1000, i * 1000 + 500, f"{prefix}{i}") for i in range(n))


def _settings(key: str = KEY, base: str = "https://llm.example/v1") -> LLMSettings:
    return LLMSettings(api_base=base, model="m/x", api_key=key, key_source="config")


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

    def _event(self, what: str) -> None:
        self.log.append(f"{self.pid}:{what}")

    def poll(self) -> Optional[int]:
        return self.proc.poll()

    def write_line(self, line: str) -> None:
        if self.write_error or self.dead.is_set() or self.stdin_closed:
            raise OSError(22, "Invalid argument")
        req = json.loads(line)
        self.requests.append(req)
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
        w = FakeWorker(argv, env, line_sink, self.responder, self.log, **wkw)
        self.workers.append(w)
        return w


def _user(req: dict) -> dict:
    return json.loads(req["messages"][1]["content"])


def _ok(content: str, req: dict) -> dict:
    return {
        "id": req["id"],
        "ok": True,
        "content": content,
        "finish_reason": "stop",
        "model": "served",
        "latency_ms": 3,
    }


def _err(kind: str, req: dict) -> dict:
    return {"id": req["id"], "ok": False, "kind": kind, "status": None, "message": "x"}


def good(req: dict, w: FakeWorker) -> dict:
    return _ok(json.dumps({c["id"]: f"中{c['ja']}" for c in _user(req)["cues"]}), req)


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
        spawner: Spawner,
        settings: Optional[LLMSettings] = None,
        deadline_s: float = 5.0,
        job_fn: Optional[Callable] = None,
    ) -> None:
        self.spawner = spawner
        self.settings = settings or _settings()
        self.keepers: list[Any] = []
        self.tr = Translator(
            RUN,
            name="t",
            spawn=spawner,
            settings_fn=lambda: self.settings,
            worker_argv_fn=lambda: ["worker-argv", "llm-worker"],
            job_fn=job_fn or (lambda proc: None),
            deadline_s=deadline_s,
        )
        self.tr.start()

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


def test_constants():
    assert (BATCH_SIZE, CONTEXT_SIZE) == (40, 5)
    assert RESPONSE_DEADLINE_S == 60.0 and REQUEST_TIMEOUT_S == 30.0
    assert "JSON" in SYSTEM_PROMPT or "json" in SYSTEM_PROMPT
    assert "○" in SYSTEM_PROMPT


def test_batch_shaping_and_success(harness_factory):
    sp = Spawner(good)
    h = harness_factory(sp)
    cues = _cues(90)
    r = h.run(cues)
    assert r.run == RUN and r.job_id == "a.mp4" and r.outcome == "translated"
    assert r.zh_cues == tuple(
        Cue(c.index, c.start_ms, c.end_ms, f"中{c.text}") for c in cues
    )
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
    assert len({req["id"] for req in reqs}) == 3
    assert reqs[0]["id"] == "a.mp4#0#0"
    assert KEY not in json.dumps(reqs)


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


@pytest.mark.parametrize("bad", CONTENT_FAILURES)
def test_content_failure_twice_fails_the_file(harness_factory, bad):
    sp = Spawner(scripted(bad, bad))
    h = harness_factory(sp)
    r = h.run(_cues(4))
    assert r.outcome == "failed" and r.zh_cues == ()
    assert len(sp.workers[0].requests) == 2  # the failed half ends the file


@pytest.mark.parametrize("kind", ["rate_limit", "network", "bad_response"])
def test_retryable_error_kinds(harness_factory, kind):
    e = lambda req, w: _err(kind, req)  # noqa: E731
    sp = Spawner(scripted(e))
    h = harness_factory(sp)
    assert h.run(_cues(4)).outcome == "translated"
    sp2 = Spawner(scripted(e, "good", e))
    h2 = harness_factory(sp2)
    r = h2.run(_cues(4))
    assert r.outcome == "failed" and kind in r.detail
    assert len(sp2.workers[0].requests) == 3


@pytest.mark.parametrize("kind", ["auth", "refusal"])
def test_non_retryable_kinds_fail_at_once(harness_factory, kind):
    sp = Spawner(lambda req, w: _err(kind, req))
    h = harness_factory(sp)
    r = h.run(_cues(90))
    assert r.outcome == "failed" and kind in r.detail
    assert len(sp.workers[0].requests) == 1


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


def test_eof_twice_is_network_failure(harness_factory):
    def crash(req: dict, w: FakeWorker) -> None:
        w.die()
        return None

    sp = Spawner(crash)
    h = harness_factory(sp)
    r = h.run(_cues(4))
    assert r.outcome == "failed" and "network" in r.detail


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


def test_mismatched_reply_only_then_timeout(harness_factory):
    def mismatch_only(req: dict, w: FakeWorker) -> dict:
        return {**good(req, w), "id": "stale#9#0"}

    sp = Spawner(mismatch_only)
    h = harness_factory(sp, deadline_s=0.2)
    t0 = time.monotonic()
    r = h.run(_cues(2))
    assert r.outcome == "failed" and "network" in r.detail
    assert time.monotonic() - t0 < 5


def test_spawn_raising_is_network_then_retry(harness_factory):
    sp = Spawner(good)
    sp.fail_times = 1
    h = harness_factory(sp)
    assert h.run(_cues(4)).outcome == "translated"


def test_spawn_always_raising_fails_without_leaking(harness_factory, caplog):
    caplog.set_level(logging.DEBUG)
    sp = Spawner(good)
    sp.fail_times = 99
    h = harness_factory(sp)
    r = h.run(_cues(4))
    assert r.outcome == "failed" and "network" in r.detail
    assert "spawn: OSError" in r.detail
    assert KEY not in r.detail and KEY not in caplog.text


def test_no_key_non_loopback_fails(harness_factory):
    sp = Spawner(good)
    h = harness_factory(sp, settings=_settings(key=""))
    r = h.run(_cues(2))
    assert r.outcome == "failed" and r.detail == "no LLM key"
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


def test_logs_carry_kind_and_latency_only(harness_factory, caplog):
    caplog.set_level(logging.DEBUG)
    sp = Spawner(scripted(lambda req, w: _err("rate_limit", req)))
    h = harness_factory(sp)
    cues = _cues(4, prefix="秘密のセリフ")
    assert h.run(cues).outcome == "translated"
    text = caplog.text
    assert "latency_ms" in text and "rate_limit" in text
    for marker in (KEY, "秘密のセリフ", "中秘密", "Bearer"):
        assert marker not in text


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

    h = harness_factory(blocking_spawn)  # type: ignore[arg-type]
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
    # cancel() arrives while _ensure_worker is tearing it down (outside the
    # lock) → cancel returns fast, the thread joins, and no new worker spawns.
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


def _real_translator(srv: ThreadingHTTPServer) -> Translator:
    s = _settings(base=f"http://127.0.0.1:{srv.server_address[1]}/v1")
    tr = Translator(RUN, name="real", settings_fn=lambda: s)
    tr.start()
    return tr


def _no_child_threads() -> bool:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if not [t for t in threading.enumerate() if t.name.startswith("subs-")]:
            return True
        time.sleep(0.02)
    return False


def test_real_worker_end_to_end_translated(fake_llm, caplog):
    caplog.set_level(logging.DEBUG)
    srv = fake_llm("ok")
    tr = _real_translator(srv)
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
