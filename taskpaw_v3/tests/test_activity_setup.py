"""T5: install/check/uninstall only in disposable synthetic homes."""

import importlib
import json
import ntpath
import shlex
import sys
from pathlib import Path, PureWindowsPath

import pytest


@pytest.fixture
def setup(tmp_path, monkeypatch, request):
    s = importlib.import_module("taskpaw_v3.integrations.activity_setup")
    # Generic reconciliation/undo tests use the POSIX tool policy (both tools).
    # Only shell transport is replaced on Windows; verify_writer still validates
    # argv, runs the real Python writer and checks its nonce/state/timestamp.
    monkeypatch.setattr(s, "WINDOWS", False)
    if getattr(request, "param", sys.platform) == "win32":
        native_run = s.subprocess.run
        native_which = s.shutil.which

        def direct_writer(argv, **kwargs):
            assert argv[1] == "-c"
            words = shlex.split(argv[2])
            assert words[0] == "exec"
            return native_run(words[1:], **kwargs)

        monkeypatch.setattr(s.subprocess, "run", direct_writer)
        monkeypatch.setattr(
            s.shutil,
            "which",
            lambda name: (
                "synthetic-shell" if name in {"bash", "sh"} else native_which(name)
            ),
        )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return s, tmp_path


@pytest.fixture
def run(capsys):
    def invoke(s, home, action, tool="all", *extra, expected=0):
        code = s.main([action, "--home", str(home), "--tool", tool, *extra])
        if code != expected:
            captured = capsys.readouterr()
            pytest.fail(
                f"{action} {tool}: exit {code}, expected {expected}\n"
                f"stdout:\n{captured.out}\nstderr:\n{captured.err}"
            )

    return invoke


def target(home, tool):
    return home / f".{tool}" / ("settings.json" if tool == "claude" else "hooks.json")


def put(home, tool, data):
    p = target(home, tool)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data))
    return p


