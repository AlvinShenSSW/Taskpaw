"""#301: temporary packages and fake Filmpaw only."""

# ruff: noqa: F811
import errno
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from taskpaw_v3.agent.server import workflow_routes
from taskpaw_v3.tests.test_workflow_control_routes import (  # noqa: F401
    client,
    host,
    probeable_video,
    world,
)
from taskpaw_v3.tests.test_workflow_settings import World as SettingsWorld
from taskpaw_v3.tests.test_workflow_settings import _tools
from taskpaw_v3.workflow import board, file_identity


@pytest.mark.parametrize("data_source", ["data_dir", "config_dir"])
@pytest.mark.parametrize("drive", ["network", "removable"])
def test_default_output_requires_a_fixed_local_disk(
    tmp_path, monkeypatch, data_source, drive
):
    import ctypes
    import sys

    from taskpaw_v3.agent.server import workflow_settings as ws
    from taskpaw_v3.core.config import AgentConfig, WorkflowSettings
    from taskpaw_v3.workflow import staging

    if drive == "removable" and sys.platform != "win32":
        pytest.skip("Windows fixed-drive check")
    settings = WorkflowSettings(
        enabled=True,
        role="controller",
        data_dir=str(tmp_path / "data") if data_source == "data_dir" else None,
        **_tools(tmp_path),
    )
    expected = Path(ws.effective_output(settings, tmp_path))
    checked = []

    def network(path):
        checked.append(path)
        return drive == "network"

    monkeypatch.setattr(staging, "_network_drive", network)
    if drive == "removable":
        monkeypatch.setattr(ctypes.windll.kernel32, "GetDriveTypeW", lambda _: 2)
    with pytest.raises(ws.Refused) as refused:
        ws.validate(
            settings,
            settings,
            AgentConfig(server_id="s", machine="m"),
            tmp_path,
            settings,
            ws.Retained(),
        )
    assert "本机" in refused.value.fields["output_dir"]
    assert checked == [expected]


@pytest.mark.parametrize("failure", ["corrupt", "io", "rename", "crash"])
def test_failed_placement_keeps_sources_and_retry_uses_plain_name(
    world, monkeypatch, failure
):
    from taskpaw_v3.tests import test_workflow_scrape_step as scr
    from taskpaw_v3.tests.test_workflow_local_dispatch import Crash
    from taskpaw_v3.workflow import scrape_step

    step = scr.scrape_step(world.tmp)
    captured = []

    def capture(context, final):
        captured.append((context, final))
        raise Crash()

    monkeypatch.setattr(step, "_place", capture)
    dispatcher = scr.dispatcher(world.rt, step)
    for _ in range(4):
        assert dispatcher.run_once()
    with pytest.raises(Crash):
        dispatcher.run_once()
    context, final = captured[0]
    source = world.rt.final_dir(final)
    before = {
        p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()
    }
    output = world.rt.output_dir
    other = output / ".placing-another-film"
    other.mkdir(parents=True)
    (other / "keep").write_bytes(b"keep")
    real_rename, real_link, real_copy = (
        Path.rename,
        os.link,
        scrape_step.shutil.copyfile,
    )
    copied = []
    linked = []
    source_deletes = []
    real_delete = file_identity.delete_exact

    def delete(path, member, cancel=None):
        if path.is_relative_to(source):
            source_deletes.append(path)
        return real_delete(path, member, cancel)

    def link(path, dest):
        if failure == "link":
            linked.append(dest)
            if len(linked) == 2:
                raise OSError("fake local link failure")
            return real_link(path, dest)
        if Path(dest).is_relative_to(output):
            raise OSError(errno.EXDEV, "fake other volume")
        return real_link(path, dest)

    def rename(path, dest):
        if path.is_relative_to(source):
            raise OSError(errno.EXDEV, "fake other volume")
        if Path(dest) == output / source.name and failure == "rename":
            raise OSError("fake rename failure")
        return real_rename(path, dest)

    def copy(path, dest, *args, **kwargs):
        result = real_copy(path, dest, *args, **kwargs)
        copied.append(dest)
        if len(copied) == 2:
            if failure == "corrupt":
                Path(dest).write_bytes(b"corrupt")
            elif failure == "io":
                raise OSError("fake copy failure")
            elif failure == "crash":
                raise Crash()
        return result

    monkeypatch.setattr(file_identity, "delete_exact", delete)
    with monkeypatch.context() as patch:
        patch.setattr(os, "link", link)
        patch.setattr(Path, "rename", rename)
        patch.setattr(scrape_step.shutil, "copyfile", copy)
        with pytest.raises((scrape_step._Failure, OSError, Crash)):
            scrape_step.LocalScrapeStep._place(step, context, final)
    assert not source_deletes
    assert {
        p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()
    } == before
    if failure != "crash":
        assert list(output.iterdir()) == [other]
    else:
        assert not (output / source.name).exists()
    # A new attempt owns only this film's stale staging; other films are untouched.
    latest = world.rt.store.latest_final_revision(world.rt.operator, world.film)
    final = final.model_copy(update={"revision": latest + 1})
    placed = scrape_step.LocalScrapeStep._place(step, context, final)
    target = output / source.name
    assert Path(placed.output_directory) == target
    assert set(output.iterdir()) == {other, target}
    assert (other / "keep").read_bytes() == b"keep"
    assert {
        p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file()
    } == before


