"""#299 R1/R2/§6: the first-run marker and the setup wizard's step.

`agent.setup.json` sits next to agent.yaml. `service._main` writes it (atomic)
right after it scaffolds a fresh agent.yaml: `{v, status: "pending", step:
"role", server_id}`. While it is pending, names the config's `server_id` and
no event-state record, anchor or `.fault-*` backup exists, the backend
initializes the event lineage itself (`initialize_state`, origin
`new_pairing`) — a fresh install has no old pairing to retire, so the native
confirmation dialog has nothing to protect. Anything else (no marker, a
finished one, another identity, leftover state) keeps that dialog.

The wizard (UI) reads `GET /control/setup` and records each step it enters
with `PUT /control/setup {step, role}`: a step of the role's path, never
skipping ahead of the furthest step reached. `POST /control/setup/complete`
finishes it (a 工作机 must have joined its controller first). Nothing here
holds a secret; the file is never logged.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from taskpaw_v3.core.state import StateError, atomic_json, initialize_state, state_paths

if TYPE_CHECKING:
    from taskpaw_v3.core.config import WorkflowSettings

log = logging.getLogger("taskpaw.agent.setup")

SETUP_NAME = "agent.setup.json"
SetupRole = Literal["agent", "controller", "worker"]
PATHS: dict[str, tuple[str, ...]] = {
    "agent": ("role", "network", "done"),
    "controller": ("role", "network", "controller", "done"),
    "worker": ("role", "network", "worker_fields", "worker_join", "done"),
}
# the media-workflow role each setup role saves (普通 Agent = 不参与)
WORKFLOW_ROLE = {"agent": "off", "controller": "controller", "worker": "worker"}
NOT_JOINED = "加入主控后才能继续"
DONE = "设置已完成"


def marker_path(config_path: Path) -> Path:
    return Path(config_path).with_name(SETUP_NAME)


def write_pending(config_path: Path, server_id: str) -> None:
    """The fresh-install marker; written only right after a scaffold."""
    atomic_json(
        marker_path(config_path),
        {"v": 1, "status": "pending", "step": "role", "server_id": server_id},
    )


def read_marker(config_path: Path) -> Optional[dict[str, Any]]:
    """The marker, or None when absent or not a marker (never trusted then)."""
    try:
        data = json.loads(marker_path(config_path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        log.warning("unreadable setup marker ignored")
        return None
    if (
        not isinstance(data, dict)
        or data.get("v") != 1
        or data.get("status") not in ("pending", "done")
        or not isinstance(data.get("server_id"), str)
        or data.get("step") not in {s for p in PATHS.values() for s in p}
        or data.get("role") not in (None, *PATHS)
    ):
        log.warning("unreadable setup marker ignored")
        return None
    return data


def _state_present(state_path: Path) -> bool:
    primary, anchor, _ = state_paths(state_path)
    return any(p.exists() or p.is_symlink() for p in (primary, anchor)) or any(
        any(p.parent.glob(p.name + ".fault-*")) for p in (primary, anchor)
    )


def silent_init_allowed(config_path: Path, state_path: Path, server_id: str) -> bool:
    """R1: a pending marker for THIS identity and no state of any kind."""
    marker = read_marker(config_path)
    if marker is None or marker["status"] != "pending":
        return False
    if marker["server_id"] != server_id:
        return False
    try:
        return not _state_present(state_path)
    except OSError:
        return False


def maybe_initialize(config_path: Path, state_path: Path, server_id: str) -> bool:
    """Initialize the event lineage of a fresh install; False leaves startup
    to the existing admission (and its confirmation dialog)."""
    if not silent_init_allowed(config_path, state_path, server_id):
        return False
    try:
        initialize_state(state_path, server_id, origin="new_pairing")
    except (StateError, OSError):
        log.warning("first-run event state could not be initialized")
        return False
    log.info("first run: initialized the event state of a fresh install")
    return True


class SetupRefused(Exception):
    def __init__(self, status: int, detail: str, fields: dict[str, str] | None = None):
        super().__init__(detail)
        self.status, self.detail, self.fields = status, detail, fields or {}


class SetupStore:
    """The marker's wizard state, serialized; the file is the truth."""

    def __init__(self, config_path: Path) -> None:
        self.config_path = Path(config_path)
        self._lock = threading.Lock()

    def view(self) -> dict[str, Any]:
        marker = read_marker(self.config_path)
        if marker is None:
            return {"status": "absent", "step": None, "role": None}
        return {
            "status": marker["status"],
            "step": marker["step"],
            "role": marker.get("role"),
        }

    def _pending(self) -> dict[str, Any]:
        marker = read_marker(self.config_path)
        if marker is None or marker["status"] != "pending":
            raise SetupRefused(409, DONE)
        return marker

    def put(
        self, body: Any, joined: Callable[[], bool], workflow_role: Callable[[], str]
    ) -> dict[str, Any]:
        """Enter `step` of `role`'s path. Forward-only: never a step past the
        one after the furthest reached (a changed role restarts at 网络与访问);
        going back to an earlier step is a UI move and needs no write."""
        if not isinstance(body, dict):
            raise SetupRefused(422, "请求格式不正确", {"step": "格式不正确"})
        role, step = body.get("role"), body.get("step")
        if role not in PATHS:
            raise SetupRefused(422, "请选择角色", {"role": "请选择角色"})
        path = PATHS[role]
        if step not in path or step == "role":
            raise SetupRefused(422, "步骤不正确", {"step": "步骤不正确"})
        with self._lock:
            marker = self._pending()
            old = marker.get("step")
            reached = (
                path.index(old) if marker.get("role") == role and old in path else 0
            )
            if path.index(step) > reached + 1:
                raise SetupRefused(422, "请按顺序完成每一步", {"step": "请按顺序完成"})
            if step == "done":
                self._check_done(role, joined, workflow_role)
            marker.update(step=step, role=role)
            atomic_json(marker_path(self.config_path), marker)
            return self.view()

    @staticmethod
    def _check_done(
        role: str, joined: Callable[[], bool], workflow_role: Callable[[], str]
    ) -> None:
        if workflow_role() != WORKFLOW_ROLE[role]:
            raise SetupRefused(422, "角色设置尚未保存", {"role": "角色设置尚未保存"})
        if role == "worker" and not joined():
            raise SetupRefused(422, NOT_JOINED, {"step": NOT_JOINED})

    def complete(
        self, joined: Callable[[], bool], workflow_role: Callable[[], str]
    ) -> dict[str, Any]:
        with self._lock:
            marker = self._pending()
            role = marker.get("role")
            if role not in PATHS:
                raise SetupRefused(422, "请选择角色", {"role": "请选择角色"})
            self._check_done(role, joined, workflow_role)
            marker.update(status="done", step="done")
            atomic_json(marker_path(self.config_path), marker)
            return self.view()


