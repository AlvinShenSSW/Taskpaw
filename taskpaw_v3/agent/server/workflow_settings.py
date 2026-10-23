"""P2-4 (#295) R13–R16: 设置 · 媒体工作流 — agent.yaml's `workflow` from the UI.

`GET /control/workflow/settings` shows the saved and the running `workflow`
fields (no token, no key: the section holds none), whether a restart is
pending, what is in flight and the LTX presets. `PUT` takes a role (不参与 /
主控 / GPU 工作机) and the role's fields, validates each field (errors in
Chinese, never echoing a value), refuses a switch that would strand work, and
saves through `MonitorAdmin.update_config` (a backup, unknown keys kept). Every
change applies after a restart: port 5682, the provider closures, the file
lease and keep-awake belong to the running process.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any, Literal, cast

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from taskpaw_v3.core.config import AgentConfig, WorkflowSettings

if TYPE_CHECKING:
    from taskpaw_v3.agent.server.admin import MonitorAdmin

Role = Literal["off", "controller", "worker"]
SAVED = "已保存，重启 TaskPaw 后生效"
STORE_NAME = "workflow.db"  # `LocalRuntime.db_path`
PATHS = (
    "inbox_root",
    "data_dir",
    "ffprobe_path",
    "jasna_exe_path",
    "whisperjav_exe_path",
    "mdcx_python",
    "mdcx_root",
    "mdcx_profile",
    "filmpaw_data_dir",
    "staging_dir",
)
_DIRS = ("inbox_root", "mdcx_root")
_FILES = (
    "ffprobe_path",
    "jasna_exe_path",
    "whisperjav_exe_path",
    "mdcx_python",
    "mdcx_profile",
)
_INTS = ("worker_api_port", "staging_cap_gb")
FIELDS = (*PATHS, "remote_workers", "ltx_gpu", *_INTS)
_COMPUTE = ("restore", "asr", "translate")


class Refused(Exception):
    """A refused save: 422 per field, or 409 `workflow_busy`."""

    def __init__(
        self,
        status: int,
        detail: str,
        *,
        code: str | None = None,
        fields: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status, self.detail, self.code = status, detail, code
        self.fields = fields or {}


def role_of(settings: WorkflowSettings) -> Role:
    return settings.role if settings.enabled else "off"


def view(settings: WorkflowSettings) -> dict[str, Any]:
    """The section's fields (every one is a path, a switch or a number)."""
    value = settings.model_dump(include={"enabled", *FIELDS, "role"})
    value["role"] = role_of(settings)
    return value


def presets() -> list[dict[str, str]]:
    from taskpaw_v3.monitors.plugins.jasna_presets import preset_catalogue

    return [{"id": "auto", "label": "自动"}] + [
        {"id": str(gpu["id"]), "label": str(gpu["label"])}
        for gpu in preset_catalogue()["gpus"]
    ]


def default_data_dir(
    settings: WorkflowSettings, config_dir: Path | None
) -> Path | None:
    """`start_local_workflow`'s own choice of the workflow's data directory."""
    if settings.data_dir:
        return Path(settings.data_dir)
    return config_dir / "workflow" if config_dir is not None else None


# ── the body ────────────────────────────────────────────────────────────
def parse(body: Any) -> tuple[Role, dict[str, Any]]:
    """`{role, …fields}` → (role, the `workflow` fields named); 422 per field."""
    if not isinstance(body, dict):
        raise Refused(422, "请求格式不正确", fields={"role": "格式不正确"})
    role = body.get("role")
    errors: dict[str, str] = {}
    if role not in ("off", "controller", "worker"):
        errors["role"] = "请选择角色"
    fields: dict[str, Any] = {}
    for key, value in body.items():
        if key == "role":
            continue
        if key not in FIELDS:
            errors[str(key)[:64]] = "未知字段"
        elif key in PATHS or key == "ltx_gpu":
            if value is not None and not isinstance(value, str):
                errors[key] = "格式不正确"
            else:
                text = (value or "").strip()
                fields[key] = text or (None if key != "ltx_gpu" else "auto")
        elif key == "remote_workers":
            if not isinstance(value, bool):
                errors[key] = "格式不正确"
            else:
                fields[key] = value
        elif isinstance(value, bool) or not isinstance(value, int):
            errors[key] = "格式不正确"
        elif key == "worker_api_port" and not 1 <= value <= 65535:
            errors[key] = "端口须在 1–65535 之间"
        elif key == "staging_cap_gb" and not 1 <= value <= 100_000:
            errors[key] = "暂存上限须在 1–100000 GB 之间"
        else:
            fields[key] = value
    if errors:
        raise Refused(422, "设置有误", fields=errors)
    if role == "off":
        fields["enabled"] = False
    else:
        fields.update(enabled=True, role=role)
    return cast(Role, role), fields


