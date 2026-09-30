"""External CPU probe (#163): process_util.scan_activity + cpu_percents.

Fake psutil — no real processes are inspected.
"""

from __future__ import annotations

import re

import pytest

from taskpaw_v3.monitors import process_util as pu


class _CpuTimes:
    def __init__(self, user, system):
        self.user = user
        self.system = system


class _Proc:
    def __init__(self, pid, ppid, name, cmdline, user=0.0, system=0.0):
        self.info = {
            "pid": pid,
            "create_time": 1.0,
            "ppid": ppid,
            "name": name,
            "cmdline": cmdline,
            "cpu_times": _CpuTimes(user, system),
        }


class _FakePsutil:
    class NoSuchProcess(Exception): ...

    class AccessDenied(Exception): ...

    class ZombieProcess(Exception): ...

    def __init__(self, procs):
        self._procs = procs

    def process_iter(self, fields=None):
        return list(self._procs)


def _pat(**kv):
    return {k: re.compile(v, re.IGNORECASE) for k, v in kv.items()}


def test_scan_activity_sums_subtree_cpu(monkeypatch):
    # claude(10) → bash(11) → rg(12); nginx(99) is foreign.
    procs = [
        _Proc(10, 1, "claude", ["claude"], user=1.0, system=0.5),  # 1.5
        _Proc(11, 10, "bash", ["bash", "-c", "rg foo"], user=2.0, system=0.0),  # 2.0
        _Proc(12, 11, "rg", ["rg", "foo"], user=0.5, system=0.0),  # 0.5
        _Proc(99, 1, "nginx", ["nginx"], user=9.0, system=9.0),
    ]
    monkeypatch.setattr(pu, "psutil", _FakePsutil(procs))
    out = pu.scan_activity(_pat(claude=r"\bclaude\b"))
    assert out["claude"]["present"] is True
    assert out["claude"]["cpu_seconds"] == pytest.approx(4.0)  # 1.5 + 2.0 + 0.5


def test_scan_activity_absent_tool(monkeypatch):
    monkeypatch.setattr(pu, "psutil", _FakePsutil([_Proc(99, 1, "nginx", ["nginx"])]))
    out = pu.scan_activity(_pat(claude=r"\bclaude\b"))
    assert out["claude"]["present"] is False
    assert out["claude"]["cpu_seconds"] == 0.0


def test_scan_activity_no_psutil(monkeypatch):
    monkeypatch.setattr(pu, "psutil", None)
    with pytest.raises(RuntimeError):
        pu.scan_activity(_pat(claude=r"\bclaude\b"))


def test_scan_activity_subtree_is_cycle_safe(monkeypatch):
    # A pathological ppid cycle must not hang (bounded walk).
    procs = [
        _Proc(10, 12, "claude", ["claude"], user=1.0),
        _Proc(11, 10, "child", ["child"], user=1.0),
        _Proc(12, 11, "loop", ["loop"], user=1.0),  # 12's parent is 11 → cycle
    ]
    monkeypatch.setattr(pu, "psutil", _FakePsutil(procs))
    out = pu.scan_activity(_pat(claude=r"\bclaude\b"))
    assert out["claude"]["present"] is True
    assert out["claude"]["cpu_seconds"] == pytest.approx(3.0)  # each counted once


def test_scan_activity_matching_descendant_counted_once(monkeypatch):
    # Both a `claude` parent and its `claude-worker` child match the regex — every
    # pid's CPU must be summed ONCE (union of subtrees), not double-counted through
    # both its own root and its parent's subtree (Codex 外门).
    procs = [
        _Proc(10, 1, "claude", ["claude"], user=1.0),  # 1.0
        _Proc(11, 10, "claude-worker", ["claude-worker"], user=2.0),  # 2.0
        _Proc(12, 11, "bash", ["bash"], user=4.0),  # 4.0
    ]
    monkeypatch.setattr(pu, "psutil", _FakePsutil(procs))
    out = pu.scan_activity(_pat(claude=r"claude"))
    assert out["claude"]["present"] is True
    assert out["claude"]["cpu_seconds"] == pytest.approx(7.0)  # 1+2+4, each once


def _sample(cpu=4.0, present=True):
    return {
        "claude": {
            "present": present,
            "complete": True,
            "roots": [{"pid": 10, "created": 1}],
            "cpus": {(10, 1): (cpu, (10, 1))},
        }
    }


def test_cpu_percents_first_sample_is_zero():
    pct, prev = pu.cpu_percents({}, 0, _sample(), 2)
    assert pct == {} and "claude" in prev


def test_cpu_percents_delta():
    _, prev = pu.cpu_percents({}, 0, _sample(), 1)
    assert pu.cpu_percents(prev, 1, _sample(6), 3)[0]["claude"] == 100