@pytest.mark.parametrize("existing", [False, True])
def test_install_idempotent_check_exact_uninstall(setup, existing, run):
    s, home = setup
    original = b'{"unrelated": 1, "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "do-not-run"}]}]}}'
    if existing:
        for tool in ("claude", "codex"):
            p = target(home, tool)
            p.parent.mkdir()
            p.write_bytes(original)
    run(s, home, "install")
    before = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    run(s, home, "install")
    assert before == {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    run(s, home, "check")
    assert not list((home / ".taskpaw").glob("agent-activity*"))
    for tool in ("claude", "codex"):
        hooks = json.loads(target(home, tool).read_text())["hooks"]
        assert "SubagentStart" in hooks and "SubagentStop" in hooks
        if tool == "claude":
            assert hooks["Notification"][0]["matcher"] == "permission_prompt"
    run(s, home, "uninstall")
    for tool in ("claude", "codex"):
        assert (
            target(home, tool).read_bytes() == original
            if existing
            else not target(home, tool).exists()
        )
    run(s, home, "uninstall")
    run(s, home, "install")


def test_reinstall_reconciles_legacy_notification_matcher(setup, monkeypatch, run):
    s, home = setup
    required = s.required

    def legacy_required(tool, command):
        groups = required(tool, command)
        groups["Notification"]["matcher"] = "permission_prompt|idle_prompt"
        return groups

    with monkeypatch.context() as legacy:
        legacy.setattr(s, "required", legacy_required)
        run(s, home, "install", "claude")
    p = target(home, "claude")
    data = json.loads(p.read_text())
    unrelated = {"type": "command", "command": "user-notification-handler"}
    data["hooks"]["Notification"][0]["hooks"].append(unrelated)
    p.write_text(json.dumps(data))
    run(s, home, "check", "claude", expected=1)
    run(s, home, "install", "claude")
    run(s, home, "check", "claude")
    groups = json.loads(p.read_text())["hooks"]["Notification"]
    assert groups[0] == {
        "matcher": "permission_prompt|idle_prompt",
        "hooks": [unrelated],
    }
    assert len(groups) == 2
    assert groups[1]["matcher"] == "permission_prompt"
    assert len(groups[1]["hooks"]) == 1
    assert s.owned(groups[1]["hooks"][0], "claude")
    before = p.read_bytes()
    run(s, home, "install", "claude")
    assert p.read_bytes() == before


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


def test_selective_uninstall_preserves_later_edits_and_unowned_groups(setup, run):
    s, home = setup
    p = put(home, "claude", {"hooks": {"Stop": [{"matcher": "old", "hooks": []}]}})
    run(s, home, "install", "claude")
    data = json.loads(p.read_text())
    data["later"] = True
    data["hooks"]["Stop"].append(
        {"matcher": "new", "hooks": [{"type": "command", "command": "unrelated"}]}
    )
    p.write_text(json.dumps(data))
    run(s, home, "uninstall", "claude")
    result = json.loads(p.read_text())
    assert result["later"] and result["hooks"]["Stop"] == [
        {"matcher": "old", "hooks": []},
        {"hooks": []},
        {"matcher": "new", "hooks": [{"type": "command", "command": "unrelated"}]},
    ]


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("tool", ["claude", "codex"])
@pytest.mark.parametrize(
    "changed_path",
    [
        "writer",
        pytest.param(
            "python-symlink",
            marks=pytest.mark.skipif(
                sys.platform == "win32",
                reason="Executes Python via an extensionless POSIX symlink; Windows symlinks also require privileges",
            ),
        ),
    ],
)
def test_reinstall_after_external_edit_revokes_whole_file_restore(
    setup, existing, tool, changed_path, run
):
    s, home = setup
    if existing:
        put(home, tool, {"baseline": True})
    run(s, home, "install", tool)
    p = target(home, tool)
    data = json.loads(p.read_text())
    data["external"] = True
    handler = {"type": "command", "command": "user-hook-never-executed"}
    data["hooks"]["Stop"][0]["hooks"].append(handler)
    p.write_text(json.dumps(data))
    if changed_path == "python-symlink":
        replacement = home / "python-alias"
        replacement.symlink_to(s.sys.executable)
        option = "--python"
    else:
        replacement = home / "writer-copy.py"
        replacement.write_bytes(s.WRITER.read_bytes())
        option = "--writer"
    run(s, home, "install", tool, option, str(replacement))
    record = json.loads((home / f".taskpaw/hook-setup/{tool}.json").read_text())
    assert not {"original_exists", "baseline", "baseline_hash"} & record.keys()
    # A subsequent update must not regain whole-file restoration authority.
    run(s, home, "install", tool)
    run(s, home, "uninstall", tool)
    result = json.loads(p.read_text())
    assert result["external"]
    assert result.get("baseline", False) is existing
    handlers = [
        h for groups in result["hooks"].values() for g in groups for h in g["hooks"]
    ]
    assert handlers == [handler]


def test_missing_record_never_removes_empty_groups(setup, run):
    s, home = setup
    run(s, home, "install", "claude")
    (home / ".taskpaw/hook-setup/claude.json").unlink()
    run(s, home, "uninstall", "claude")
    assert json.loads(target(home, "claude").read_text())["hooks"]["Stop"] == [
        {"hooks": []}
    ]


@pytest.mark.parametrize(
    "bad", ["{", '{"hooks":{},"hooks":{}}', '{"hooks":[]}', '{"hooks":{"Stop":[{}]}}']
)
def test_malformed_settings_never_replaced(setup, bad, run):
    s, home = setup
    p = put(home, "claude", {})
    p.write_text(bad)
    run(s, home, "install", "claude", expected=1)
    assert p.read_text() == bad


def test_windows_codex_all_preflight_no_edits(setup, monkeypatch, capsys, run):
    s, home = setup
    monkeypatch.setattr(s, "WINDOWS", True)
    bash = home / "Git/bin/bash.exe"
    bash.parent.mkdir(parents=True)
    bash.touch()
    monkeypatch.setenv("CLAUDE_CODE_GIT_BASH_PATH", str(bash))
    # Shell dispatch is irrelevant: all-tool preflight must refuse Codex before
    # writing settings even when Claude's writer has successfully verified.
    monkeypatch.setattr(s, "verify_writer", lambda *args: None)
    run(s, home, "install", expected=1)
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


def test_check_rejects_edited_owned_command_without_execution(setup, run):
    s, home = setup
    run(s, home, "install", "claude")
    p = target(home, "claude")
    data = json.loads(p.read_text())
    data["hooks"]["Stop"][0]["hooks"][0]["command"] += " ; touch INJECTION_SENTINEL"
    p.write_text(json.dumps(data))
    run(s, home, "check", "claude", expected=1)
    assert not (home / "INJECTION_SENTINEL").exists()


def test_backup_failure_and_hash_conflict_prevent_edit(setup, monkeypatch, run):
    s, home = setup
    p = put(home, "claude", {"keep": True})
    original = p.read_bytes()
    monkeypatch.setattr(
        s, "backup", lambda *a: (_ for _ in ()).throw(OSError("synthetic"))
    )
    run(s, home, "install", "claude", expected=1)
    assert p.read_bytes() == original


@pytest.mark.parametrize("linked_directory", [False, True])
@pytest.mark.skipif(
    sys.platform == "win32", reason="Creating real symlinks requires Windows privileges"
)
def test_settings_symlink_refused(setup, linked_directory, run):
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
    run(s, home, "install", "claude", expected=1)
    assert other.read_text() == "{}"
    assert p.is_symlink()


@pytest.mark.parametrize("linked_directory", [False, True])
@pytest.mark.skipif(
    sys.platform == "win32", reason="Creating real symlinks requires Windows privileges"
)
def test_setup_through_symlinked_home_or_tool_directory(setup, linked_directory, run):
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
    run(s, home, "install")
    run(s, home, "check")
    run(s, home, "uninstall")
    for tool in ("claude", "codex"):
        assert target(home, tool).read_bytes() == original


def test_missing_executables_and_usage_codes(setup, run):
    s, home = setup
    run(s, home, "install", "claude", "--python", str(home / "absent"), expected=1)
    with pytest.raises(SystemExit) as e:
        s.main(["nonsense"])
    assert e.value.code == 2


def test_hash_conflict_preserves_concurrent_settings(setup, monkeypatch, run):
    s, home = setup
    p = put(home, "claude", {"baseline": 1})
    original_fsync = s.os.fsync

    def conflict(fd):
        original_fsync(fd)
        if list(p.parent.glob(".taskpaw-*.tmp")):
            p.write_text('{"concurrent":true}')

    monkeypatch.setattr(s.os, "fsync", conflict)
    run(s, home, "install", "claude", expected=1)
    assert json.loads(p.read_text()) == {"concurrent": True}
    backups = list((home / ".taskpaw/hook-setup/backups").glob("*.bak"))
    assert len(backups) == 1 and json.loads(backups[0].read_text()) == {"baseline": 1}


@pytest.mark.parametrize("failure", ["replace", "record"])
def test_write_failures_report_and_keep_backup(
    setup, monkeypatch, capsys, failure, run
):
    s, home = setup
    p = put(home, "claude", {"baseline": 1})
    original = s.atomic_write

    def fail(path, raw, expected):
        if (failure == "replace" and path == p) or (
            failure == "record" and path.name == "claude.json"
        ):
            error = PermissionError(13, "PRIVATE DETAIL", "PRIVATE_FILENAME")
            error.winerror = 32
            raise error
        original(path, raw, expected)

    monkeypatch.setattr(s, "atomic_write", fail)
    run(s, home, "install", "claude", expected=1)
    output = capsys.readouterr().out
    assert "backup retained" in output and "PRIVATE DETAIL" not in output
    assert "PRIVATE_FILENAME" not in output
    stage = "settings_write" if failure == "replace" else "undo_write"
    assert f"stage={stage}" in output
    assert "exception=PermissionError" in output and "errno=13" in output
    assert "winerror=32" in output
    assert list((home / ".taskpaw/hook-setup/backups").glob("*.bak"))


@pytest.mark.parametrize(
    "fault", ["state", "nonce", "stale", "timeout", "missing", "nonfinite", "exit"]
)
def test_check_rejects_bad_writer_output_and_cleans_temp(
    setup, monkeypatch, fault, run
):
    s, home = setup
    run(s, home, "install", "claude")

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
    run(s, home, "check", "claude", expected=1)
    assert not list((home / ".taskpaw").glob(".activity-check-*"))
    assert not list((home / ".taskpaw").glob("agent-activity*"))


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Executes the generated command through real POSIX bash",
)
@pytest.mark.parametrize("setup", ["posix"], indirect=True)
def test_literal_metacharacter_paths_execute_without_injection(setup, run):
    s, home = setup
    directory = home / "目录 ' space $(touch INJECTION_SENTINEL)"
    directory.mkdir()
    writer = directory / "writer.py"
    writer.write_bytes(s.WRITER.read_bytes())
    run(s, home, "install", "claude", "--writer", str(writer))
    run(s, home, "check", "claude")
    assert not (Path.cwd() / "INJECTION_SENTINEL").exists()
    assert not (home / "INJECTION_SENTINEL").exists()


