"""The 媒体处理中心 UI contract (U1): the real routes over a realistic runtime.

The responses of `GET /films`, `/films/{id}`, `/plans` and the target preflight
are written to the UI's fixture, which its page tests load. The committed
fixture must equal what the backend produces now, so a changed field fails here
before the UI silently renders it empty. Regenerate after an intended change:

    TASKPAW_WRITE_UI_CONTRACT=1 uv run --locked pytest taskpaw_v3/tests/test_workflow_ui_contract.py
"""

# ruff: noqa: F811

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from taskpaw_v3.monitors.plugins.jasna_progress import (
    console_text,
    parse_jasna_progress_line,
)
from taskpaw_v3.tests import test_workflow_return_step as ret
from taskpaw_v3.tests.test_workflow_control_routes import (
    BASE,
    Host,
    _client,
    _drop,
    _root,
)
from taskpaw_v3.tests.test_workflow_finalize import (  # noqa: F401
    World,
    probeable_video,
)
from taskpaw_v3.tests.test_workflow_plan_routes import with_lookup
from taskpaw_v3.workflow.exec_restore import progress_text
from taskpaw_v3.workflow.executors import ExecOutcome

FIXTURE = Path(__file__).parents[1] / "ui/src/test/fixtures/workflow-contract.json"
REGENERATE = "TASKPAW_WRITE_UI_CONTRACT"
# Jasna 0.11.1's encode line, as recorded on the owner's machine (test_jasna).
LTX_ENCODE = (
    "LTX 2/4 encode:  52%|█████▏    | 156/300 [00:27<00:25,  5.62frame/s, "
    "run 12:12 left]"
)
TRANSLATE_ERROR = "LLM 超时：翻译服务 120 秒没有响应"
# Where the temp folders stand in for, as an operator's Windows machine shows it.
NAS = "\\\\nas-a\\actors"
FILMPAW = "C:\\Users\\operator\\AppData\\Roaming\\Filmpaw"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_SHA256 = re.compile(r"\b[0-9a-f]{64}\b")
_TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)")


class FailingTranslate(ret.FakeTranslate):
    def run(self, context):
        return ExecOutcome.failed("needs_attention", TRANSLATE_ERROR)


class HeldRestore(ret.FakeRestore):
    """Reports the LTX encode line as exec_restore does, then holds the stage."""

    def __init__(self, rt):
        super().__init__(rt)
        self.started = threading.Event()
        self.release = threading.Event()

    def run(self, context):
        state = parse_jasna_progress_line(LTX_ENCODE, {})
        detail = {**state, "current_file": "movie.mkv"}
        detail["console"] = console_text(LTX_ENCODE)  # U1c heartbeat
        context.progress(progress_text("", state), ltx=detail)
        self.started.set()
        self.release.wait(30)
        return super().run(context)


class ContractHost(Host):
    """`LocalWorkflowHost.status` over the real dispatcher and finalizer."""

    def __init__(self, world: World, dispatcher: Any):
        super().__init__(world)
        self.dispatcher = dispatcher

    def status(self) -> dict[str, Any]:
        d = self.dispatcher.status()
        return {
            "state": self.state,
            "reason": self.reason,
            "intake": self.intake,
            "ltx_download": None,
            "dispatch": {
                "state": d.state,
                "reason": d.reason,
                "current": d.current,
                "waiting": {**d.waiting, **self.finalizer.waiting},
                "paused": d.paused,
            },
        }


