"""Core: protocol (clear-on-ack), auth, lifecycle, config."""

from __future__ import annotations

import subprocess
import sys

import pytest

from taskpaw_v3.core.auth import token_ok
from taskpaw_v3.core.config import AgentConfig, HubConfig, load_yaml, save_yaml
from taskpaw_v3.core.lifecycle import GracefulShutdown
from taskpaw_v3.core.protocol import EventQueue


# ── protocol ──────────────────────────────────────────────────────────────
def test_eventqueue_monotonic_and_additive_fields():
    q = EventQueue(machine="m")
    e1 = q.add("mon", "first")
    assert e1["id"] == 1 and set(e1) == {"id", "time", "machine", "monitor", "message"}
    data = {"k": 1}
    e2 = q.add("mon", "rich", level="alert", title="T", data=data)
    data["k"] = 2  # caller mutation must not leak into the queued event
    assert e2["id"] == 2 and e2["level"] == "alert" and e2["title"] == "T"
    assert e2["data"] == {"k": 1}


def test_eventqueue_clear_on_ack_vs_legacy():
    q = EventQueue(machine="m")
    q.add("mon", "a")
    q.add("mon", "b")
    # ack=0 returns both without clearing (survives a Hub crash before persist)
    assert [e["id"] for e in q.payload(ack_id=0)["events"]] == [1, 2]
    assert [e["id"] for e in q.payload(ack_id=0)["events"]] == [1, 2]
    # ack=2 trims both
    assert q.payload(ack_id=2)["events"] == []
    # legacy (no ack) clears on read
    q.add("mon", "c")
    assert len(q.payload()["events"]) == 1
    assert q.payload()["events"] == []


def test_eventqueue_recent_is_non_destructive_and_bounded():
    # The console event log reads recent() — newest last, capped, and it must NOT
    # drain the queue the Hub polls (#44).
    q = EventQueue(machine="m")
    for i in range(5):
        q.add("mon", f"e{i}", level="info")
    recent = q.recent(limit=3)
    assert [e["message"] for e in recent] == ["e2", "e3", "e4"]  # newest last, last 3
    assert len(q.payload(ack_id=0)["events"]) == 5  # nothing consumed
    assert q.recent(limit=0) == []


def test_eventqueue_recent_filters_by_monitor():
    # #130: recent(monitor=...) returns only that monitor's events, filtered BEFORE
    # the limit slice so a chatty neighbour can't crowd out a quiet monitor.
    q = EventQueue(machine="m")
    for i in range(5):
        q.add("noisy", f"n{i}")
    q.add("quiet", "q0")
    # last 2 overall would be n4 + q0; filtered to "quiet" we still see q0.
    assert [e["message"] for e in q.recent(limit=2, monitor="quiet")] == ["q0"]
    assert [e["message"] for e in q.recent(monitor="noisy")] == [
        f"n{i}" for i in range(5)
    ]
    assert q.recent(monitor="ghost") == []
    # unfiltered behaviour is unchanged (monitor defaults to None)
    assert len(q.recent()) == 6


def test_eventqueue_last_event_times():
    # #130: map of monitor -> most-recent event time, for per-monitor freshness.
    q = EventQueue(machine="m")
    assert q.last_event_times() == {}
    a0 = q.add("a", "a0")
    q.add("b", "b0")
    a1 = q.add("a", "a1")
    times = q.last_event_times()
    assert set(times) == {"a", "b"}
    assert (
        times["a"] == a1["time"] and times["a"] >= a0["time"]
    )  # newest wins per monitor


def test_eventqueue_recent_survives_hub_ack_drain():
    # The Events tab must keep showing local history even when the Hub poll has
    # drained the delivery queue (recent() reads a separate ring, not _queue) (#44).
    q = EventQueue(machine="m")
    for i in range(3):
        q.add("mon", f"e{i}")
    assert q.payload()["events"]  # legacy drain clears _queue
    assert q.payload(ack_id=0)["events"] == []  # _queue is now empty
    assert [e["message"] for e in q.recent()] == ["e0", "e1", "e2"]  # history survived