def test_backup_precedes_replace(setup, monkeypatch, run):
    s, home = setup
    p = put(home, "claude", {"baseline": 1})
    replace = s.os.replace
    seen = []

    def guarded(src, dst):
        if dst == p:
            files = list((home / ".taskpaw/hook-setup/backups").glob("*.bak"))
            assert len(files) == 1
            seen.append(files[0])
        replace(src, dst)

    monkeypatch.setattr(s.os, "replace", guarded)
    run(s, home, "install", "claude")
    assert seen and json.loads(seen[0].read_text()) == {"baseline": 1}


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows chmod does not implement POSIX permission bits",
)
def test_install_preserves_posix_permissions(setup, run):
    s, home = setup
    p = put(home, "claude", {"baseline": 1})
    p.chmod(0o640)
    run(s, home, "install", "claude")
    assert p.stat().st_mode & 0o777 == 0o640
    backups = list((home / ".taskpaw/hook-setup/backups").glob("*.bak"))
    assert len(backups) == 1 and backups[0].stat().st_mode & 0o777 == 0o600


def test_preflight_all_malformed_second_tool_never_edits_first(setup, run):
    s, home = setup
    p = put(home, "codex", {})
    p.write_text("{")
    run(s, home, "install", expected=1)
    assert not target(home, "claude").exists()