def test_cpu_percents_exited_child_clamps_to_zero():
    _, prev = pu.cpu_percents({}, 0, _sample(10), 1)
    assert pu.cpu_percents(prev, 1, _sample(8), 3)[0]["claude"] == 0


def test_cpu_percents_absent_tool_omitted():
    assert pu.cpu_percents({}, 0, _sample(present=False), 1) == ({}, {})


# T1: exact identity fixtures reconstructed from the issue, not private captures.
def _record(pid, ppid, exe, cpu=0, created=1, name=None, args=()):
    p = _Proc(pid, ppid, name or exe.rsplit("/", 1)[-1], [exe, *args], user=cpu)
    p.info.update(exe=exe, create_time=created)
    return p


def test_exact_identity_renderer_and_real_chatgpt_codex(monkeypatch):
    procs = [
        _record(1, 0, "/sbin/init"),
        _record(
            2,
            1,
            "/Applications/ChatGPT.app/Contents/Frameworks/Renderer",
            43.7,
            args=("codex",),
        ),
        _record(3, 1, "/Applications/ChatGPT.app/Contents/MacOS/codex", 0.7),
        _record(4, 1, "/Applications/Claude.app/Contents/MacOS/Claude", 50),
        _record(5, 1, "/bin/node", 60, args=("claude MCP disclaimer",)),
    ]
    monkeypatch.setattr(pu, "psutil", _FakePsutil(procs))
    out = pu.scan_activity({"claude": None, "codex": None})
    assert not out["claude"]["present"]
    assert out["codex"]["cpu_seconds"] == 0.7


def test_global_nearest_cpu_ownership_and_hosts(monkeypatch):
    procs = [
        _record(1, 0, "/sbin/init"),
        _record(
            2,
            1,
            "/Applications/Visual Studio Code.app/Contents/MacOS/Electron",
            name="Code",
        ),
        _record(3, 2, "/bin/bash"),
        _record(4, 3, "/bin/claude", 1),
        _record(5, 4, "/bin/codex", 2),
        _record(6, 5, "/bin/worker", 4),
    ]
    monkeypatch.setattr(pu, "psutil", _FakePsutil(procs))
    out = pu.scan_activity({"claude": None, "codex": None, "vscode": None})
    assert out["claude"]["cpu_seconds"] == 1
    assert out["codex"]["cpu_seconds"] == 6
    assert out["claude"]["roots"][0]["host"] == "vscode"
    assert out["vscode"]["cpu_seconds"] == 0


@pytest.mark.parametrize(
    "exe,name,windows,expected",
    [
        (r"C:\Tools\CODEX.EXE", "codex", True, True),
        ("/bin/Codex", "Codex", False, False),
        ("/bin/node", "codex", False, True),
        ("", "Claude", False, False),
    ],
)
def test_identity_platform_and_name_fallback(monkeypatch, exe, name, windows, expected):
    monkeypatch.setattr(pu, "WINDOWS", windows, raising=False)
    monkeypatch.setattr(pu, "psutil", _FakePsutil([_record(3, 0, exe, name=name)]))
    assert pu.scan_activity({"codex": None})["codex"]["present"] is expected


@pytest.mark.parametrize(
    "exe,argv,tool",
    [
        (
            "/home/user/.local/share/claude/versions/2.1.206",
            ["/home/user/.local/bin/claude"],
            "claude",
        ),
        (
            "/usr/bin/node",
            ["node", "/usr/lib/node_modules/@moonshot-ai/kimi-code/dist/main.mjs"],
            "kimi",
        ),
        ("/usr/bin/node", ["node", "@moonshot-ai/kimi-code/dist/main.mjs"], "kimi"),
        ("/usr/bin/python3", ["python3", "-m", "kimi_cli"], "kimi"),
        ("/usr/bin/python3", ["python3", "/home/user/.local/bin/kimi"], "kimi"),
        ("/usr/bin/node", ["node", "/usr/bin/kimi-cli"], "kimi"),
    ],
)
def test_cli_launcher_identity_provides_live_root(monkeypatch, exe, argv, tool):
    monkeypatch.setattr(pu, "WINDOWS", False)
    proc = _record(3, 0, exe, cpu=2)
    proc.info["cmdline"] = argv
    monkeypatch.setattr(pu, "psutil", _FakePsutil([proc]))
    out = pu.scan_activity({tool: None})[tool]
    assert out["present"] and out["cpu_seconds"] == 2
    assert out["roots"] == [{"pid": 3, "created": 1, "host": "other"}]