def test_eventqueue_persists_counter_before_visible():
    saved = []
    q = EventQueue(machine="m", persist_counter=saved.append)
    q.add("mon", "x")
    assert saved == [2]  # next_id persisted during add()


def test_eventqueue_persist_failure_keeps_event_invisible():
    """If the counter persist raises, nothing is mutated: the event is not
    appended and the id is not advanced (durable-before-visible)."""

    def boom(_next_id):
        raise OSError("disk full")

    q = EventQueue(machine="m", persist_counter=boom)
    with pytest.raises(OSError):
        q.add("mon", "x")
    assert len(q) == 0
    assert q.next_id == 1  # not advanced
    # A working persist afterwards still starts at id 1 (no gap/reuse).
    saved = []
    q._persist_counter = saved.append
    assert q.add("mon", "y")["id"] == 1
    assert saved == [2]


def test_eventqueue_cap_drops_oldest():
    dropped = []
    q = EventQueue(machine="m", max_size=3, on_overflow=dropped.append)
    for i in range(5):
        q.add("mon", f"e{i}")
    ids = [e["id"] for e in q.payload(ack_id=0)["events"]]
    assert ids == [3, 4, 5] and dropped == [1, 1]


def test_eventqueue_rejects_bad_level_and_data():
    q = EventQueue(machine="m")
    with pytest.raises(ValueError):
        q.add("mon", "x", level="nope")
    with pytest.raises(ValueError):
        q.add("mon", "x", data=["not", "a", "dict"])


# ── auth ────────────────────────────────────────────────────────────────--
def test_auth_disabled_when_token_empty():
    assert token_ok("", None) is True
    assert token_ok("   ", "anything") is True


def test_auth_requires_exact_bearer():
    assert token_ok("secret", "Bearer secret") is True
    assert token_ok("secret", "Bearer wrong") is False
    assert token_ok("secret", None) is False
    assert token_ok("secret", "secret") is False


# ── lifecycle ───────────────────────────────────────────────────────────--
def test_graceful_shutdown_runs_lifo_and_idempotent():
    gs = GracefulShutdown()
    order = []
    gs.register("a", lambda: order.append("a"))
    gs.register("b", lambda: order.append("b"))
    gs.shutdown()
    gs.shutdown()  # idempotent
    assert order == ["b", "a"]  # LIFO
    assert gs.stopped.is_set()


def test_graceful_shutdown_terminates_child():
    gs = GracefulShutdown(child_timeout=5)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    gs.register_child("sleeper", proc)
    gs.shutdown()
    assert proc.poll() is not None  # terminated


