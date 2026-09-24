"""Global LLM settings + chat() + the terminable llm-worker sidecar (#178).

Hermetic (D7/D8): no network egress, no default ports — every HTTP server here is
a local `http.server` on 127.0.0.1:0, and the autouse `_llm_isolation` fixture
(conftest) resets the holder and strips TASKPAW_LLM_* from the environment.
"""

from __future__ import annotations

import http.client
import io
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from taskpaw_v3.core import llm, llm_worker
from taskpaw_v3.core.llm import (
    DEFAULT_LLM_API_BASE,
    DEFAULT_LLM_MODEL,
    LLM_KEY_ENV,
    ChatResult,
    LLMError,
    LLMSettings,
    chat,
    get_llm_settings,
    reset_llm_settings,
    resolve_llm_settings,
    set_llm_settings,
)
from taskpaw_v3.core.llm_worker import (
    ENV_BASE,
    ENV_KEY,
    ENV_MODEL,
    assign_kill_on_close_job,
    handle_request,
    serve,
    settings_from_env,
    worker_argv,
    worker_env,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

KEY_MARKER = "sk-KEYMARKER-9f3a"
PROMPT_MARKER = "PROMPTMARKER-77c1"
BODY_MARKER = "BODYMARKER-5e20"


def _settings(key: str = KEY_MARKER, base: str = "https://llm.example/v1"):
    return LLMSettings(api_base=base, model="m/x", api_key=key, key_source="config")


def _msgs():
    return [{"role": "user", "content": f"hello {PROMPT_MARKER}"}]


def _envelope(content="hi", finish_reason="stop", **extra) -> dict:
    choice: dict = {"message": {"role": "assistant", "content": content}}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    choice.update(extra)
    return {"model": "served/model", "choices": [choice]}


class _FakeOpener:
    """OpenerDirector-like: `.open(request, timeout=)` (D16). Records the request;
    returns a file-like body or raises the configured exception."""

    def __init__(self, body=None, exc: BaseException | None = None):
        self.body = body
        self.exc = exc
        self.requests: list = []
        self.timeouts: list = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self.exc is not None:
            raise self.exc
        raw = self.body if isinstance(self.body, bytes) else json.dumps(self.body)
        return io.BytesIO(raw if isinstance(raw, bytes) else raw.encode("utf-8"))


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://llm.example/v1/chat/completions",
        code,
        f"status {code} {BODY_MARKER}",
        None,  # type: ignore[arg-type]
        io.BytesIO(BODY_MARKER.encode()),
    )


# ── T-S1 resolver ─────────────────────────────────────────────────────────
def test_resolve_env_key_wins_stripped():
    s = resolve_llm_settings(
        "https://b/v1", "m", "stored", environ={LLM_KEY_ENV: "  envkey \n"}
    )
    assert (s.api_key, s.key_source) == ("envkey", "env")
    assert (s.api_base, s.model) == ("https://b/v1", "m")


def test_resolve_blank_env_falls_back_to_config_stripped():
    s = resolve_llm_settings("b", "m", "  stored\r\n", environ={LLM_KEY_ENV: "   "})
    assert (s.api_key, s.key_source) == ("stored", "config")


def test_resolve_neither_is_none():
    s = resolve_llm_settings("b", "m", "   ", environ={})
    assert (s.api_key, s.key_source) == ("", "none")


def test_resolve_reads_os_environ_by_default(monkeypatch):
    monkeypatch.setenv(LLM_KEY_ENV, "fromenv")
    assert resolve_llm_settings("b", "m", "").key_source == "env"


def test_llm_settings_from_config():
    from taskpaw_v3.core.config import AgentConfig
    from taskpaw_v3.core.llm import llm_settings_from_config

    cfg = AgentConfig(server_id="s", machine="m", llm_api_key="k", llm_model="x/y")
    s = llm_settings_from_config(cfg, environ={})
    assert s == LLMSettings(DEFAULT_LLM_API_BASE, "x/y", "k", "config")