def test_placement_copies_when_the_volume_has_no_hard_links(world, monkeypatch):
    """Kimi r2 F1: ReFS/exFAT refuse hard links with a non-EXDEV error; the
    verified copy takes over instead of failing the stage."""
    from taskpaw_v3.tests import test_workflow_scrape_step as scr
    from taskpaw_v3.tests.test_workflow_local_dispatch import Crash
    from taskpaw_v3.workflow import scrape_step

    step = scr.scrape_step(world.tmp)
    captured = []

    def capture(context, final):
        captured.append((context, final))
        raise Crash()

    monkeypatch.setattr(step, "_place", capture)
    dispatcher = scr.dispatcher(world.rt, step)
    for _ in range(4):
        assert dispatcher.run_once()
    with pytest.raises(Crash):
        dispatcher.run_once()
    context, final = captured[0]
    source = world.rt.final_dir(final)
    before = {
        p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()
    }

    def no_links(path, dest):
        raise OSError(errno.EINVAL, "fake: volume has no hard links")

    monkeypatch.setattr(os, "link", no_links)
    placed = scrape_step.LocalScrapeStep._place(step, context, final)
    target = world.rt.output_dir / source.name
    assert Path(placed.output_directory) == target
    assert {
        p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file()
    } == before


@pytest.mark.parametrize("reason", ["no_approval", "changed_approval", "vanished"])
def test_archive_progress_cleared_before_wait_or_resubmit(monkeypatch, reason):
    from taskpaw_v3.workflow.finalize import Finalizer

    fin = Finalizer(SimpleNamespace(data_dir=Path("unused")), None, SimpleNamespace())
    approval = SimpleNamespace(approval_id="current", binding=object())
    wf = SimpleNamespace(film_id="film", approval=approval)
    op = SimpleNamespace(approval_id="current", operation_id="operation")
    if reason == "no_approval":
        wf.approval = None
    elif reason == "changed_approval":
        op.approval_id = "old"
    fin.progress[wf.film_id] = {"copied_bytes": 60}
    fin.filmpaw.lookup = lambda _: None
    submissions = []
    monkeypatch.setattr(fin, "_submit", lambda *args: submissions.append(args))
    assert fin._poll(wf, op, {}) == (reason == "vanished")
    assert wf.film_id not in fin.progress
    assert len(submissions) == (reason == "vanished")


@pytest.mark.parametrize("recorded", [False, True])
def test_placement_crash_at_manifest_write_retries_without_suffix(
    world, monkeypatch, recorded
):
    from taskpaw_v3.tests import test_workflow_scrape_step as scr
    from taskpaw_v3.tests.test_workflow_local_dispatch import Crash

    step = scr.scrape_step(world.tmp)
    dispatcher = scr.dispatcher(world.rt, step)
    for _ in range(4):
        assert dispatcher.run_once()
    real_put = world.rt.store.put_final_manifest

    def crash(*args):
        if recorded:
            real_put(*args)
        raise Crash()

    monkeypatch.setattr(world.rt.store, "put_final_manifest", crash)
    with pytest.raises(Crash):
        dispatcher.run_once()
    assert not (world.rt.output_dir / "ABC-123").exists()
    world.rt.close()
    world.rt = world.runtime()
    assert scr.dispatcher(world.rt, step).run_once()
    final = board.final_manifest(world.rt, world.film)
    assert board.output_dir(world.rt, final) == world.rt.output_dir / "ABC-123"
    assert list(world.rt.output_dir.iterdir()) == [world.rt.output_dir / "ABC-123"]


def test_output_default_and_creation(tmp_path):
    tools = _tools(tmp_path)
    w = SettingsWorld(tmp_path, data_dir=str(tmp_path / "data"))
    assert w.get().json()["saved"]["output_dir"] == str(tmp_path / "data" / "成品")
    assert w.get().json()["output_dir_default"] is True
    target = tmp_path / "output"
    response = w.put(
        dict(
            role="controller",
            **tools,
            output_dir=str(target),
            create_dirs=["output_dir"],
        )
    )
    assert response.status_code == 200, response.text
    assert target.is_dir()
    assert w.get().json()["output_dir_default"] is False


