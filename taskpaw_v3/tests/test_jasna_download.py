import hashlib
import io

import pytest

from taskpaw_v3.monitors.plugins import jasna_download as D


@pytest.fixture
def tiny(monkeypatch):
    data = b"encrypted fixture"
    monkeypatch.setattr(
        D, "FILES", {"model.enc": (len(data), hashlib.sha256(data).hexdigest())}
    )
    monkeypatch.setattr(D.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(data))
    return data


def test_download_verifies_then_publishes(tmp_path, tiny):
    messages = []
    D.download(tmp_path, ["model.enc"], messages.append)
    assert (tmp_path / "model.enc").read_bytes() == tiny
    assert messages[-1]["state"] == "complete"
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize("data", [b"short", b"x" * 17, b"x" * 100])
def test_corrupt_download_never_published(tmp_path, tiny, monkeypatch, data):
    monkeypatch.setattr(D.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(data))
    with pytest.raises(OSError):
        D.download(tmp_path, ["model.enc"], lambda m: None)
    assert list(tmp_path.iterdir()) == []


def test_existing_file_reused_without_network(tmp_path, tiny, monkeypatch):
    target = tmp_path / "model.enc"
    target.write_bytes(b"existing")
    monkeypatch.setattr(
        D.urllib.request, "urlopen", lambda *a, **kw: pytest.fail("network")
    )
    D.download(tmp_path, ["model.enc"], lambda m: None)
    assert target.read_bytes() == b"existing"


def test_concurrent_publish_does_not_overwrite(tmp_path, tiny, monkeypatch):
    def opening(*a, **kw):
        (tmp_path / "model.enc").write_bytes(b"other downloader")
        return io.BytesIO(tiny)

    monkeypatch.setattr(D.urllib.request, "urlopen", opening)
    D.download(tmp_path, ["model.enc"], lambda m: None)
    assert (tmp_path / "model.enc").read_bytes() == b"other downloader"


def test_fallback_uses_only_pinned_mirror(tmp_path, tiny, monkeypatch):
    urls = []

    def opening(url, **kw):
        urls.append(url)
        if len(urls) == 1:
            raise OSError("offline")
        return io.BytesIO(tiny)

    monkeypatch.setattr(D.urllib.request, "urlopen", opening)
    D.download(tmp_path, ["model.enc"], lambda m: None)
    assert urls == [
        f"https://{host}/Kruk2/ltx-restore/resolve/{D.REVISION}/model.enc"
        for host in ("huggingface.co", "hf-mirror.com")
    ]


@pytest.mark.parametrize(
    "files", [["../secret"], ["https://example.com/a"], [], ["model.enc", "model.enc"]]
)
def test_reject_untrusted_filenames(tmp_path, tiny, files):
    with pytest.raises(ValueError):
        D.download(tmp_path, files, lambda m: None)


def test_worker_role_is_fixed():
    assert D.worker_argv()[-1] == "taskpaw_v3.monitors.plugins.jasna_download"


def test_packaged_role_dispatch_and_argv(monkeypatch):
    import sys

    from taskpaw_v3.packaging.backend_main import main

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert D.worker_argv() == [sys.executable, "jasna-download"]
    monkeypatch.setattr(D, "main", lambda: 17)
    assert main(["jasna-download"]) == 17


def test_download_session_retry_progress_and_stop(tmp_path, monkeypatch):
    from taskpaw_v3.monitors.plugins import jasna_download_session as S
    from taskpaw_v3.monitors.subs.child import Eof

    children = []

    class FakeChild:
        pid = 123
        rc = None
        survives = False

        def __init__(self, argv, **kw):
            self.sink = kw["line_sink"]
            self.payload = None
            children.append(self)

        def write_line(self, line):
            self.payload = line

        def poll(self):
            return self.rc

        def join_readers(self, timeout):
            pass

        def close_stdin(self):
            pass

        def terminate_tree(self, timeout):
            return not self.survives

    monkeypatch.setattr(S, "ChildProcess", FakeChild)
    session = S.DownloadSession()
    name = D.SHARED_FILES[1]
    session.request(tmp_path, [name], "ltx fast")
    request = session.status["request_id"]
    assert not session.respond("stale", True)
    assert session.respond(request, True)
    assert not children
    assert not session.respond(request, True)
    assert not session.tick()
    child = children[0]
    assert name in child.payload
    child.sink.put('{"state":"downloading","downloaded_bytes":123}')
    session.tick()
    assert session.status["downloaded_bytes"] == 123
    child.sink.put('{"state":"error","error":"fixture network failure"}')
    child.sink.put(Eof(child.pid))
    child.rc = 1
    assert not session.tick()
    assert session.status["state"] == "error"
    assert session.status["request_id"] != request
    assert session.respond(session.status["request_id"], True)
    session.tick()
    child = children[1]
    for _ in range(1000):
        child.sink.put('{"state":"downloading","downloaded_bytes":200}')
    child.sink.put('{"state":"complete"}')
    child.rc = 0
    assert not session.tick()  # wait until reader EOF, never lose terminal message
    child.sink.put(Eof(child.pid))
    assert session.tick()
    assert session.status == {} and session.child is None
    session.request(tmp_path, [name], "ltx fast")
    session.respond(session.status["request_id"], True)
    session.tick()
    child = children[-1]
    child.survives = True
    assert not session.stop(0.01)
    assert session.child is child and session.status == {}
    child.survives = False
    assert session.stop(0.01) and session.child is None