# ── T-S2 holder ───────────────────────────────────────────────────────────
def test_holder_defaults_set_get_reset():
    d = get_llm_settings()
    assert d == LLMSettings(DEFAULT_LLM_API_BASE, DEFAULT_LLM_MODEL, "", "none")
    s = _settings()
    set_llm_settings(s)
    got = get_llm_settings()
    assert got is s  # the same immutable snapshot
    with pytest.raises(AttributeError):  # frozen
        got.api_key = "x"  # type: ignore[misc]
    reset_llm_settings()
    assert get_llm_settings() == d


# ── T-C1 request shape ────────────────────────────────────────────────────
def test_chat_request_shape_with_key():
    op = _FakeOpener(_envelope())
    chat(
        _settings(),
        _msgs(),
        temperature=0.1,
        max_tokens=7,
        json_mode=True,
        timeout=12.5,
        opener=op,
    )
    (req,) = op.requests
    assert req.full_url == "https://llm.example/v1/chat/completions"
    assert req.get_method() == "POST"
    body = json.loads(req.data)
    assert body == {
        "model": "m/x",
        "messages": _msgs(),
        "temperature": 0.1,
        "max_tokens": 7,
        "response_format": {"type": "json_object"},
    }
    assert req.get_header("Content-type") == "application/json"
    from taskpaw_v3 import __version__

    assert req.get_header("User-agent") == f"TaskPaw/{__version__}"
    # Authorization is UNREDIRECTED: never re-sent to a redirect target (D10).
    assert req.unredirected_hdrs.get("Authorization") == f"Bearer {KEY_MARKER}"
    assert "Authorization" not in req.headers
    assert op.timeouts == [12.5]


def test_chat_request_shape_without_key_or_optionals():
    op = _FakeOpener(_envelope())
    chat(_settings(key=""), _msgs(), opener=op)
    (req,) = op.requests
    body = json.loads(req.data)
    assert set(body) == {"model", "messages", "temperature"}
    assert body["temperature"] == 0.3
    assert not req.has_header("Authorization")  # local Ollama: no header at all
    assert op.timeouts == [30.0]


# ── T-C2 success envelope ─────────────────────────────────────────────────
def test_chat_success():
    r = chat(_settings(), _msgs(), opener=_FakeOpener(_envelope("  hi  ")))
    assert isinstance(r, ChatResult)
    assert (r.content, r.finish_reason, r.model) == ("  hi  ", "stop", "served/model")
    assert isinstance(r.latency_ms, int) and r.latency_ms >= 0


def test_chat_missing_finish_reason_is_stop_and_model_falls_back():
    body = _envelope(finish_reason=None)
    del body["model"]
    r = chat(_settings(), _msgs(), opener=_FakeOpener(body))
    assert (r.finish_reason, r.model) == ("stop", "m/x")


def test_chat_length_strict_vs_lenient():
    body = _envelope("partial", finish_reason="length")
    with pytest.raises(LLMError) as ei:
        chat(_settings(), _msgs(), opener=_FakeOpener(body))
    assert ei.value.kind == "bad_response"
    assert ei.value.message == "finish_reason=length"
    r = chat(_settings(), _msgs(), strict=False, opener=_FakeOpener(body))
    assert (r.content, r.finish_reason) == ("partial", "length")


@pytest.mark.parametrize("content", [None, ""])
def test_chat_lenient_length_with_null_or_empty_content(content):
    # D14: a reasoning model that spends the budget thinking → null/empty + length.
    body = _envelope(content, finish_reason="length")
    r = chat(_settings(), _msgs(), strict=False, opener=_FakeOpener(body))
    assert (r.content, r.finish_reason) == ("", "length")