def _path_error(raw: str) -> str | None:
    if raw.startswith(("\\\\", "//")):
        return "不能是网络路径"
    windows, posix = PureWindowsPath(raw), PurePosixPath(raw)
    if ".." in windows.parts or ".." in posix.parts:
        return "不能包含 .."
    if not Path(raw).is_absolute():
        return "必须是完整路径"
    return None


def validate(
    new: WorkflowSettings,
    old: WorkflowSettings,
    config: AgentConfig,
    config_dir: Path | None,
) -> None:
    """R14: the fields the new role uses, each with its own message."""
    from taskpaw_v3.workflow.staging import StagingError, validate_dir

    role = role_of(new)
    errors: dict[str, str] = {}
    for name in PATHS:
        raw = getattr(new, name)
        if raw and (why := _path_error(raw)):
            errors[name] = why
    if role == "off":
        if errors:
            raise Refused(422, "设置有误", fields=errors)
        return
    needed = ("inbox_root", "ffprobe_path") if role == "controller" else ()
    for name in needed:
        if not getattr(new, name):
            errors.setdefault(name, "请填写")
    for name in _DIRS:
        raw = getattr(new, name)
        if raw and name not in errors and not Path(raw).is_dir():
            errors[name] = "目录不存在"
    for name in _FILES:
        raw = getattr(new, name)
        if raw and name not in errors and not Path(raw).is_file():
            errors[name] = "文件不存在"
    port = new.worker_api_port
    if not 1 <= port <= 65535:
        errors["worker_api_port"] = "端口须在 1–65535 之间"
    elif port == config.bind_port:
        errors["worker_api_port"] = "端口与网络端口相同"
    elif port == config.control_port:
        errors["worker_api_port"] = "端口与控制端口相同"
    if new.ltx_gpu not in {p["id"] for p in presets()}:
        errors["ltx_gpu"] = "未知的显卡预设"
    if role == "worker":
        if not 1 <= new.staging_cap_gb <= 100_000:
            errors["staging_cap_gb"] = "暂存上限须在 1–100000 GB 之间"
        if "staging_dir" not in errors:
            try:
                validate_dir(new.staging_dir)
            except StagingError as exc:
                errors["staging_dir"] = str(exc)
    if "data_dir" not in errors:
        before = default_data_dir(old, config_dir)
        after = default_data_dir(new, config_dir)
        if (
            before is not None
            and (before / STORE_NAME).exists()
            and (after is None or _key(after) != _key(before))
        ):
            errors["data_dir"] = "工作目录已有媒体数据"
    if errors:
        raise Refused(422, "设置有误", fields=errors)


def _key(path: Path) -> str:
    return str(path.expanduser().absolute()).rstrip("\\/").casefold()


# ── what a switch would strand (R16) ────────────────────────────────────
def controller_busy(host: Any) -> tuple[int, int]:
    """(films, of which on a GPU worker) with an unresolved compute
    assignment, an open route or an open archive operation."""
    state = getattr(host, "state", None)
    if host is None or state not in ("starting", "running"):
        return 0, 0  # nothing runs: nothing is stranded
    rt = getattr(host, "runtime", None)
    if state == "starting" or rt is None:
        raise Refused(409, "媒体流水线正在启动，请稍后再试", code="workflow_busy")
    store, op = rt.store, rt.operator
    controller = rt.identity.worker_id
    films: set[str] = set()
    remote: set[str] = set()
    for item in store.list_open_assignments(op):
        if item.stage not in _COMPUTE:
            continue
        film = store.get_stage(op, item.assignment.job_id).film_id
        films.add(film)
        if item.assignment.worker_id not in (None, controller):
            remote.add(film)
    for route in store.list_routes(op):
        films.add(route.film_id)
        if route.worker_id != controller:
            remote.add(route.film_id)
    for film in store.film_board(op, done_limit=None, energies=False):
        if film.archive_open:
            films.add(film.film_id)
    return len(films), len(remote)