def test_partial_io_failure_reports_both_tools(setup, monkeypatch, capsys, run):
    s, home = setup
    write = s.atomic_write

    def fail(path, raw, expected):
        if path == target(home, "codex"):
            raise OSError("private")
        write(path, raw, expected)

    monkeypatch.setattr(s, "atomic_write", fail)
    run(s, home, "install", expected=1)
    out = capsys.readouterr().out
    assert "claude: install succeeded" in out and "codex: local I/O failed" in out
    assert target(home, "claude").exists() and not target(home, "codex").exists()


def test_changed_marker_handler_removed_unmarked_preserved(setup, run):
    s, home = setup
    run(s, home, "install", "claude")
    p = target(home, "claude")
    data = json.loads(p.read_text())
    data["hooks"]["Stop"][0]["hooks"][0]["command"] = (
        "edited --taskpaw-hook-id taskpaw-ai-activity-v1-claude"
    )
    unmarked = data["hooks"]["SessionEnd"][0]["hooks"][0]
    unmarked["command"] = "unmarked edited command"
    data["later"] = True
    p.write_text(json.dumps(data))
    run(s, home, "uninstall", "claude")
    data = json.loads(p.read_text())
    assert data["hooks"]["Stop"] == [{"hooks": []}]
    assert data["hooks"]["SessionEnd"][0]["hooks"] == [unmarked]


def test_missing_shell_and_nondefault_codex_home_refused(setup, monkeypatch, run):
    s, home = setup
    monkeypatch.setenv("CODEX_HOME", str(home / "custom"))
    run(s, home, "install", "codex", expected=1)
    monkeypatch.setattr(s.shutil, "which", lambda *a: None)
    run(s, home, "install", "claude", expected=1)
    assert not target(home, "claude").exists()


