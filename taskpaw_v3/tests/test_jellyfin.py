"""`jellyfin` monitor (#257): config, probe, state table, events.

Everything runs against a loopback throwaway HTTP server — no real Jellyfin.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from taskpaw_v3.agent.catalog import plugin_catalog
from taskpaw_v3.core.http import NoRedirectHandler
from taskpaw_v3.monitors.plugins import jellyfin as jf
from taskpaw_v3.monitors.plugins.jellyfin import (
    JellyfinConfig,
    JellyfinInstance,
    JellyfinPlugin,
)
from taskpaw_v3.monitors.registry import default_registry

INFO = {
    "ProductName": "Jellyfin Server",
    "Version": "12.2.0",
    "ServerName": "ThunderPig",
    "StartupWizardCompleted": True,
}
HEALTHY = (200, b"Healthy")


def _json(obj, status=200):
    return (status, json.dumps(obj).encode("utf-8"))


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass  # a client that hung up on a dripped body is expected here


@contextlib.contextmanager
def _serve(routes):
    """Serve `routes`: path → (status, body) or a callable(handler)."""
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            reply = routes.get(self.path, (404, b"not found"))
            if callable(reply):
                reply(self)
                return
            status, body = reply
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = _Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _status(routes, timeout=5.0, prefix=""):
    with _serve(routes) as (base, _seen):
        return jf.evaluate(jf.probe(base + prefix, timeout))


def _drip(delay, count=200):
    def reply(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", str(count))
        handler.end_headers()
        for _ in range(count):
            handler.wfile.write(b"x")
            handler.wfile.flush()
            time.sleep(delay)

    return reply


# ── config ────────────────────────────────────────────────────────────────
def test_config_default_and_normalisation():
    assert JellyfinConfig(name="jf").base_url == "http://127.0.0.1:8096"
    assert (
        JellyfinConfig(name="jf", base_url="https://media.lan/").base_url
        == "https://media.lan"
    )
    assert (
        JellyfinConfig(name="jf", base_url="http://h:8096/jellyfin/").base_url
        == "http://h:8096/jellyfin"
    )


@pytest.mark.parametrize(
    "url",
    [
        "ftp://h:8096",
        "h:8096",
        "http://",
        "http://:8096",
        "http://user:pw@h:8096",
        "http://user@h:8096",
        "http://h:8096/?a=1",
        "http://h:8096/#frag",
        "http://h:notaport",
        " http://h:8096",
        "http://h:8096 ",
        "http://h :8096",
        "http://h:8096\n",
        "http://h:8096/\tx",
        "",
    ],
)
def test_config_rejects_bad_base_url(url):
    with pytest.raises(ValueError):
        JellyfinConfig(name="jf", base_url=url)


def test_config_rejects_unknown_key_and_has_form_schema():
    plugin = JellyfinPlugin()
    with pytest.raises(ValueError):
        plugin.validate_config({"name": "jf", "api_key": "secret"})
    prop = plugin.json_schema()["properties"]["base_url"]
    assert prop["title"] == "Base URL" and prop["default"] == "http://127.0.0.1:8096"
    assert plugin.manual_start(plugin.validate_config({"name": "jf"})) is False


def test_registered_and_in_catalog():
    reg = default_registry()
    assert reg.get("jellyfin").category == "service"
    entry = {p["type_id"]: p for p in plugin_catalog()}["jellyfin"]
    assert entry["display_name"] == "Jellyfin" and entry["system"] is False
    assert "base_url" in entry["json_schema"]["properties"]


# ── state table ───────────────────────────────────────────────────────────
def test_row2_healthy_identified_is_ok():
    routes = {"/health": HEALTHY, "/System/Info/Public": _json(INFO)}
    with _serve(routes) as (base, seen):
        st = jf.evaluate(jf.probe(base, 5.0))
    assert st.state == "ok" and st.detail == "healthy — ThunderPig 12.2.0"
    assert st.metrics["reachable"] is True and st.metrics["healthy"] is True
    assert st.metrics["health_status"] == 200
    assert st.metrics["version"] == "12.2.0"
    assert st.metrics["server_name"] == "ThunderPig"
    assert isinstance(st.metrics["response_ms"], float)
    assert seen == ["/health", "/System/Info/Public"]


def test_path_prefix_is_kept_for_both_requests():
    routes = {"/jf/health": HEALTHY, "/jf/System/Info/Public": _json(INFO)}
    with _serve(routes) as (base, seen):
        st = jf.evaluate(jf.probe(base + "/jf", 5.0))
    assert st.state == "ok"
    assert seen == ["/jf/health", "/jf/System/Info/Public"]


def test_row1_closed_port_is_unreachable_and_skips_info(monkeypatch):
    calls = []
    real = jf._fetch

    def spy(url, timeout, opener=None):
        calls.append(url)
        return real(url, timeout, opener)

    monkeypatch.setattr(jf, "_fetch", spy)
    st = jf.evaluate(jf.probe(f"http://127.0.0.1:{_closed_port()}", 2.0))
    assert st.state == "error" and st.detail == "unreachable"
    assert st.metrics["reachable"] is False and st.metrics["healthy"] is False
    assert len(calls) == 1 and calls[0].endswith("/health")


def test_row1_server_that_never_answers_times_out():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        started = time.monotonic()
        st = jf.evaluate(jf.probe(f"http://127.0.0.1:{listener.getsockname()[1]}", 0.3))
    assert st.state == "error" and st.detail == "unreachable"
    assert time.monotonic() - started < 5


def test_row3_wizard_not_completed_is_degraded():
    info = {**INFO, "StartupWizardCompleted": False}
    st = _status({"/health": HEALTHY, "/System/Info/Public": _json(info)})
    assert st.state == "degraded" and st.detail == "setup wizard not completed"
    assert st.metrics["healthy"] is True


@pytest.mark.parametrize("wizard", [None, "no", 0])
def test_wizard_absent_or_non_boolean_counts_as_completed(wizard):
    info = {k: v for k, v in INFO.items() if k != "StartupWizardCompleted"}
    if wizard is not None:
        info["StartupWizardCompleted"] = wizard
    st = _status({"/health": HEALTHY, "/System/Info/Public": _json(info)})
    assert st.state == "ok"


@pytest.mark.parametrize(
    "health, expected",
    [
        ((503, b"Unhealthy"), "unhealthy: 503 Unhealthy"),
        ((200, b"Degraded"), "unhealthy: 200 Degraded"),
        ((200, b""), "unhealthy: 200"),
    ],
)
def test_row4_unhealthy_identified_is_degraded(health, expected):
    st = _status({"/health": health, "/System/Info/Public": _json(INFO)})
    assert st.state == "degraded" and st.detail == expected
    assert st.metrics["healthy"] is False
    assert st.metrics["health_status"] == health[0]


def test_row4_takes_precedence_over_row3():
    info = {**INFO, "StartupWizardCompleted": False}
    st = _status({"/health": (503, b"Unhealthy"), "/System/Info/Public": _json(info)})
    assert st.state == "degraded" and st.detail == "unhealthy: 503 Unhealthy"


def test_unhealthy_body_is_capped_in_detail():
    st = _status({"/health": (500, b"x" * 5000), "/System/Info/Public": _json(INFO)})
    assert st.state == "degraded" and len(st.detail) < 120


@pytest.mark.parametrize(
    "info, status",
    [
        (_json({"ProductName": "Some Other Server"}), 200),
        (_json({"hello": "world"}), 200),
        (_json([INFO]), 200),
        ((200, b"<html>hi</html>"), 200),
        ((200, b"\xff\xfe\x00not utf8"), 200),
        ((200, b"[" * 60000), 200),  # nesting deep enough for RecursionError
        (_json({**INFO, "ProductName": 123}), 200),
        (_json({**INFO, "ProductName": None}), 200),
        ((404, b"not found"), 404),
        (_json(INFO, status=401), 401),
    ],
)
def test_row5_answer_that_is_not_jellyfin_is_error(info, status):
    st = _status({"/health": HEALTHY, "/System/Info/Public": info})
    assert st.state == "error"
    assert st.detail == f"not a Jellyfin server (info HTTP {status})"


def test_row5_also_when_health_is_unhealthy():
    st = _status({"/health": (404, b"nope")})
    assert st.state == "error"
    assert st.detail == "not a Jellyfin server (info HTTP 404)"


def test_non_string_version_and_server_name_become_blank():
    info = {**INFO, "Version": 12, "ServerName": ["x"]}
    st = _status({"/health": HEALTHY, "/System/Info/Public": _json(info)})
    assert st.state == "ok" and st.detail == "healthy"
    assert st.metrics["version"] == "" and st.metrics["server_name"] == ""


def test_row6_info_5xx_is_degraded_not_wrong_service():
    routes = {
        "/health": (503, b"starting"),
        "/System/Info/Public": (503, b"<html>starting</html>"),
    }
    st = _status(routes)
    assert st.state == "degraded"
    assert st.detail == "server info unavailable (health 503 starting)"
    st = _status({"/health": HEALTHY, "/System/Info/Public": (500, b"boom")})
    assert st.state == "degraded"
    assert st.detail == "server info unavailable (health 200 Healthy)"


def test_redirects_are_not_followed():
    def redirect(handler):
        handler.send_response(302)
        handler.send_header("Location", "/elsewhere")
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    routes = {
        "/health": redirect,
        "/System/Info/Public": redirect,
        "/elsewhere": HEALTHY,
    }
    with _serve(routes) as (base, seen):
        st = jf.evaluate(jf.probe(base, 5.0))
    assert "/elsewhere" not in seen
    assert st.state == "error"
    assert st.detail == "not a Jellyfin server (info HTTP 302)"


# ── time / size bounds ────────────────────────────────────────────────────
def test_dripped_health_body_is_cut_off_at_the_deadline():
    routes = {"/health": _drip(0.1), "/System/Info/Public": _json(INFO)}
    with _serve(routes) as (base, seen):
        started = time.monotonic()
        st = jf.evaluate(jf.probe(base, 0.5))
        elapsed = time.monotonic() - started
    assert st.state == "error" and st.detail == "unreachable"
    assert seen == ["/health"]
    assert elapsed < 5  # undeadlined, the 200-byte drip would take ~20 s


def test_dripped_info_body_is_row6():
    routes = {"/health": HEALTHY, "/System/Info/Public": _drip(0.1)}
    with _serve(routes) as (base, _seen):
        started = time.monotonic()
        st = jf.evaluate(jf.probe(base, 0.5))
        elapsed = time.monotonic() - started
    assert st.state == "degraded"
    assert st.detail == "server info unavailable (health 200 Healthy)"
    assert elapsed < 5


def test_body_read_is_capped():
    with _serve({"/big": (200, b"x" * (jf._MAX_BODY * 3))}) as (base, _seen):
        reply = jf._fetch(base + "/big", 5.0)
    assert reply is not None and len(reply.body) == jf._MAX_BODY


def test_environment_proxy_is_not_used(monkeypatch):
    for var in ("no_proxy", "NO_PROXY", "HTTP_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{_closed_port()}")
    routes = {"/health": HEALTHY, "/System/Info/Public": _json(INFO)}
    with _serve(routes) as (base, _seen):
        # Control: an opener built now WITHOUT the empty ProxyHandler goes to
        # the dead proxy, so this test fails if the plugin's factory drops it.
        naive = urllib.request.build_opener(NoRedirectHandler())
        assert jf._fetch(base + "/health", 2.0, naive) is None
        st = jf.evaluate(jf.probe(base, 5.0, jf._build_opener()))
    assert st.state == "ok"


# ── events ────────────────────────────────────────────────────────────────
OK = jf.JellyfinProbe(jf._Reply(*HEALTHY), jf._Reply(*_json(INFO)), 1.0)
DEGRADED = jf.JellyfinProbe(jf._Reply(503, b"Unhealthy"), jf._Reply(*_json(INFO)), 1.0)
DOWN = jf.JellyfinProbe(None, None, None)


def _run(monkeypatch, probes):
    """Feed `probes` through one instance; return per-check (state, events)."""
    it = iter(probes)
    monkeypatch.setattr(jf, "probe", lambda base_url, timeout: next(it))
    inst = JellyfinInstance("i1", JellyfinConfig(name="jf"))
    out = []
    for _ in probes:
        events: list[tuple] = []

        def emit(level, title, message, data=None, dedupe_key=None):
            events.append((level, title, message, dedupe_key))

        out.append((inst.check(emit).state, events))
    return out


def test_down_at_startup_alerts_once_then_recovers(monkeypatch):
    out = _run(monkeypatch, [DOWN, DOWN, OK, OK, DOWN])
    assert [s for s, _ in out] == ["error", "error", "ok", "ok", "error"]
    assert out[0][1] == [
        ("alert", "jf down", "http://127.0.0.1:8096: unreachable", None)
    ]
    assert out[1][1] == []
    assert out[2][1] == [
        (
            "done",
            "jf healthy",
            "http://127.0.0.1:8096: healthy — ThunderPig 12.2.0",
            None,
        )
    ]
    assert out[3][1] == []
    # a SECOND outage alerts again — no stable dedupe_key swallowing it
    assert [e[0] for e in out[4][1]] == ["alert"]


def test_first_check_ok_is_silent_and_degraded_transitions(monkeypatch):
    out = _run(monkeypatch, [OK, DEGRADED, DEGRADED, DOWN, DEGRADED, OK])
    assert [[e[0] for e in ev] for _, ev in out] == [
        [],
        ["warn"],
        [],
        ["alert"],
        ["warn"],
        ["done"],
    ]
    assert out[1][1][0][1:3] == (
        "jf degraded",
        "http://127.0.0.1:8096: unhealthy: 503 Unhealthy",
    )


def test_degraded_at_startup_warns(monkeypatch):
    out = _run(monkeypatch, [DEGRADED])
    assert [e[0] for e in out[0][1]] == ["warn"]


def test_check_uses_config_url_and_timeout(monkeypatch):
    seen = []

    def fake(base_url, timeout):
        seen.append((base_url, timeout))
        return OK

    monkeypatch.setattr(jf, "probe", fake)
    cfg = JellyfinConfig(name="jf", base_url="http://h:1/x/", timeout=7)
    inst = JellyfinPlugin().create("i1", cfg)
    assert inst.check(lambda *a, **k: None).state == "ok"
    assert seen == [("http://h:1/x", 7.0)]
