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