@pytest.mark.parametrize("valid", [True, False])
def test_worker_exits_normally_while_parent_keeps_stdin_open(tmp_path, valid):
    import json
    import subprocess

    name = D.SHARED_FILES[1]
    (tmp_path / name).write_bytes(b"existing isolated fixture")
    proc = subprocess.Popen(
        D.worker_argv(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        proc.stdin.write(
            json.dumps(
                {"destination": str(tmp_path), "files": [name if valid else "unknown"]}
            ).encode()
            + b"\n"
        )
        proc.stdin.flush()
        # Do not close stdin until the child has exited: the parent owns the
        # worker throughout completion, just like DownloadSession in production.
        rc = proc.wait(timeout=10)
        out, err = proc.communicate(timeout=5)
        assert rc == (0 if valid else 1), err.decode(errors="replace")
        assert not err
        assert json.loads(out)["state"] == ("complete" if valid else "error")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        proc.communicate(timeout=5)


def test_parent_eof_exits_a_blocked_download(tmp_path):
    import json
    import subprocess
    import sys

    code = "from taskpaw_v3.monitors.plugins import jasna_download as d; import time; d.download=lambda *a: time.sleep(60); raise SystemExit(d.main())"
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        out, err = proc.communicate(
            json.dumps(
                {"destination": str(tmp_path), "files": [D.SHARED_FILES[0]]}
            ).encode()
            + b"\n",
            timeout=5,
        )
        assert proc.returncode == 0 and not err
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_stop_during_slow_spawn_cleans_late_child(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from taskpaw_v3.monitors.plugins import jasna_download_session as S

    session = S.DownloadSession()
    killed = []
    child = SimpleNamespace(
        close_stdin=lambda: None,
        terminate_tree=lambda timeout: killed.append(True) or True,
        join_readers=lambda timeout: None,
        write_line=lambda _: pytest.fail("stopped worker received work"),
    )

    def late_spawn(*a, **kw):
        session.stop(0.01)
        return child

    monkeypatch.setattr(S, "ChildProcess", late_spawn)
    session.request(tmp_path, [D.SHARED_FILES[0]], "ltx")
    session.respond(session.status["request_id"], True)
    assert not session.tick()
    assert killed == [True] and session.child is None and session.status == {}


class _Child:
    """A fake download worker: the test feeds its output lines and exit code."""

    pid = 123

    def __init__(self, argv, **kw):
        self.sink = kw["line_sink"]
        self.payload = None
        self.rc = None
        self.survives = False

    def write_line(self, line):
        self.payload = line

    def poll(self):
        return self.rc

    def join_readers(self, timeout):
        pass

    def close_stdin(self):
        pass

    def terminate_tree(self, timeout):
        return not self.survives

    def finish(self):
        from taskpaw_v3.monitors.subs.child import Eof

        self.sink.put('{"state":"complete"}')
        self.sink.put(Eof(self.pid))
        self.rc = 0


@pytest.fixture
def workers(monkeypatch):
    from taskpaw_v3.monitors.plugins import jasna_download_session as S

    made = []

    def spawn(*a, **kw):
        made.append(_Child(*a, **kw))
        return made[-1]

    monkeypatch.setattr(S, "ChildProcess", spawn)
    return made


def _approved(destination, name=D.SHARED_FILES[1]):
    from taskpaw_v3.monitors.plugins import jasna_download_session as S

    session = S.DownloadSession()
    session.request(destination, [name], "ltx")
    assert session.respond(session.status["request_id"], True)
    return session


def test_one_download_per_destination_across_sessions(tmp_path, workers):
    """H3 D08: the monitor and the workflow never download into one folder at once."""
    first = _approved(tmp_path / "jasna")
    first.tick()
    assert first.status["state"] == "downloading" and len(workers) == 1
    second = _approved(tmp_path / "jasna" / ".." / "jasna")
    assert not second.tick()
    assert second.status["state"] == "error" and len(workers) == 1
    assert "already downloading" in second.status["error"]
    elsewhere = _approved(tmp_path / "other")
    elsewhere.tick()
    assert elsewhere.status["state"] == "downloading" and len(workers) == 2
    workers[0].finish()
    assert first.tick() is True
    # the folder is free again: the refused session's retry now starts
    assert second.respond(second.status["request_id"], True)
    second.tick()
    assert second.status["state"] == "downloading" and len(workers) == 3
    assert second.stop(0.01) and elsewhere.stop(0.01)
    again = _approved(tmp_path / "jasna")
    again.tick()
    assert again.status["state"] == "downloading"
    assert again.stop(0.01)


def test_stop_deletes_the_partial_files_of_its_request(tmp_path, workers):
    """H3 D09: a killed worker never ran its own cleanup."""
    name, other = D.SHARED_FILES[1], D.SHARED_FILES[0]
    dest = tmp_path / "jasna"
    session = _approved(dest, name)
    session.tick()
    partial = dest / f".{name}.k3j2.part"
    dest.mkdir()
    partial.write_bytes(b"half")
    kept = [dest / f".{other}.x1.part", dest / name, dest / "notes.part"]
    for path in kept:
        path.write_bytes(b"keep")
    workers[0].survives = True
    assert not session.stop(0.01)
    assert partial.exists()  # the worker may still be writing it
    workers[0].survives = False
    assert session.stop(0.01)
    assert not partial.exists()
    assert all(path.exists() for path in kept)


def test_stop_leaves_the_folder_alone_while_another_session_downloads(
    tmp_path, workers
):
    name = D.SHARED_FILES[1]
    dest = tmp_path / "jasna"
    busy = _approved(dest, name)
    busy.tick()
    waiting = _approved(dest, name)
    waiting.tick()  # refused: the folder is busy
    dest.mkdir()
    partial = dest / f".{name}.live.part"
    partial.write_bytes(b"being written")
    assert waiting.stop(0.01)
    assert partial.exists()
    assert busy.stop(0.01) and not partial.exists()