@pytest.mark.parametrize("path", [r"\\server\share\output", "relative", "mapped"])
def test_output_refuses_nonlocal_directories(tmp_path, monkeypatch, path):
    from taskpaw_v3.workflow import staging

    tools = _tools(tmp_path)
    if path == "mapped":
        path = str(tmp_path / "mapped")
        Path(path).mkdir()
        monkeypatch.setattr(staging, "_network_drive", lambda _: True)
    w = SettingsWorld(tmp_path)
    response = w.put(dict(role="controller", **tools, output_dir=path))
    assert response.status_code == 422
    assert "output_dir" in response.json()["fields"]
    assert response.json()["fields"]["output_dir"] != "未知字段"


@pytest.mark.parametrize("approved", [False, True])
def test_output_change_waits_for_confirmation_and_archive(world, approved):
    world.scrape()
    if approved:
        with world.fake:
            world.finalizer().confirm(world.film, "actor")
    tool_dir = world.tmp / "settings"
    tool_dir.mkdir()
    tools = _tools(tool_dir)
    w = SettingsWorld(
        world.tmp,
        enabled=True,
        role="controller",
        data_dir=str(world.rt.data_dir),
        **tools,
    )
    target = world.tmp / "elsewhere"
    target.mkdir()
    response = w.put(dict(role="controller", output_dir=str(target)))
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "workflow_busy"
    assert "暂停" in response.json()["detail"]


@pytest.mark.parametrize("cross_volume", [False, True])
@pytest.mark.parametrize("retain_source", [False, True])
def test_output_placement_and_exact_cleanup(
    world, monkeypatch, cross_volume, retain_source
):
    target = world.tmp / "成品"
    world.rt.output_dir = target
    deleted = []
    real_delete = file_identity.delete_exact
    real_link = os.link

    def link(path, dest):
        if cross_volume and Path(dest).is_relative_to(target):
            raise OSError(errno.EXDEV, "fake cross-volume")
        return real_link(path, dest)

    def delete(path, member, cancel=None):
        # A whole verified package must exist before any source member is deleted.
        assert (target / "ABC-123" / member.relative_path).is_file()
        deleted.append(path)
        if retain_source:
            return "blocked_writing"
        return real_delete(path, member, cancel)

    monkeypatch.setattr(os, "link", link)
    monkeypatch.setattr(file_identity, "delete_exact", delete)
    world.scrape()
    final = board.final_manifest(world.rt, world.film)
    assert final is not None
    out = board.output_dir(world.rt, final)
    assert out == target / "ABC-123"
    assert out.is_dir()
    assert len(deleted) == len(final.items)
    assert all(path.exists() == retain_source for path in deleted)
    assert all(
        file_identity.check(out / member.relative_path, member) == "ok"
        for member in world.finalizer()
        ._cleanup_manifest("workcopy", final.manifest_hash, final.items)
        .members
    )
    monkeypatch.setattr(file_identity, "delete_exact", real_delete)
    with world.fake:
        fin = world.finalizer()
        fin.confirm(world.film, "actor")
        fin.run_once()
        assert out.is_dir()  # submitting a copy is not verification
        world.ticks(fin)
    assert not out.exists()
    assert (world.nas / "ABC-123").is_dir()
    assert not list((world.rt.work_dir / "scrape").glob(f"{world.film}-r*"))


def test_output_collision_does_not_overwrite(world):
    world.rt.output_dir = world.tmp / "成品"
    existing = world.rt.output_dir / "ABC-123"
    existing.mkdir(parents=True)
    (existing / "keep").write_bytes(b"keep")
    world.scrape()
    final = board.final_manifest(world.rt, world.film)
    assert board.output_dir(world.rt, final) == world.rt.output_dir / "ABC-123 (2)"
    assert (existing / "keep").read_bytes() == b"keep"


@pytest.mark.parametrize("phase", ["copying", "verifying"])
def test_archive_progress_reaches_list_and_detail(
    world, host, client, monkeypatch, phase
):
    world.scrape()
    with world.fake:
        fin = host.finalizer
        fin.confirm(world.film, "actor")
        fin.run_once()
    task = dict(
        status="running",
        phase=phase,
        progress_bytes=60,
        total_bytes=100,
        verified_bytes=20,
        completed_count=0,
        total_count=1,
        target_path=str(world.nas),
        created_at="2026-10-24T08:00:00Z",
        items=[dict(name="ABC-123", progress_bytes=60, size_bytes=100)],
    )
    monkeypatch.setattr(fin.filmpaw, "lookup", lambda _: task)
    fin.run_once()
    for data in [
        client.get("/control/workflow/films").json()["films"][0],
        client.get(f"/control/workflow/films/{world.film}").json(),
    ]:
        step = next(s for s in data["steps"] if s["key"] == "archive")
        assert step["progress"] == dict(
            phase=phase,
            copied_bytes=60,
            verified_bytes=20,
            total_bytes=100,
            files_done=0,
            files_total=1,
            items=[dict(name="ABC-123", bytes=60, total=100)],
            target=str(world.nas / "ABC-123"),
            started_at=task["created_at"],
        )