# ── T-C3 mapping table ────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "exc,kind,status,message",
    [
        (_http_error(401), "auth", 401, None),
        (_http_error(403), "auth", 403, None),
        (_http_error(429), "rate_limit", 429, None),
        (_http_error(302), "bad_response", 302, "HTTP 302 redirect not followed"),
        (_http_error(500), "bad_response", 500, "HTTP 500"),
        (
            urllib.error.URLError(f"refused {KEY_MARKER}"),
            "network",
            None,
            "network error",
        ),
        (urllib.error.URLError(socket.timeout("t")), "network", None, "timeout"),
        (socket.timeout("timed out"), "network", None, "timeout"),
        (ConnectionResetError("reset"), "network", None, "network error"),
        (
            http.client.IncompleteRead(BODY_MARKER.encode()),
            "network",
            None,
            "network error",
        ),
        (http.client.RemoteDisconnected("gone"), "network", None, "network error"),
        (
            ValueError(f"Invalid header value b'Bearer {KEY_MARKER}'"),
            "auth",
            None,
            "API key or URL contains invalid characters",
        ),
        (
            RuntimeError(f"boom {KEY_MARKER}"),
            "bad_response",
            None,
            "unexpected error: RuntimeError",
        ),
    ],
)
def test_chat_exception_mapping(exc, kind, status, message):
    with pytest.raises(LLMError) as ei:
        chat(_settings(), _msgs(), opener=_FakeOpener(exc=exc))
    e = ei.value
    assert (e.kind, e.status) == (kind, status)
    if message is not None:
        assert e.message == message
    assert KEY_MARKER not in str(e) and BODY_MARKER not in str(e)
    # The original exception (whose text may embed the key) is not chained.
    assert e.__cause__ is None and e.__suppress_context__


@pytest.mark.parametrize(
    "body,kind,message",
    [
        (b"not json " + BODY_MARKER.encode(), "bad_response", "invalid response"),
        (b"\xff\xfe", "bad_response", "invalid response"),
        ([1, 2], "bad_response", "invalid response"),
        ({"choices": []}, "bad_response", "invalid response"),
        ({"model": "x"}, "bad_response", "invalid response"),
        ({"choices": [{"finish_reason": "stop"}]}, "bad_response", "invalid response"),
        ({"choices": ["str"]}, "bad_response", "invalid response"),
        (_envelope("x", finish_reason="content_filter"), "refusal", None),
        (_envelope("", finish_reason="stop"), "refusal", "empty reply"),
        (_envelope("   \n", finish_reason="stop"), "refusal", "empty reply"),
        (
            _envelope(None, finish_reason="tool_calls"),
            "bad_response",
            "finish_reason=tool_calls",
        ),
        (_envelope(None, finish_reason="stop"), "bad_response", "invalid response"),
        (_envelope(5, finish_reason="stop"), "bad_response", "invalid response"),
    ],
)
def test_chat_envelope_mapping(body, kind, message):
    with pytest.raises(LLMError) as ei:
        chat(_settings(), _msgs(), opener=_FakeOpener(body))
    assert ei.value.kind == kind
    if message is not None:
        assert ei.value.message == message
    assert BODY_MARKER not in str(ei.value)


def test_chat_refusal_field_is_refusal():
    body = _envelope("text")
    body["choices"][0]["message"]["refusal"] = "I can't help with that"
    with pytest.raises(LLMError) as ei:
        chat(_settings(), _msgs(), opener=_FakeOpener(body))
    assert ei.value.kind == "refusal"
    # content_filter is checked before length/strict (D3): lenient still refuses.
    with pytest.raises(LLMError) as ei:
        chat(
            _settings(),
            _msgs(),
            strict=False,
            opener=_FakeOpener(_envelope("x", finish_reason="content_filter")),
        )
    assert ei.value.kind == "refusal"