@pytest.mark.parametrize("relative", ["bin/bash.exe", "usr/bin/bash.exe"])
@pytest.mark.parametrize(
    "location", ["git_path", "program_files", "local_app_data", "env"]
)
def test_windows_git_bash_discovery(setup, monkeypatch, relative, location):
    s, home = setup
    monkeypatch.setattr(s, "WINDOWS", True)
    for key in ("CLAUDE_CODE_GIT_BASH_PATH", "ProgramFiles", "LOCALAPPDATA"):
        monkeypatch.delenv(key, raising=False)
    root = home / "Git"
    if location == "local_app_data":
        root = home / "Programs/Git"
    bash = root / relative
    bash.parent.mkdir(parents=True)
    bash.touch()
    calls = []

    def which(name):
        calls.append(name)
        return str(root / "cmd/git.exe") if location == "git_path" else None

    monkeypatch.setattr(s.shutil, "which", which)
    if location == "program_files":
        monkeypatch.setenv("ProgramFiles", str(home))
    elif location == "local_app_data":
        monkeypatch.setenv("LOCALAPPDATA", str(home))
    elif location == "env":
        monkeypatch.setenv("CLAUDE_CODE_GIT_BASH_PATH", str(bash))
    assert s.shell_for("claude") == str(bash)
    assert calls == ([] if location == "env" else ["git.exe"])


@pytest.mark.parametrize("source", ["env", "path", "default"])
def test_windows_system32_bash_rejected(setup, monkeypatch, source):
    s, home = setup
    monkeypatch.setattr(s, "WINDOWS", True)
    for key in ("CLAUDE_CODE_GIT_BASH_PATH", "ProgramFiles", "LOCALAPPDATA"):
        monkeypatch.delenv(key, raising=False)
    root = home / "Windows/sYsTeM32"
    bash = root / ("Git/bin/bash.exe" if source == "default" else "bash.exe")
    bash.parent.mkdir(parents=True)
    bash.touch()
    monkeypatch.setattr(
        s.shutil, "which", lambda name: str(bash) if name == "bash" else None
    )
    if source == "env":
        monkeypatch.setenv("CLAUDE_CODE_GIT_BASH_PATH", str(bash))
    elif source == "default":
        monkeypatch.setenv("ProgramFiles", str(root))
    with pytest.raises(s.SetupError, match="Git Bash missing"):
        s.shell_for("claude")


def test_windows_git_bash_env_takes_precedence(setup, monkeypatch):
    s, home = setup
    monkeypatch.setattr(s, "WINDOWS", True)
    monkeypatch.setenv("CLAUDE_CODE_GIT_BASH_PATH", str(home / "missing/bash.exe"))
    bash = home / "Git/bin/bash.exe"
    bash.parent.mkdir(parents=True)
    bash.touch()
    monkeypatch.setenv("ProgramFiles", str(home))
    with pytest.raises(s.SetupError, match="Git Bash missing"):
        s.shell_for("claude")


@pytest.mark.parametrize("git_directory", ["cmd", "bin"])
@pytest.mark.parametrize("relative", ["bin/bash.exe", "usr/bin/bash.exe"])
def test_windows_git_bash_drive_path_resolution(
    setup, monkeypatch, git_directory, relative
):
    s, _ = setup
    monkeypatch.setattr(s, "WINDOWS", True)
    for key in ("CLAUDE_CODE_GIT_BASH_PATH", "ProgramFiles", "LOCALAPPDATA"):
        monkeypatch.delenv(key, raising=False)
    root = r"C:\Program Files\Git"
    git = ntpath.join(root, git_directory, "git.exe")
    expected = ntpath.normpath(ntpath.join(root, relative))
    checked = []

    class SyntheticWindowsPath(PureWindowsPath):
        def resolve(self):
            return self

        def is_file(self):
            checked.append(str(self))
            return str(self) == expected

    # Real Windows lexical rules, with file existence supplied independently.
    monkeypatch.setattr(s, "Path", SyntheticWindowsPath)
    monkeypatch.setattr(
        s.shutil, "which", lambda name: git if name == "git.exe" else None
    )
    assert s.shell_for("claude") == expected
    assert checked[-1] == expected
    assert all(ntpath.isabs(path) for path in checked)


@pytest.mark.parametrize("setup", ["win32"], indirect=True)
def test_windows_claude_install_check_uninstall(setup, monkeypatch, run):
    s, home = setup
    monkeypatch.setattr(s, "WINDOWS", True)
    bash = home / "Program Files/Git/bin/bash.exe"
    bash.parent.mkdir(parents=True)
    bash.touch()
    monkeypatch.setenv("CLAUDE_CODE_GIT_BASH_PATH", str(bash))
    # The fixture replaces only shell transport, retaining the real writer and
    # Windows command rendering plus the complete install/check/undo lifecycle.
    run(s, home, "install", "claude")
    record = json.loads((home / ".taskpaw/hook-setup/claude.json").read_text())
    words = shlex.split(record["command"])
    assert words[0] == str(Path(s.sys.executable).absolute()).replace("\\", "/")
    assert "\\" not in record["command"]
    run(s, home, "check", "claude")
    run(s, home, "uninstall", "claude")
    assert not target(home, "claude").exists()