def worker_joined(settings: "WorkflowSettings", config_dir: Optional[Path]) -> bool:
    """A confirmed pairing: `worker-identity.json` naming a worker id."""
    from taskpaw_v3.agent.server.workflow_settings import default_data_dir
    from taskpaw_v3.workflow.remote_worker import IDENTITY_NAME, WorkerIdentity

    data = default_data_dir(settings, config_dir)
    if data is None:
        return False
    try:
        identity = WorkerIdentity.read(data / IDENTITY_NAME)
    except (OSError, ValueError):
        return False
    return identity is not None and bool(identity.worker_id)


def register_setup_routes(
    app: FastAPI,
    config_path: Optional[Path],
    saved_workflow: Callable[[], "WorkflowSettings"],
) -> None:
    """`/control/setup` (loopback control API, control token)."""
    from taskpaw_v3.agent.server.workflow_settings import role_of

    store = SetupStore(config_path) if config_path is not None else None

    def joined() -> bool:
        directory = config_path.parent if config_path is not None else None
        return worker_joined(saved_workflow(), directory)

    def workflow_role() -> str:
        return role_of(saved_workflow())

    def refusal(exc: SetupRefused) -> JSONResponse:
        content: dict[str, Any] = {"detail": exc.detail}
        if exc.fields:
            content["fields"] = exc.fields
        return JSONResponse(status_code=exc.status, content=content)

    @app.get("/control/setup")
    def setup_view() -> dict[str, Any]:
        if store is None:
            return {"status": "absent", "step": None, "role": None}
        return store.view()

    @app.put("/control/setup")
    async def setup_put(request: Request) -> Any:
        if store is None:
            return refusal(SetupRefused(409, DONE))
        try:
            body = await request.json()
        except ValueError:
            body = None
        try:
            return store.put(body, joined, workflow_role)
        except SetupRefused as exc:
            return refusal(exc)
        except OSError:
            return JSONResponse(status_code=500, content={"detail": "无法保存设置进度"})

    @app.post("/control/setup/complete")
    def setup_complete() -> Any:
        if store is None:
            return refusal(SetupRefused(409, DONE))
        try:
            return store.complete(joined, workflow_role)
        except SetupRefused as exc:
            return refusal(exc)
        except OSError:
            return JSONResponse(status_code=500, content={"detail": "无法保存设置进度"})