def test_open_recorded_output_and_archived_history(world, host, client, monkeypatch):
    opened = []
    # open_folder is Windows-only (D09); replace it so the route runs on every OS
    monkeypatch.setattr(
        workflow_routes, "open_folder", lambda path: opened.append(str(path))
    )
    world.scrape()
    final = board.final_manifest(world.rt, world.film)
    recorded = board.output_dir(world.rt, final)
    world.rt.output_dir = world.tmp / "changed-setting"
    url = f"/control/workflow/films/{world.film}/open-folder"
    assert (
        client.post(url, json={"which": "output", "path": str(world.tmp)}).status_code
        == 422
    )
    assert client.post(url, json={"which": "output"}).status_code == 204
    assert opened == [str(recorded)]
    with world.fake:
        fin = host.finalizer
        fin.confirm(world.film, "actor")
        world.ticks(fin)
    assert client.post(url, json={"which": "archive"}).status_code == 204
    assert opened[-1] == str(world.nas / "ABC-123")
    missing = client.post(url, json={"which": "output"})
    assert (missing.status_code, missing.json()["detail"]) == (409, "目录已不存在")


def test_settings_open_directory_is_local_and_missing_is_409(tmp_path, monkeypatch):
    opened = []
    # open_folder is Windows-only (D09); replace it so the route runs on every OS
    monkeypatch.setattr(
        workflow_routes, "open_folder", lambda path: opened.append(str(path))
    )
    w = SettingsWorld(tmp_path)
    url = "/control/workflow/settings/directory/open"
    headers = {"Authorization": "Bearer control-token"}
    assert (
        w.client.post(url, json={"path": str(tmp_path)}, headers=headers).status_code
        == 204
    )
    assert opened == [str(tmp_path)]
    assert (
        w.client.post(
            url, json={"path": str(tmp_path / "gone")}, headers=headers
        ).status_code
        == 409
    )
    assert (
        w.client.post(
            url, json={"path": r"\\server\share"}, headers=headers
        ).status_code
        == 422
    )


def test_cross_volume_verification_failure_keeps_every_source(world, monkeypatch):
    from taskpaw_v3.tests import test_workflow_scrape_step as scr
    from taskpaw_v3.workflow import scrape_step

    target = world.rt.output_dir
    link = os.link
    copy = scrape_step.shutil.copyfile
    real_delete = file_identity.delete_exact
    deletes = []

    def cross_volume(path, dest):
        if Path(dest).is_relative_to(target):
            raise OSError(errno.EXDEV, "fake other volume")
        return link(path, dest)

    def corrupt(source, dest, *args, **kwargs):
        result = copy(source, dest, *args, **kwargs)
        if Path(dest).is_relative_to(target):
            Path(dest).write_bytes(b"corrupt copy")
        return result

    def delete(path, member, cancel=None):
        if not path.is_relative_to(target):
            deletes.append(path)
        return real_delete(path, member, cancel)

    monkeypatch.setattr(os, "link", cross_volume)
    monkeypatch.setattr(scrape_step.shutil, "copyfile", corrupt)
    monkeypatch.setattr(file_identity, "delete_exact", delete)
    dispatcher = scr.dispatcher(world.rt, scr.scrape_step(world.tmp))
    for _ in range(6):
        dispatcher.run_once()
    assert world.status("scrape") == "failed"
    assert board.final_manifest(world.rt, world.film) is None
    assert not deletes
    assert list((world.rt.work_dir / "scrape").rglob("*.mkv"))
    assert list(world.inbox.rglob("*.mkv"))
    assert not list(target.iterdir())


def test_output_rejects_mapped_drive_before_reading_it(tmp_path, monkeypatch):
    from taskpaw_v3.workflow import staging

    tools = _tools(tmp_path)
    w = SettingsWorld(tmp_path)
    path = str(tmp_path / "mapped-missing")
    monkeypatch.setattr(staging, "_network_drive", lambda _: True)
    response = w.put(dict(role="controller", **tools, output_dir=path))
    assert "网络路径" in response.json()["fields"]["output_dir"]