def test_i216_reinstall_repairs_lost_record_without_false_baseline(setup, run):
    s, home = setup
    run(s, home, "install", "claude")
    path = target(home, "claude")
    record_path = home / ".taskpaw/hook-setup/claude.json"
    record_path.unlink()
    before = path.read_bytes()
    backups = list((home / ".taskpaw/hook-setup/backups").glob("*.bak"))
    run(s, home, "install", "claude")
    assert record_path.exists()
    record = json.loads(record_path.read_text())
    assert not {"original_exists", "baseline", "baseline_hash"} & record.keys()
    assert path.read_bytes() == before
    assert list((home / ".taskpaw/hook-setup/backups").glob("*.bak")) == backups
    run(s, home, "uninstall", "claude")
    assert not any(
        s.owned(h, "claude")
        for groups in json.loads(path.read_text())["hooks"].values()
        for group in groups
        for h in group["hooks"]
    )


@pytest.mark.parametrize("fault", ["missing", "changed", "denied"])
def test_i216_missing_baseline_uses_selective_uninstall(
    setup, run, monkeypatch, capsys, fault
):
    s, home = setup
    path = put(home, "claude", {"keep": True})
    run(s, home, "install", "claude")
    record = json.loads((home / ".taskpaw/hook-setup/claude.json").read_text())
    baseline = Path(record["baseline"])
    if fault == "missing":
        baseline.unlink()
    elif fault == "changed":
        baseline.write_text("PRIVATE CORRUPT")
    else:
        original = s.read_bytes

        def denied(p):
            if p == baseline:
                raise PermissionError("PRIVATE DENIED")
            return original(p)

        monkeypatch.setattr(s, "read_bytes", denied)
    run(s, home, "uninstall", "claude")
    data = json.loads(path.read_text())
    assert data["keep"] is True
    assert not any(
        s.owned(h, "claude")
        for groups in data["hooks"].values()
        for group in groups
        for h in group["hooks"]
    )
    output = capsys.readouterr().out
    assert "selective" in output and "PRIVATE" not in output
    assert not (home / ".taskpaw/hook-setup/claude.json").exists()


def test_i216_selective_cleanup_preserves_shifted_empty_user_group(setup, run):
    s, home = setup
    path = put(home, "claude", {"hooks": {"Stop": [{"hooks": []}]}})
    run(s, home, "install", "claude")
    data = json.loads(path.read_text())
    # Shift the preexisting empty group into the recorded created group's index.
    data["hooks"]["Stop"].insert(
        0, {"hooks": [{"type": "command", "command": "user-never-run"}]}
    )
    data["user_edit"] = True
    path.write_text(json.dumps(data))
    run(s, home, "uninstall", "claude")
    groups = json.loads(path.read_text())["hooks"]["Stop"]
    assert groups[1] == {"hooks": []}
    assert len(groups) == 3


def test_preflight_io_diagnostic_preserves_settings(setup, monkeypatch, capsys, run):
    s, home = setup
    path = put(home, "claude", {"keep": True})
    before = path.read_bytes()

    def refused(*args):
        raise PermissionError(13, "PRIVATE_MESSAGE --token=SECRET", "PRIVATE_FILENAME")

    monkeypatch.setattr(s, "read_bytes", refused)
    run(s, home, "install", "claude", expected=1)
    output = capsys.readouterr().out
    assert "claude: local I/O failed" in output
    assert "stage=preflight_settings_read" in output
    assert "exception=PermissionError" in output and "errno=13" in output
    assert all(
        word not in output for word in ("PRIVATE_MESSAGE", "SECRET", "PRIVATE_FILENAME")
    )
    assert path.read_bytes() == before and not (home / ".taskpaw").exists()