def test_graceful_shutdown_continues_if_callback_raises():
    gs = GracefulShutdown()
    ran = []
    gs.register("boom", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    gs.register("ok", lambda: ran.append(1))
    gs.shutdown()  # must not propagate
    assert ran == [1]  # the other callback still ran


# ── config ──────────────────────────────────────────────────────────────--
def test_agent_config_roundtrip_and_secret_masking(tmp_path):
    cfg = AgentConfig(server_id="s1", machine="dev", api_token="secret")
    path = tmp_path / "agent.yaml"
    save_yaml(cfg, path)
    loaded = load_yaml(AgentConfig, path)
    assert loaded.server_id == "s1" and loaded.bind_port == 5680


def test_agent_config_rejects_bad_port_and_nonloopback_control():
    with pytest.raises(Exception):
        AgentConfig(server_id="s", machine="m", bind_port=70000)
    with pytest.raises(Exception):
        AgentConfig(server_id="s", machine="m", control_host="0.0.0.0")


def test_hub_config_defaults():
    h = HubConfig()
    assert h.poll_interval == 60 and h.openclaw_enabled is False


def test_agent_state_persists_event_id(tmp_path):
    from taskpaw_v3.core.state import load_next_id, save_next_id

    p = tmp_path / "agent.state.json"
    assert load_next_id(p) == 1
    save_next_id(p, 42)
    assert load_next_id(p) == 42


def test_build_queue_persists_across_restart(tmp_path):
    from taskpaw_v3.agent.server.launcher import build_queue

    cfg = AgentConfig(server_id="s", machine="dev")
    state = tmp_path / "agent.state.json"
    q1 = build_queue(cfg, state)
    assert q1.add("mon", "a")["id"] == 1
    assert q1.add("mon", "b")["id"] == 2
    # New queue (simulated restart) resumes from the persisted counter.
    q2 = build_queue(cfg, state)
    assert q2.add("mon", "c")["id"] == 3


def test_bind_host_normalized_on_both_configs():
    """bind_host is trimmed + de-bracketed so the value persisted and handed to
    claim_port/loopback_url matches what the exposure guard classified (#114)."""
    assert (
        AgentConfig(server_id="s", machine="m", bind_host="  192.168.1.10  ").bind_host
        == "192.168.1.10"
    )
    assert AgentConfig(server_id="s", machine="m", bind_host="[::1]").bind_host == "::1"
    assert HubConfig(bind_host="  10.0.0.5 ").bind_host == "10.0.0.5"
    assert HubConfig(bind_host="[::1]").bind_host == "::1"


def test_agent_config_llm_fields():
    """#178 AC1: global LLM API settings — defaults, normalisation, validation."""
    c = AgentConfig(server_id="s", machine="m")
    assert c.llm_api_base == "https://api.x.ai/v1"
    assert c.llm_model == "grok-4.3"
    assert c.llm_api_key == ""
    c = AgentConfig(
        server_id="s",
        machine="m",
        llm_api_base=" https://x/v1/ ",
        llm_model="  a/b \n",
        llm_api_key=" sk-abc\r\n",
    )
    assert c.llm_api_base == "https://x/v1"  # stripped + trailing / removed
    assert c.llm_model == "a/b"
    assert c.llm_api_key == "sk-abc"  # CR/LF + whitespace normalised (D1)
    assert AgentConfig(server_id="s", machine="m", llm_api_base="  ").llm_api_base == ""
    assert (
        AgentConfig(server_id="s", machine="m", llm_api_base="HTTP://h:1/").llm_api_base
        == "HTTP://h:1"
    )
    for bad in ("ftp://x/v1", "openrouter.ai/api/v1", "javascript:alert(1)"):
        with pytest.raises(Exception):
            AgentConfig(server_id="s", machine="m", llm_api_base=bad)


def test_agent_config_llm_fallback_fields():
    # #190/#192: two optional fallback providers + the failover switch; same
    # normalisation/validation as the primary.
    c = AgentConfig(server_id="s", machine="m")
    for n in (1, 2):
        assert getattr(c, f"llm_fallback{n}_api_base") == ""
        assert getattr(c, f"llm_fallback{n}_model") == ""
        assert getattr(c, f"llm_fallback{n}_api_key") == ""
    assert c.llm_failover is True
    for n in (1, 2):
        c = AgentConfig(
            server_id="s",
            machine="m",
            **{
                f"llm_fallback{n}_api_base": " https://api.deepseek.com/v1/ ",
                f"llm_fallback{n}_model": "  deepseek-chat \n",
                f"llm_fallback{n}_api_key": " sk-abc\r\n",
            },
        )
        assert getattr(c, f"llm_fallback{n}_api_base") == "https://api.deepseek.com/v1"
        assert getattr(c, f"llm_fallback{n}_model") == "deepseek-chat"
        assert getattr(c, f"llm_fallback{n}_api_key") == "sk-abc"
        for bad in ("ftp://x/v1", "api.deepseek.com/v1", "javascript:alert(1)"):
            with pytest.raises(Exception):
                AgentConfig(
                    server_id="s", machine="m", **{f"llm_fallback{n}_api_base": bad}
                )
    assert (
        AgentConfig(server_id="s", machine="m", llm_failover=False).llm_failover
        is False
    )


def test_agent_config_fallback_key_validation_never_echoes_it():
    from pydantic import ValidationError

    marker = "sk-super-secret-fallback"
    for n in (1, 2):
        with pytest.raises(ValidationError) as ei:
            AgentConfig(
                server_id="s", machine="m", **{f"llm_fallback{n}_api_key": [marker]}
            )
        assert marker not in str(ei.value) and f"llm_fallback{n}_api_key" in str(
            ei.value
        )


def test_agent_example_yaml_documents_llm_keys():
    from taskpaw_v3 import bootstrap

    path = bootstrap.EXAMPLES / "agent.example.yaml"
    text = path.read_text(encoding="utf-8")
    for key in ("llm_api_base:", "llm_model:", "llm_api_key:"):
        assert key in text
    for key in (
        "llm_fallback1_api_base:",
        "llm_fallback1_model:",
        "llm_fallback1_api_key:",
        "llm_fallback2_api_base:",
        "llm_fallback2_model:",
        "llm_fallback2_api_key:",
        "llm_failover:",
        "TASKPAW_LLM_FALLBACK1_API_KEY",
        "TASKPAW_LLM_FALLBACK2_API_KEY",
    ):
        assert key in text
    cfg = load_yaml(AgentConfig, path)
    assert cfg.llm_api_base == "https://api.x.ai/v1"
    assert cfg.llm_model == "grok-4.3"
    assert cfg.llm_api_key == ""
    for n in (1, 2):
        assert getattr(cfg, f"llm_fallback{n}_api_base") == ""
        assert getattr(cfg, f"llm_fallback{n}_model") == ""
        assert getattr(cfg, f"llm_fallback{n}_api_key") == ""
    assert cfg.llm_failover is True


def test_data_dir_holder(tmp_path):
    # #192 C5: set only by run_agent (from its config_path); None = no
    # persistence. The autouse conftest fixture resets it to None per test.
    from taskpaw_v3.core import datadir

    assert datadir.get_data_dir() is None
    datadir.set_data_dir(tmp_path)
    assert datadir.get_data_dir() == tmp_path
    datadir.set_data_dir(None)
    assert datadir.get_data_dir() is None


def test_agent_config_validation_errors_never_echo_a_secret_input(tmp_path):
    # Codex 外门 #178: a wrongly typed secret (a YAML list, a non-string PATCH
    # value) must not be printed by pydantic's ValidationError — that text ends
    # up in the startup traceback (persisted backend log) or a 400 body
    # (constitution §2). hide_input_in_errors hides every field's input; the
    # field name and reason still show so the operator can fix the YAML.
    from pydantic import ValidationError

    marker = "sk-super-secret-marker"
    with pytest.raises(ValidationError) as ei:
        AgentConfig(server_id="s", machine="m", llm_api_key=[marker])
    text = str(ei.value)
    assert marker not in text and "llm_api_key" in text
    # errors() still carries `input` unless asked not to — the surfaces that
    # matter (str() in tracebacks / 400 details) are what the config hides.
    assert marker not in repr(ei.value.errors(include_input=False))
    # The startup path: load_yaml on a malformed agent.yaml.
    path = tmp_path / "agent.yaml"
    yaml_text = (
        f"server_id: s\nmachine: m\nllm_api_key: ['{marker}']\napi_token: [{marker}]\n"
    )
    path.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ValidationError) as ei2:
        load_yaml(AgentConfig, path)
    assert marker not in str(ei2.value)


@pytest.mark.parametrize("prefix", ["llm_", "llm_fallback1_", "llm_fallback2_"])
@pytest.mark.parametrize(
    "value", [None, " Auto ", "", "  ", True, False, "true", "false"]
)
def test_thinking_config_three_states(prefix, value):
    field = prefix + "thinking_off"
    assert getattr(AgentConfig(server_id="s", machine="m"), field) is None
    cfg = AgentConfig(server_id="s", machine="m", **{field: value})
    expected = {"true": True, "false": False}.get(value, value)
    if isinstance(value, str) and value.strip().lower() in ("", "auto"):
        expected = None
    assert getattr(cfg, field) is expected