@pytest.mark.parametrize(
    "exe,argv,name",
    [
        ("/Applications/Claude.app/Contents/MacOS/Claude", ["claude"], "claude"),
        ("/Applications/ChatGPT.app/Contents/Frameworks/Renderer", ["codex"], "codex"),
        ("/bin/bash", ["bash", "claude"], "bash"),
        ("/bin/node", ["node", "/unrelated/main.mjs", "kimi"], "node"),
        ("/bin/node", ["node", "--eval", "claude"], "node"),
        ("/bin/python3", ["python3", "-c", "kimi_cli"], "python3"),
        ("/bin/python3", ["python3", "-m", "kimi_cli_other"], "python3"),
    ],
)
def test_identity_fallback_still_excludes_gui_and_unrelated_arguments(
    monkeypatch, exe, argv, name
):
    monkeypatch.setattr(pu, "WINDOWS", False)
    proc = _record(3, 0, exe, name=name)
    proc.info["cmdline"] = argv
    monkeypatch.setattr(pu, "psutil", _FakePsutil([proc]))
    out = pu.scan_activity({"claude": None, "codex": None, "kimi": None})
    assert not any(sample["present"] for sample in out.values())


@pytest.mark.parametrize("editor_created,expected", [(2, "vscode"), (5, "unknown")])
def test_windows_vscode_host_before_missing_explorer_parent(
    monkeypatch, editor_created, expected
):
    monkeypatch.setattr(pu, "WINDOWS", True)
    procs = [
        _record(1, 99, r"C:\Windows\explorer.exe", created=1),
        _record(2, 1, r"C:\VSCode\Code.exe", created=editor_created),
        _record(3, 2, r"C:\VSCode\Code Helper.exe", created=editor_created),
        _record(4, 3, r"C:\Tools\claude.exe", created=4),
    ]
    monkeypatch.setattr(pu, "psutil", _FakePsutil(procs))
    out = pu.scan_activity({"claude": None})["claude"]
    assert out["roots"][0]["host"] == expected


def test_cpu_identity_new_child_exit_reuse_and_missing(monkeypatch):
    monkeypatch.setattr(pu, "psutil", _FakePsutil([_record(3, 0, "/bin/codex", 1)]))
    first = pu.scan_activity({"codex": None})
    pct, prev = pu.cpu_percents({}, 0, first, 1)
    assert pct == {}
    monkeypatch.setattr(
        pu,
        "psutil",
        _FakePsutil(
            [_record(3, 0, "/bin/codex", 1.007), _record(4, 3, "/bin/worker", 100)]
        ),
    )
    pct, prev = pu.cpu_percents(prev, 1, pu.scan_activity({"codex": None}), 2)
    assert pct["codex"] == pytest.approx(0.7)
    monkeypatch.setattr(
        pu, "psutil", _FakePsutil([_record(3, 0, "/bin/codex", 300, created=2)])
    )
    assert pu.cpu_percents(prev, 2, pu.scan_activity({"codex": None}), 3)[0] == {}


def test_process_and_descendant_bounds_not_idle(monkeypatch):
    procs = [_record(1, 0, "/bin/claude")] + [
        _record(n, 1, "/bin/worker") for n in range(2, 8300)
    ]
    monkeypatch.setattr(pu, "psutil", _FakePsutil(procs))
    out = pu.scan_activity({"claude": None})
    assert out["claude"]["limited"] and not out["claude"]["complete"]
    assert len(out["claude"]["cpus"]) <= 501


def test_host_and_nested_ownership_independent_of_selected_tools(monkeypatch):
    procs = [
        _record(1, 0, "/sbin/init"),
        _record(2, 1, "/Applications/Visual Studio Code.app/Contents/MacOS/Electron"),
        _record(3, 2, "/bin/claude", 1),
        _record(4, 3, "/bin/codex", 100),
    ]
    monkeypatch.setattr(pu, "psutil", _FakePsutil(procs))
    out = pu.scan_activity({"claude": None})["claude"]
    assert out["roots"][0]["host"] == "vscode"
    assert out["cpu_seconds"] == 1


@pytest.mark.parametrize("parent,created", [(99, 1), (2, 1), (1, 0)])
def test_incomplete_reused_cyclic_ancestry_unknown(monkeypatch, parent, created):
    procs = [
        _record(1, 0, "/sbin/init", created=1),
        _record(2, parent, "/bin/claude", created=created),
    ]
    monkeypatch.setattr(pu, "psutil", _FakePsutil(procs))
    assert pu.scan_activity({"claude": None})["claude"]["roots"][0]["host"] == "unknown"


def test_missing_cpu_unavailable_and_regex_only_basename(monkeypatch):
    p = _record(1, 0, "/bin/claude")
    p.info["cpu_times"] = None
    monkeypatch.setattr(
        pu,
        "psutil",
        _FakePsutil([p, _record(2, 0, "/bin/node", args=("magic-agent",))]),
    )
    out = pu.scan_activity({"claude": None, "custom": re.compile("magic-agent")})
    assert not out["claude"]["complete"] and out["claude"]["errors"]
    assert not out["custom"]["present"]