@pytest.mark.parametrize("fault", ["facts", "cleanup"])
def test_final_verify_io_diagnostic_distinguishes_cleanup(
    setup, monkeypatch, capsys, run, fault
):
    import sqlite3
    import time
    from types import SimpleNamespace

    from taskpaw_v3.integrations.activity_writer import ActivityStoreError

    s, home = setup
    put(home, "claude", {"keep": True})
    original_verify = s.verify_writer
    calls = 0

    def checked(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            return  # Preflight succeeds; exercise the actual postbackup main catch.
        return original_verify(*args)

    def writer(argv, **kwargs):
        assert kwargs["timeout"] == 5
        words = shlex.split(argv[-1])
        output = Path(words[words.index("--path") + 1])
        event = json.loads(kwargs["input"])
        state = {
            "UserPromptSubmit": "busy",
            "PermissionRequest": "waiting",
            "Stop": "idle",
        }
        output.write_text(
            json.dumps(
                {
                    "tool": "claude",
                    "state": state[event["hook_event_name"]],
                    "session": event["session_id"],
                    "ts": time.time(),
                }
            )
        )
        return SimpleNamespace(returncode=0)

    def facts(*args):
        if fault == "facts":
            try:
                error = sqlite3.OperationalError("PRIVATE_SQL SECRET")
                error.sqlite_errorcode = 5
                raise error
            except sqlite3.Error:
                raise ActivityStoreError("PRIVATE_STORE") from None
        return {}

    monkeypatch.setattr(s, "verify_writer", checked)
    monkeypatch.setattr(s.subprocess, "run", writer)
    monkeypatch.setattr(s, "read_facts", facts)
    monkeypatch.setattr(s, "_projection_matches", lambda *args: True)
    if fault == "cleanup":
        remove = s.tempfile.TemporaryDirectory._rmtree

        def cleanup(cls, *args, **kwargs):
            remove(*args, **kwargs)
            raise PermissionError(13, "PRIVATE_CLEANUP SECRET", "PRIVATE_FILENAME")

        monkeypatch.setattr(
            s.tempfile.TemporaryDirectory, "_rmtree", classmethod(cleanup)
        )
    run(s, home, "install", "claude", expected=1)
    output = capsys.readouterr().out
    assert "stage=final_writer_verify" in output and "backup retained" in output
    if fault == "facts":
        assert "operation=verify_fact_read" in output
        assert "exception=ActivityStoreError" in output
        assert (
            "context=sqlite3.OperationalError" in output
            and "sqlite_errorcode=5" in output
        )
    else:
        assert "operation=verify_temp_cleanup" in output
        assert "exception=PermissionError" in output and "errno=13" in output
    assert all(word not in output for word in ("PRIVATE_", "SECRET"))
    assert json.loads(target(home, "claude").read_text())["keep"] is True
    assert list((home / ".taskpaw/hook-setup/backups").glob("*.bak"))
    assert not list((home / ".taskpaw").glob(".activity-check-*"))


@pytest.mark.parametrize("fault", ["bounded", "unavailable"])
def test_io_diagnostic_private_subclass_is_bounded_and_fail_safe(
    setup, monkeypatch, capsys, run, fault
):
    s, home = setup
    path = put(home, "claude", {"keep": True})
    before = path.read_bytes()

    class SECRET_PRIVATE_CLASS(OSError):
        def __getattribute__(self, name):
            if fault == "unavailable" and name == "errno":
                raise RuntimeError("SECRET_METADATA_FAILURE")
            return super().__getattribute__(name)

    def refused(*args):
        error = SECRET_PRIVATE_CLASS(
            1 << 100, "PRIVATE_MESSAGE SECRET", "PRIVATE_FILENAME"
        )
        error.winerror = True
        error.__context__ = error
        raise error

    monkeypatch.setattr(s, "read_bytes", refused)
    run(s, home, "install", "claude", expected=1)
    output = capsys.readouterr().out
    assert "claude: local I/O failed" in output
    assert all(
        word not in output for word in ("PRIVATE_", "SECRET", "errno=", "winerror=")
    )
    if fault == "bounded":
        assert "exception=OSErrorSubclass" in output and "truncated=true" in output
    else:
        assert "diagnostic=unavailable" in output
    assert len(output.encode("ascii")) <= 1100
    assert path.read_bytes() == before and not (home / ".taskpaw").exists()