def test_chat_unusual_finish_reason_value_is_not_echoed_verbatim():
    # The finish_reason value comes from the server body: only a short token-like
    # value is echoed; anything else is reported generically (never body text).
    body = _envelope("x", finish_reason=f"weird {BODY_MARKER} " * 20)
    with pytest.raises(LLMError) as ei:
        chat(_settings(), _msgs(), opener=_FakeOpener(body))
    assert ei.value.kind == "bad_response"
    assert BODY_MARKER not in str(ei.value)


# ── T-C4 secrets never in error text or logs ──────────────────────────────
def test_chat_crlf_key_maps_to_auth_without_leaking():
    # The real default opener: http.client.putheader rejects the CR/LF header
    # value with a ValueError that EMBEDS the value (A9) — it must not escape. The
    # rejection happens before any connect (and port 1 would refuse anyway).
    bad = _settings(key=f"{KEY_MARKER}\r\nX-Injected: 1", base="http://127.0.0.1:1/v1")
    with pytest.raises(LLMError) as ei:
        chat(bad, _msgs())
    assert ei.value.kind == "auth"
    assert ei.value.message == "API key or URL contains invalid characters"
    assert KEY_MARKER not in str(ei.value)
    assert ei.value.__cause__ is None and ei.value.__suppress_context__


def test_chat_logs_never_carry_key_prompt_or_body(caplog):
    caplog.set_level(logging.DEBUG)
    cases = [
        _FakeOpener(_envelope(f"reply {BODY_MARKER}")),
        _FakeOpener(_envelope("x", finish_reason="length")),
        _FakeOpener(b"garbage " + BODY_MARKER.encode()),
        _FakeOpener(exc=_http_error(401)),
        _FakeOpener(exc=_http_error(500)),
        _FakeOpener(exc=urllib.error.URLError(KEY_MARKER)),
        _FakeOpener(exc=ValueError(KEY_MARKER)),
        _FakeOpener(exc=RuntimeError(KEY_MARKER)),
    ]
    for op in cases:
        try:
            chat(_settings(), _msgs(), opener=op)
        except LLMError:
            pass
    records = [r for r in caplog.records if r.name == "taskpaw.llm"]
    assert len(records) == len(cases)  # exactly one log line per call
    for text in (caplog.text, *(r.getMessage() for r in caplog.records)):
        assert KEY_MARKER not in text
        assert PROMPT_MARKER not in text
        assert BODY_MARKER not in text
        assert "Bearer" not in text