def worker_busy(host: Any) -> bool:
    """A worker's routes, a claim or held children (`claimed` covers them)."""
    if host is None:
        return False
    dispatcher = getattr(host, "dispatcher", None)
    return bool(getattr(host, "routes", None)) or bool(
        dispatcher is not None and dispatcher.claimed()
    )


def refuse_busy(
    new: WorkflowSettings,
    running: WorkflowSettings,
    controller: Callable[[], Any],
    worker: Callable[[], Any],
) -> None:
    """R16, against what runs now: leaving 主控 or turning 允许 GPU 工作机接入
    off while films are in flight, or leaving a busy worker role."""
    if role_of(running) == "controller":
        leaving = role_of(new) != "controller" or (
            running.remote_workers and not new.remote_workers
        )
        if leaving:
            films, remote = controller_busy(controller())
            if films:
                raise Refused(
                    409,
                    f"还有 {films} 部影片在处理中（{remote} 部在 GPU 工作机上），"
                    "请先暂停派发并等待完成",
                    code="workflow_busy",
                )
    elif role_of(running) == "worker" and role_of(new) != "worker":
        if worker_busy(worker()):
            raise Refused(
                409,
                "本机还有派来的影片或进行中的任务，请先暂停领取并等待完成",
                code="workflow_busy",
            )


# ── routes ──────────────────────────────────────────────────────────────
def register_workflow_settings(
    app: FastAPI,
    config: AgentConfig,
    admin: MonitorAdmin | None,
    *,
    config_dir: Path | None,
    workflow_provider: Callable[[], Any] | None,
    worker_provider: Callable[[], Any] | None,
) -> None:
    def controller() -> Any:
        return workflow_provider() if workflow_provider is not None else None

    def worker() -> Any:
        return worker_provider() if worker_provider is not None else None

    def busy() -> dict[str, int]:
        if role_of(config.workflow) != "controller":
            return {"films": 0, "remote": 0}
        try:
            films, remote = controller_busy(controller())
        except Refused:
            return {"films": 0, "remote": 0}
        return {"films": films, "remote": remote}

    def saved() -> WorkflowSettings:
        if admin is None:
            return config.workflow
        value = admin.desired_workflow()
        return value if isinstance(value, WorkflowSettings) else config.workflow

    @app.get("/control/workflow/settings")
    def workflow_settings() -> dict[str, Any]:
        current = saved()
        return {
            "saved": view(current),
            "running": view(config.workflow),
            "restart_required": current != config.workflow,
            "busy": busy(),
            "presets": presets(),
        }

    @app.put("/control/workflow/settings")
    async def workflow_settings_put(request: Request) -> Any:
        from starlette.concurrency import run_in_threadpool

        if admin is None:
            raise HTTPException(status_code=503, detail="设置不可用")
        try:
            body = await request.json()
        except ValueError:
            body = None
        try:
            _role, fields = parse(body)

            def check(validated: AgentConfig, old: WorkflowSettings) -> None:
                validate(validated.workflow, old, validated, config_dir)
                refuse_busy(validated.workflow, config.workflow, controller, worker)

            result = await run_in_threadpool(
                admin.update_config, {"workflow": fields}, workflow_check=check
            )
        except Refused as exc:
            content: dict[str, Any] = {"detail": exc.detail}
            if exc.code:
                content["code"] = exc.code
            if exc.fields:
                content["fields"] = exc.fields
            return JSONResponse(status_code=exc.status, content=content)
        except ValueError:  # the merged config failed the model (never echoed)
            return JSONResponse(
                status_code=422,
                content={"detail": "设置有误", "fields": {"role": "设置有误"}},
            )
        except OSError:  # nothing was committed (the backup goes first)
            return JSONResponse(
                status_code=500, content={"detail": "保存失败：无法写入 agent.yaml"}
            )
        return {
            "ok": True,
            "restart_required": result["restart_required"],
            "message": SAVED,
        }
