"""#301: temporary packages and fake Filmpaw only."""

# ruff: noqa: F811
import errno
import os
from pathlib import Path

import pytest

from taskpaw_v3.tests.test_workflow_control_routes import (  # noqa: F401
    client,
    host,
    probeable_video,
    world,
)
from taskpaw_v3.tests.test_workflow_settings import World as SettingsWorld
from taskpaw_v3.tests.test_workflow_settings import _tools
from taskpaw_v3.workflow import board, file_identity


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
def test_output_placement_and_exact_cleanup(world, monkeypatch, cross_volume):
    target = world.tmp / "成品"
    world.rt.output_dir = target
    deleted = []
    real_delete = file_identity.delete_exact
    real_rename = Path.rename

    def rename(path, dest):
        if cross_volume and Path(dest).is_relative_to(target):
            raise OSError(errno.EXDEV, "fake cross-volume")
        return real_rename(path, dest)

    def delete(path, member, cancel=None):
        # A whole verified package must exist before any source member is deleted.
        assert (target / "ABC-123" / member.relative_path).is_file()
        deleted.append(path)
        return real_delete(path, member, cancel)

    monkeypatch.setattr(Path, "rename", rename)
    monkeypatch.setattr(file_identity, "delete_exact", delete)
    world.scrape()
    final = board.final_manifest(world.rt, world.film)
    assert final is not None
    out = board.output_dir(world.rt, final)
    assert out == target / "ABC-123"
    assert out.is_dir()
    assert len(deleted) == (len(final.items) if cross_volume else 0)
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
    monkeypatch.setattr(os, "startfile", opened.append, raising=False)
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
    monkeypatch.setattr(os, "startfile", opened.append, raising=False)
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
    rename = Path.rename
    copy = scrape_step.shutil.copyfile
    deletes = []

    def cross_volume(path, dest):
        if Path(dest).is_relative_to(target):
            raise OSError(errno.EXDEV, "fake other volume")
        return rename(path, dest)

    def corrupt(source, dest, *args, **kwargs):
        result = copy(source, dest, *args, **kwargs)
        if Path(dest).is_relative_to(target):
            Path(dest).write_bytes(b"corrupt copy")
        return result

    monkeypatch.setattr(Path, "rename", cross_volume)
    monkeypatch.setattr(scrape_step.shutil, "copyfile", corrupt)
    monkeypatch.setattr(
        file_identity, "delete_exact", lambda *args: deletes.append(args)
    )
    dispatcher = scr.dispatcher(world.rt, scr.scrape_step(world.tmp))
    for _ in range(6):
        dispatcher.run_once()
    assert world.status("scrape") == "failed"
    assert board.final_manifest(world.rt, world.film) is None
    assert not deletes
    assert list((world.rt.work_dir / "scrape").rglob("*.mkv"))
    assert list(world.inbox.rglob("*.mkv"))


def test_output_rejects_mapped_drive_before_reading_it(tmp_path, monkeypatch):
    from taskpaw_v3.workflow import staging

    tools = _tools(tmp_path)
    w = SettingsWorld(tmp_path)
    path = str(tmp_path / "mapped-missing")
    monkeypatch.setattr(staging, "_network_drive", lambda _: True)
    response = w.put(dict(role="controller", **tools, output_dir=path))
    assert "网络路径" in response.json()["fields"]["output_dir"]