class Stable:
    """Replaces what differs per run: ids, hashes, times and temp paths."""

    def __init__(self, tmp: Path, paths: dict[Path, str], names: dict[str, str]):
        self.tmp = str(tmp)
        self.paths = {str(path): shown for path, shown in paths.items()}
        self.names = dict(names)
        self.hashes: dict[str, str] = {}

    def _id(self, match: re.Match[str]) -> str:
        return self.names.setdefault(match[0], f"id-{len(self.names) + 1}")

    def _hash(self, match: re.Match[str]) -> str:
        return self.hashes.setdefault(match[0], f"{len(self.hashes) + 1:064x}")

    def text(self, value: str) -> str:
        for path, shown in self.paths.items():
            if path in value:
                head, tail = value.split(path, 1)
                value = head + shown + tail.replace("/", "\\")
        assert self.tmp not in value, value  # a path the UI must never see
        value = _UUID.sub(self._id, value)
        value = _SHA256.sub(self._hash, value)
        return _TIME.sub("2026-10-21T08:00:00Z", value)

    def __call__(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {self.text(k): self(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self(v) for v in value]
        if isinstance(value, str):
            return self.text(value)
        return value


def ok(response) -> Any:
    assert response.status_code == 200, response.text
    return response.json()


def build(tmp_path: Path) -> dict[str, Any]:
    world = World(tmp_path)  # 待确认: Miko's final package, one candidate
    rt = world.rt
    try:
        world.scrape()
        failed = ret.add_real_film(rt)  # 异常: translate needs the operator
        rt.executors["translate"] = FailingTranslate(rt)
        ret.run_all(ret.dispatcher(rt))
        rt.executors["translate"] = ret.FakeTranslate(rt)
        restoring = ret.add_real_film(rt)  # 修复: LTX encode, held
        held = HeldRestore(rt)
        rt.executors["restore"] = held
        looked_up = ret.add_real_film(rt, plan=False)  # 待处理 with a cover
        unknown = ret.add_real_film(rt, plan=False)  # 待处理, nothing found
        profile = with_lookup(world, tmp_path)
        assert rt.lookup.run_once()
        profile.set(fake_lookup="fail")
        assert rt.lookup.run_once()
        dispatcher = ret.dispatcher(rt)
        worker = threading.Thread(target=dispatcher.run_once, daemon=True)
        worker.start()  # claims before the INBOX root exists to check sources
        try:
            assert held.started.wait(30), dispatcher.status()
            root = _root(world)
            stuck = _drop(world, root, "Downloads/ABF-393", "ABF-393.mp4", "setup.exe")
            arriving = _drop(world, root, "Performer/MOV-002", "MOV-002.mp4")
            host = ContractHost(world, dispatcher)
            host.intake = {
                "package_id": arriving,
                "phase": "copy",
                "bytes_completed": 1073741824,
                "total": 4294967296,
            }
            client = _client(lambda: host)
            with world.fake:
                host.finalizer.run_once()  # 等待确认入库
                url = f"{BASE}/films/{world.film}/target"
                target = {
                    "candidates": ok(client.get(url)),
                    "selected": ok(client.get(url, params={"performer_id": "actor"})),
                    "missing": ok(client.get(url, params={"performer_id": "gone"})),
                }
                films = ok(client.get(f"{BASE}/films"))
                film_ids = [world.film, failed, restoring, looked_up, unknown]
                details = {f: ok(client.get(f"{BASE}/films/{f}")) for f in film_ids}
                plans = ok(client.get(f"{BASE}/plans"))
            target["offline"] = ok(client.get(url, params={"performer_id": "actor"}))
        finally:
            held.release.set()
            worker.join(30)
        names = {
            world.film: "film-review",
            failed: "film-failed",
            restoring: "film-restoring",
            looked_up: "film-planning",
            unknown: "film-planning-unknown",
            stuck: "package-stuck",
            arriving: "package-arriving",
            rt.identity.worker_id: "worker-local",
        }
        paths = {world.nas.parent: NAS, world.filmpaw_dir: FILMPAW}
        stable = Stable(tmp_path, paths, names)
        return stable(
            {
                "films": films,
                "film": {names[f]: details[f] for f in film_ids},
                "plans": plans,
                "target": target,
            }
        )
    finally:
        rt.close()


def test_the_ui_fixture_matches_the_backend(tmp_path):
    produced = build(tmp_path)
    if os.environ.get(REGENERATE) == "1":
        FIXTURE.write_text(
            json.dumps(produced, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    committed = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert committed == produced, (
        f"{FIXTURE.name} is stale: rerun with {REGENERATE}=1 and fix the UI"
    )


def test_the_fixture_covers_every_page_the_ui_tests():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    groups = {row["group"] for row in data["films"]["films"]}
    assert {"planning", "active", "attention", "confirm"} <= groups
    restore = next(
        step
        for row in data["films"]["films"]
        for step in row["steps"]
        if step["key"] == "restore" and step["state"] == "running"
    )
    assert restore["ltx"]["ltx_progress"]["stage_name"] == "encode"
    assert restore["started_at"] and restore["console"] == LTX_ENCODE  # U1c
    statuses = {e["lookup"]["status"] for e in data["plans"]["plans"]}
    assert statuses == {"succeeded", "failed"}
    assert data["target"]["missing"]["error_code"] == "missing"
    assert data["target"]["offline"]["error_code"] == "offline"
    assert len(data["target"]["candidates"]["candidates"]["items"]) == 1
