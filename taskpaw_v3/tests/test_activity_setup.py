"""T5: install/check/uninstall only in disposable synthetic homes."""

import importlib
import json
import shlex
from pathlib import Path

import pytest


@pytest.fixture
def setup(tmp_path, monkeypatch):
    s = importlib.import_module("taskpaw_v3.integrations.activity_setup")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return s, tmp_path


def run(s, home, action, tool="all", *extra):
    return s.main([action, "--home", str(home), "--tool", tool, *extra])


def target(home, tool):
    return home / f".{tool}" / ("settings.json" if tool == "claude" else "hooks.json")


def put(home, tool, data):
    p = target(home, tool)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data))
    return p


@pytest.mark.parametrize("existing", [False, True])
def test_install_idempotent_check_exact_uninstall(setup, existing):
    s, home = setup
    original = b'{"unrelated": 1, "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "do-not-run"}]}]}}'
    if existing:
        for tool in ("claude", "codex"):
            p = target(home, tool)
            p.parent.mkdir()
            p.write_bytes(original)
    assert run(s, home, "install") == 0
    before = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    assert run(s, home, "install") == 0
    assert before == {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    assert run(s, home, "check") == 0
    assert not list((home / ".taskpaw").glob("agent-activity*"))
    for tool in ("claude", "codex"):
        hooks = json.loads(target(home, tool).read_text())["hooks"]
        assert ("SubagentStop" in hooks) is (tool == "codex")
    assert run(s, home, "uninstall") == 0
    for tool in ("claude", "codex"):
        assert (
            target(home, tool).read_bytes() == original
            if existing
            else not target(home, tool).exists()
        )
    assert run(s, home, "uninstall") == 0
    assert run(s, home, "install") == 0


@pytest.mark.parametrize(
    "command,owned",
    [
        ("python writer --taskpaw-hook-id taskpaw-ai-activity-v1-claude", True),
        ("python edited --taskpaw-hook-id 'taskpaw-ai-activity-v1-claude'", True),
        ("python writer --taskpaw-hook-id taskpaw-ai-activity-v1-claude-extra", False),
        ("python writer --taskpaw-hook-id taskpaw-ai-activity-v1-codex", False),
        ("echo '--taskpaw-hook-id taskpaw-ai-activity-v1-claude'", False),
        ("python activity_writer.py", False),
    ],
)
def test_exact_marker_ownership(setup, command, owned):
    s, _ = setup
    assert s.owned({"type": "command", "command": command}, "claude") is owned


def test_selective_uninstall_preserves_later_edits_and_unowned_groups(setup):
    s, home = setup
    p = put(home, "claude", {"hooks": {"Stop": [{"matcher": "old", "hooks": []}]}})
    assert run(s, home, "install", "claude") == 0
    data = json.loads(p.read_text())
    data["later"] = True
    data["hooks"]["Stop"].append(
        {"matcher": "new", "hooks": [{"type": "command", "command": "unrelated"}]}
    )
    p.write_text(json.dumps(data))
    assert run(s, home, "uninstall", "claude") == 0
    result = json.loads(p.read_text())
    assert result["later"] and result["hooks"]["Stop"] == [
        {"matcher": "old", "hooks": []},
        {"matcher": "new", "hooks": [{"type": "command", "command": "unrelated"}]},
    ]


def test_missing_record_never_removes_empty_groups(setup):
    s, home = setup
    assert run(s, home, "install", "claude") == 0
    (home / ".taskpaw/hook-setup/claude.json").unlink()
    assert run(s, home, "uninstall", "claude") == 0
    assert json.loads(target(home, "claude").read_text())["hooks"]["Stop"] == [
        {"hooks": []}
    ]


@pytest.mark.parametrize(
    "bad", ["{", '{"hooks":{},"hooks":{}}', '{"hooks":[]}', '{"hooks":{"Stop":[{}]}}']
)
def test_malformed_settings_never_replaced(setup, bad):
    s, home = setup
    p = put(home, "claude", {})
    p.write_text(bad)
    assert run(s, home, "install", "claude") == 1
    assert p.read_text() == bad


def test_windows_codex_all_preflight_no_edits(setup, monkeypatch, capsys):
    s, home = setup
    monkeypatch.setattr(s, "WINDOWS", True)
    assert run(s, home, "install") == 1
    assert "Windows Codex hook dispatch not verified" in capsys.readouterr().out
    assert not target(home, "claude").exists()


def test_quoted_paths_literal_and_windows_slashes(setup):
    s, _ = setup
    command = s.render_command(
        r"C:\Program Files\Python\python.exe",
        r"D:\代码 $x\writer.py",
        "claude",
        r"C:\My Home\state.json",
        windows=True,
    )
    tokens = shlex.split(command)
    assert tokens[:2] == ["C:/Program Files/Python/python.exe", "D:/代码 $x/writer.py"]
    assert "\\" not in command


def test_check_rejects_edited_owned_command_without_execution(setup):
    s, home = setup
    assert run(s, home, "install", "claude") == 0
    p = target(home, "claude")
    data = json.loads(p.read_text())
    data["hooks"]["Stop"][0]["hooks"][0]["command"] += " ; touch INJECTION_SENTINEL"
    p.write_text(json.dumps(data))
    assert run(s, home, "check", "claude") == 1
    assert not (home / "INJECTION_SENTINEL").exists()


def test_backup_failure_and_hash_conflict_prevent_edit(setup, monkeypatch):
    s, home = setup
    p = put(home, "claude", {"keep": True})
    original = p.read_bytes()
    monkeypatch.setattr(
        s, "backup", lambda *a: (_ for _ in ()).throw(OSError("synthetic"))
    )
    assert run(s, home, "install", "claude") == 1
    assert p.read_bytes() == original


@pytest.mark.parametrize("linked_directory", [False, True])
def test_settings_symlink_refused(setup, linked_directory):
    s, home = setup
    other = home / "other.json"
    other.write_text("{}")
    p = target(home, "claude")
    if linked_directory:
        directory = home / "dotfiles"
        directory.mkdir()
        p.parent.symlink_to(directory, target_is_directory=True)
    else:
        p.parent.mkdir()
    p.symlink_to(other)
    assert run(s, home, "install", "claude") == 1
    assert other.read_text() == "{}"
    assert p.is_symlink()


@pytest.mark.parametrize("linked_directory", [False, True])
def test_setup_through_symlinked_home_or_tool_directory(setup, linked_directory):
    s, home = setup
    real_home = home / "real-home"
    real_home.mkdir()
    if linked_directory:
        for tool in ("claude", "codex"):
            directory = real_home / f".{tool}"
            directory.mkdir()
            (home / f".{tool}").symlink_to(directory, target_is_directory=True)
    else:
        alias = home / "home-alias"
        alias.symlink_to(real_home, target_is_directory=True)
        home = alias
    original = b'{"unrelated": true}'
    for tool in ("claude", "codex"):
        path = target(home, tool)
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(original)
    assert run(s, home, "install") == 0
    assert run(s, home, "check") == 0
    assert run(s, home, "uninstall") == 0
    for tool in ("claude", "codex"):
        assert target(home, tool).read_bytes() == original


def test_missing_executables_and_usage_codes(setup):
    s, home = setup
    assert run(s, home, "install", "claude", "--python", str(home / "absent")) == 1
    with pytest.raises(SystemExit) as e:
        s.main(["nonsense"])
    assert e.value.code == 2


def test_hash_conflict_preserves_concurrent_settings(setup, monkeypatch):
    s, home = setup
    p = put(home, "claude", {"baseline": 1})
    original_fsync = s.os.fsync

    def conflict(fd):
        original_fsync(fd)
        if list(p.parent.glob(".taskpaw-*.tmp")):
            p.write_text('{"concurrent":true}')

    monkeypatch.setattr(s.os, "fsync", conflict)
    assert run(s, home, "install", "claude") == 1
    assert json.loads(p.read_text()) == {"concurrent": True}
    backups = list((home / ".taskpaw/hook-setup/backups").glob("*.bak"))
    assert len(backups) == 1 and json.loads(backups[0].read_text()) == {"baseline": 1}


@pytest.mark.parametrize("failure", ["replace", "record"])
def test_write_failures_report_and_keep_backup(setup, monkeypatch, capsys, failure):
    s, home = setup
    p = put(home, "claude", {"baseline": 1})
    original = s.atomic_write

    def fail(path, raw, expected):
        if (failure == "replace" and path == p) or (
            failure == "record" and path.name == "claude.json"
        ):
            raise OSError("PRIVATE DETAIL")
        original(path, raw, expected)

    monkeypatch.setattr(s, "atomic_write", fail)
    assert run(s, home, "install", "claude") == 1
    output = capsys.readouterr().out
    assert "backup retained" in output and "PRIVATE DETAIL" not in output
    assert list((home / ".taskpaw/hook-setup/backups").glob("*.bak"))


@pytest.mark.parametrize(
    "fault", ["state", "nonce", "stale", "timeout", "missing", "nonfinite", "exit"]
)
def test_check_rejects_bad_writer_output_and_cleans_temp(setup, monkeypatch, fault):
    s, home = setup
    assert run(s, home, "install", "claude") == 0

    def fake_run(argv, **kwargs):
        import time
        from types import SimpleNamespace

        words = shlex.split(argv[-1])
        output = Path(words[words.index("--path") + 1])
        event = json.loads(kwargs["input"])
        if fault == "timeout":
            raise s.subprocess.TimeoutExpired(argv, 5)
        if fault != "missing":
            output.write_text(
                json.dumps(
                    {
                        "tool": "claude",
                        "state": "wrong" if fault == "state" else "busy",
                        "session": "wrong" if fault == "nonce" else event["session_id"],
                        "ts": 0
                        if fault == "stale"
                        else float("nan")
                        if fault == "nonfinite"
                        else time.time(),
                    }
                )
            )
        return SimpleNamespace(returncode=1 if fault == "exit" else 0)

    monkeypatch.setattr(s.subprocess, "run", fake_run)
    assert run(s, home, "check", "claude") == 1
    assert not list((home / ".taskpaw").glob(".activity-check-*"))
    assert not list((home / ".taskpaw").glob("agent-activity*"))


def test_literal_metacharacter_paths_execute_without_injection(setup):
    s, home = setup
    directory = home / "目录 ' space $(touch INJECTION_SENTINEL)"
    directory.mkdir()
    writer = directory / "writer.py"
    writer.write_bytes(s.WRITER.read_bytes())
    assert run(s, home, "install", "claude", "--writer", str(writer)) == 0
    assert run(s, home, "check", "claude") == 0
    assert not (Path.cwd() / "INJECTION_SENTINEL").exists()
    assert not (home / "INJECTION_SENTINEL").exists()


def test_backup_precedes_replace_and_permissions(setup, monkeypatch):
    s, home = setup
    p = put(home, "claude", {"baseline": 1})
    p.chmod(0o640)
    replace = s.os.replace
    seen = []

    def guarded(src, dst):
        if dst == p:
            files = list((home / ".taskpaw/hook-setup/backups").glob("*.bak"))
            assert len(files) == 1
            seen.append(files[0])
        replace(src, dst)

    monkeypatch.setattr(s.os, "replace", guarded)
    assert run(s, home, "install", "claude") == 0
    assert seen and p.stat().st_mode & 0o777 == 0o640
    assert seen[0].stat().st_mode & 0o777 == 0o600


def test_preflight_all_malformed_second_tool_never_edits_first(setup):
    s, home = setup
    p = put(home, "codex", {})
    p.write_text("{")
    assert run(s, home, "install") == 1
    assert not target(home, "claude").exists()


def test_partial_io_failure_reports_both_tools(setup, monkeypatch, capsys):
    s, home = setup
    write = s.atomic_write

    def fail(path, raw, expected):
        if path == target(home, "codex"):
            raise OSError("private")
        write(path, raw, expected)

    monkeypatch.setattr(s, "atomic_write", fail)
    assert run(s, home, "install") == 1
    out = capsys.readouterr().out
    assert "claude: install succeeded" in out and "codex: local I/O failed" in out
    assert target(home, "claude").exists() and not target(home, "codex").exists()


def test_changed_marker_handler_removed_unmarked_preserved(setup):
    s, home = setup
    assert run(s, home, "install", "claude") == 0
    p = target(home, "claude")
    data = json.loads(p.read_text())
    data["hooks"]["Stop"][0]["hooks"][0]["command"] = (
        "edited --taskpaw-hook-id taskpaw-ai-activity-v1-claude"
    )
    unmarked = data["hooks"]["SessionEnd"][0]["hooks"][0]
    unmarked["command"] = "unmarked edited command"
    data["later"] = True
    p.write_text(json.dumps(data))
    assert run(s, home, "uninstall", "claude") == 0
    data = json.loads(p.read_text())
    assert data["hooks"]["Stop"] == []
    assert data["hooks"]["SessionEnd"][0]["hooks"] == [unmarked]


def test_missing_shell_and_nondefault_codex_home_refused(setup, monkeypatch):
    s, home = setup
    monkeypatch.setenv("CODEX_HOME", str(home / "custom"))
    assert run(s, home, "install", "codex") == 1
    monkeypatch.setattr(s.shutil, "which", lambda *a: None)
    assert run(s, home, "install", "claude") == 1
    assert not target(home, "claude").exists()