# ── T-C5 default opener refuses redirects (D10/D16) ──────────────────────
class _Recorder(BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep test output quiet
        pass

    def _record(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        self.server.seen.append((self.command, self.path, dict(self.headers), body))

    def do_GET(self):
        self._record()
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        self._record()
        if self.server.redirect_to:
            self.send_response(302)
            self.send_header("Location", self.server.redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


def _serve(handler, **attrs) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    srv.seen = []  # type: ignore[attr-defined]
    srv.redirect_to = None  # type: ignore[attr-defined]
    for k, v in attrs.items():
        setattr(srv, k, v)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _stop(*servers: ThreadingHTTPServer) -> None:
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def test_chat_default_opener_does_not_follow_redirect():
    leak = _serve(_Recorder)
    first = _serve(
        _Recorder, redirect_to=f"http://127.0.0.1:{leak.server_address[1]}/leak"
    )
    try:
        s = _settings(base=f"http://127.0.0.1:{first.server_address[1]}/v1")
        with pytest.raises(LLMError) as ei:
            chat(s, _msgs(), timeout=5)
        assert ei.value.kind == "bad_response"
        assert ei.value.message == "HTTP 302 redirect not followed"
        assert ei.value.status == 302
        assert leak.seen == []  # the redirect target received nothing
        ((method, path, headers, _body),) = first.seen
        assert (method, path) == ("POST", "/v1/chat/completions")
        # The unredirected Authorization reached the intended origin only.
        assert headers.get("Authorization") == f"Bearer {KEY_MARKER}"
    finally:
        _stop(first, leak)


def test_default_opener_has_no_redirect_handler():
    assert any(isinstance(h, llm._NoRedirect) for h in llm._DEFAULT_OPENER.handlers)


# ══ llm-worker sidecar (AC3) ══════════════════════════════════════════════


# ── T-W1 argv / env ───────────────────────────────────────────────────────
def test_worker_argv_frozen_vs_source(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert worker_argv() == [sys.executable, "-m", "taskpaw_v3.core.llm_worker"]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\App\taskpaw-backend.exe")
    assert worker_argv() == [r"C:\App\taskpaw-backend.exe", "llm-worker"]


def test_worker_env_sets_vars_without_mutating(monkeypatch):
    monkeypatch.setenv("TASKPAW_TEST_PASSTHRU", "1")
    before = dict(os.environ)
    env = worker_env(_settings())
    assert env[ENV_BASE] == "https://llm.example/v1"
    assert env[ENV_MODEL] == "m/x"
    assert env[ENV_KEY] == KEY_MARKER
    assert env["TASKPAW_TEST_PASSTHRU"] == "1"  # default base = os.environ copy
    assert dict(os.environ) == before  # never mutates the parent's environment
    base = {"PATH": "p", ENV_KEY: "stale"}
    env = worker_env(_settings(key=""), base)
    assert ENV_KEY not in env  # empty key → removed, not inherited
    assert base == {"PATH": "p", ENV_KEY: "stale"}  # base untouched
    assert env["PATH"] == "p"


def test_settings_from_env_round_trips():
    s = _settings()
    back = settings_from_env(worker_env(s, {}))
    assert (back.api_base, back.model, back.api_key) == (
        s.api_base,
        s.model,
        s.api_key,
    )
    assert back.key_source == "env"
    none = settings_from_env(worker_env(_settings(key=""), {}))
    assert (none.api_key, none.key_source) == ("", "none")


# ── T-W2 handle_request (pure, never raises) ──────────────────────────────
def _ok_chat(calls: list):
    def fake(settings, messages, **kw):
        calls.append((settings, messages, kw))
        return ChatResult("こんにちは", "stop", settings.model, 12)

    return fake


def _line(**req) -> str:
    return json.dumps({"messages": _msgs(), **req})


def test_handle_request_success_and_params():
    calls: list = []
    out = handle_request(
        _line(id="r1", temperature=0.2, max_tokens=50, json_mode=True, timeout=9),
        _settings(),
        chat_fn=_ok_chat(calls),
    )
    assert "\n" not in out and "\r" not in out  # single line
    assert json.loads(out) == {
        "id": "r1",
        "ok": True,
        "content": "こんにちは",
        "finish_reason": "stop",
        "model": "m/x",
        "latency_ms": 12,
    }
    ((settings, messages, kw),) = calls
    assert settings == _settings() and messages == _msgs()
    assert kw == {"temperature": 0.2, "max_tokens": 50, "json_mode": True, "timeout": 9}


def test_handle_request_defaults():
    calls: list = []
    handle_request(_line(id=None), _settings(), chat_fn=_ok_chat(calls))
    assert calls[0][2] == {
        "temperature": 0.3,
        "max_tokens": None,
        "json_mode": False,
        "timeout": 30.0,
    }


def test_handle_request_overrides_base_and_model_but_never_key():
    calls: list = []
    handle_request(
        _line(id=7, api_base="http://other/v1/", model="o/m", api_key="evil"),
        _settings(),
        chat_fn=_ok_chat(calls),
    )
    s = calls[0][0]
    assert (s.api_base, s.model) == ("http://other/v1", "o/m")
    assert s.api_key == KEY_MARKER  # the request cannot override the key


@pytest.mark.parametrize("rid", ["abc", 42])
def test_handle_request_llm_error_echoes_id(rid):
    def fake(*a, **k):
        raise LLMError("rate_limit", "rate limited", 429)

    out = json.loads(handle_request(_line(id=rid), _settings(), chat_fn=fake))
    assert out == {
        "id": rid,
        "ok": False,
        "kind": "rate_limit",
        "status": 429,
        "message": "rate limited",
    }


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        "{{{",
        "[1, 2]",
        json.dumps({"id": 1}),  # no messages
        json.dumps({"id": 1, "messages": "hi"}),
        json.dumps({"id": 1, "messages": [], "temperature": "hot"}),
        json.dumps({"id": 1, "messages": [], "max_tokens": True}),
        json.dumps({"id": 1, "messages": [], "json_mode": "yes"}),
        json.dumps({"id": 1, "messages": [], "timeout": -1}),
        json.dumps({"id": 1, "messages": [], "api_base": 5}),
        json.dumps({"id": {"nested": 1}, "messages": []}),  # id not a scalar
    ],
)
def test_handle_request_invalid_request(line):
    def boom(*a, **k):
        raise AssertionError("chat must not be called")

    out = json.loads(handle_request(line, _settings(), chat_fn=boom))
    assert out["ok"] is False and out["kind"] == "bad_response"
    assert out["message"] == "invalid request" and out["status"] is None
    assert out["id"] in (None, 1)


def test_handle_request_garbage_has_null_id():
    assert json.loads(handle_request("{{{", _settings()))["id"] is None


def test_handle_request_unexpected_exception_is_contained():
    def boom(*a, **k):
        raise RuntimeError(KEY_MARKER)

    out = handle_request(_line(id=3), _settings(), chat_fn=boom)
    assert json.loads(out) == {
        "id": 3,
        "ok": False,
        "kind": "bad_response",
        "status": None,
        "message": "unexpected error: RuntimeError",
    }
    assert KEY_MARKER not in out


# ── T-W3 serve(): unconditional exit on EOF (D5/D15) ──────────────────────
class _ExitRecorder:
    def __init__(self):
        self.codes: list = []
        self.called = threading.Event()

    def __call__(self, code):
        self.codes.append(code)
        self.called.set()


def test_serve_answers_in_order_then_exits_on_eof():
    stdin = io.BytesIO((_line(id=1) + "\n" + _line(id=2) + "\r\n").encode("utf-8"))
    stdout = io.StringIO()
    rec = _ExitRecorder()
    assert serve(stdin, stdout, _settings(), chat_fn=_ok_chat([]), exit_fn=rec) == 0
    lines = stdout.getvalue().split("\n")
    assert lines[-1] == ""  # every reply is \n-terminated
    assert [json.loads(x)["id"] for x in lines[:-1]] == [1, 2]
    assert rec.codes == [0]


def test_serve_exits_on_eof_while_request_in_flight():
    release = threading.Event()

    def blocking_chat(settings, messages, **kw):
        release.wait(10)
        return ChatResult("late", "stop", "m", 1)

    stdin = io.BytesIO((_line(id="slow") + "\n").encode("utf-8"))
    stdout = io.StringIO()
    rec = _ExitRecorder()
    result: list = []
    t = threading.Thread(
        target=lambda: result.append(
            serve(stdin, stdout, _settings(), chat_fn=blocking_chat, exit_fn=rec)
        ),
        daemon=True,
    )
    t.start()
    try:
        # EOF → exit_fn(0) immediately, BEFORE the in-flight request completes.
        assert rec.called.wait(1.0)
        assert not release.is_set() and stdout.getvalue() == ""
        assert rec.codes == [0]
    finally:
        release.set()
    t.join(5)
    assert result == [0]  # the sentinel lets an injected exit_fn return


# ── T-W4 Job Object (best-effort layer, D6) ───────────────────────────────
def test_job_object_is_none_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert assign_kill_on_close_job(object()) is None  # type: ignore[arg-type]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects")
def test_job_object_kills_child_on_close():
    # The base interpreter, not the venv's python.exe launcher (critic E6): the
    # job must hold the process that actually sleeps.
    exe = getattr(sys, "_base_executable", sys.executable)
    proc = subprocess.Popen([exe, "-I", "-c", "import time; time.sleep(60)"])
    try:
        keeper = assign_kill_on_close_job(proc)
        assert keeper is not None
        assert proc.poll() is None
        keeper.close()
        proc.wait(timeout=2)
        keeper.close()  # idempotent
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(5)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects")
def test_job_object_failure_is_logged_and_none(caplog):
    class _Bogus:
        _handle = 0  # not a process handle → AssignProcessToJobObject fails

    caplog.set_level(logging.WARNING)
    assert assign_kill_on_close_job(_Bogus()) is None  # type: ignore[arg-type]
    assert any("job object" in r.getMessage().lower() for r in caplog.records)


# ── T-W5 integration: real worker subprocess + loopback fake LLM ──────────
class _FakeLLM(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        srv = self.server
        srv.seen.append((dict(self.headers), body))
        srv.arrived.set()
        self.close_connection = True
        if srv.mode == "ok":
            reply = json.dumps(_envelope("こんにちは OK")).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)
        elif srv.mode == "500":
            msg = f"upstream {BODY_MARKER}".encode()
            self.send_response(500)
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
        elif srv.mode == "hang":  # hold the connection open, send nothing
            srv.release.wait(30)
        elif srv.mode == "drip":  # headers, then one body byte per second
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "100000")
            self.end_headers()
            try:
                while not srv.release.is_set():
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    srv.release.wait(1.0)
            except OSError:
                return  # the worker was killed — the point of the test


def _fake_llm(mode: str) -> ThreadingHTTPServer:
    return _serve(
        _FakeLLM, mode=mode, arrived=threading.Event(), release=threading.Event()
    )


def _stop_fake(srv: ThreadingHTTPServer) -> None:
    srv.release.set()  # type: ignore[attr-defined]
    _stop(srv)


def _spawn_worker(srv: ThreadingHTTPServer):
    s = _settings(base=f"http://127.0.0.1:{srv.server_address[1]}/v1")
    # Hermetic: bypass any configured proxy for the loopback fake.
    base = {k: v for k, v in os.environ.items() if k.lower() != "no_proxy"}
    base["no_proxy"] = "*"
    env = worker_env(s, base)
    proc = subprocess.Popen(
        worker_argv(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(REPO_ROOT),
    )
    return proc, env


def _readline(stream, timeout: float = 30.0) -> bytes:
    box: list = []
    t = threading.Thread(target=lambda: box.append(stream.readline()), daemon=True)
    t.start()
    t.join(timeout)
    assert box, "worker did not answer in time"
    return box[0]


def _send_req(proc, **req) -> None:
    proc.stdin.write((json.dumps({"messages": _msgs(), **req}) + "\n").encode())
    proc.stdin.flush()


def _reap(proc) -> bytes:
    if proc.poll() is None:
        proc.kill()
    try:
        _out, err = proc.communicate(timeout=10)
    except ValueError:  # pipes already consumed/closed by an earlier _reap
        return b""
    return err or b""


def test_worker_subprocess_round_trip_error_and_secrets():
    srv = _fake_llm("ok")
    proc, env = _spawn_worker(srv)
    try:
        # (c) the key travels in the child's environment, never in argv.
        assert env[ENV_KEY] == KEY_MARKER
        assert all(KEY_MARKER not in str(a) for a in proc.args)
        # (a) a normal request round-trips through the real chat().
        _send_req(proc, id="a", timeout=10)
        line = _readline(proc.stdout)
        assert line.endswith(b"\n") and not line.endswith(b"\r\n")
        reply = json.loads(line.decode("utf-8"))
        assert reply == {
            "id": "a",
            "ok": True,
            "content": "こんにちは OK",
            "finish_reason": "stop",
            "model": "served/model",
            "latency_ms": reply["latency_ms"],
        }
        headers, body = srv.seen[0]  # type: ignore[attr-defined]
        assert headers["Authorization"] == f"Bearer {KEY_MARKER}"
        assert json.loads(body)["model"] == "m/x"
        # (b) a 500 maps to an error line.
        srv.mode = "500"  # type: ignore[attr-defined]
        _send_req(proc, id=2, timeout=10)
        err = json.loads(_readline(proc.stdout).decode("utf-8"))
        assert err == {
            "id": 2,
            "ok": False,
            "kind": "bad_response",
            "status": 500,
            "message": "HTTP 500",
        }
        # Closing stdin (the cancel contract) ends an idle worker with 0.
        proc.stdin.close()
        assert proc.wait(timeout=5) == 0
        # (f) the worker's stderr never carries the key (or prompt/body).
        stderr = _reap(proc).decode("utf-8", "replace")
        assert "llm chat:" in stderr  # the kind/status/latency lines are there
        for marker in (KEY_MARKER, PROMPT_MARKER, BODY_MARKER, "Bearer"):
            assert marker not in stderr
    finally:
        _reap(proc)
        _stop_fake(srv)


def test_worker_close_stdin_cancels_in_flight_request():
    # (d) the server holds the connection open; EOF on stdin must end the worker
    # within 1 s even though its main thread is blocked in a socket read (D5).
    srv = _fake_llm("hang")
    proc, _env = _spawn_worker(srv)
    try:
        _send_req(proc, id="d", timeout=60)
        assert srv.arrived.wait(30)  # type: ignore[attr-defined]
        time.sleep(0.2)  # the worker is now inside resp.read()
        assert proc.poll() is None
        proc.stdin.close()
        assert proc.wait(timeout=1.0) == 0
        assert _readline(proc.stdout, 1.0) == b""  # no half-written reply
        stderr = _reap(proc).decode("utf-8", "replace")
        assert KEY_MARKER not in stderr
    finally:
        _reap(proc)
        _stop_fake(srv)


@pytest.mark.skipif(sys.platform != "win32", reason="taskkill tree kill")
def test_worker_tree_kill_during_drip():
    # (e) the fallback: a slow-drip body defeats the socket timeout; a tree kill
    # ends the whole worker tree (venv launcher + interpreter) and the parent's
    # pipe read returns promptly.
    srv = _fake_llm("drip")
    proc, _env = _spawn_worker(srv)
    try:
        _send_req(proc, id="e", timeout=60)
        assert srv.arrived.wait(30)  # type: ignore[attr-defined]
        box: list = []
        reader = threading.Thread(
            target=lambda: box.append(proc.stdout.readline()), daemon=True
        )
        reader.start()
        time.sleep(0.2)
        assert proc.poll() is None and not box
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
        reader.join(1.0)
        assert not reader.is_alive() and box == [b""]
        stderr = _reap(proc).decode("utf-8", "replace")
        assert KEY_MARKER not in stderr
    finally:
        _reap(proc)
        _stop_fake(srv)


def test_worker_main_reads_env_and_runs_serve(monkeypatch):
    # main() reads settings from the environment and hands the real stdio to
    # serve(); its return value is serve()'s (reached only under a fake).
    seen = {}

    def fake_serve(stdin, stdout, settings, **kw):
        seen["settings"] = settings
        return 0

    monkeypatch.setattr(llm_worker, "serve", fake_serve)
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: None)
    out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", newline="\r\n")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO()))
    monkeypatch.setenv(ENV_BASE, "http://h/v1")
    monkeypatch.setenv(ENV_MODEL, "a/b")
    monkeypatch.setenv(ENV_KEY, "k")
    assert llm_worker.main([]) == 0
    assert seen["settings"] == LLMSettings("http://h/v1", "a/b", "k", "env")
    # stdout is reconfigured for the protocol: UTF-8, bare \n line endings.
    assert out.encoding == "utf-8"
    out.write("x\n")
    out.flush()
    assert out.buffer.getvalue() == b"x\n"
