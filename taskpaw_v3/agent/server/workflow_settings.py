"""P2-4 (#295) R13–R16: 设置 · 媒体工作流 — agent.yaml's `workflow` from the UI.

`GET /control/workflow/settings` shows the saved and the running `workflow`
fields (no token, no key: the section holds none), whether a restart is
pending, what is in flight and the LTX presets. `PUT` takes a role (不参与 /
主控 / GPU 工作机) and the role's fields, validates each field (errors in
Chinese, never echoing a value), refuses a switch that would strand work, and
saves through `MonitorAdmin.update_config` (a backup, unknown keys kept). Every
change applies after a restart: port 5682, the provider closures, the file
lease and keep-awake belong to the running process.

§9 review fixes: the data directory guard runs for every role and compares
with the running directory as well as the saved one, and a worker's paired
directory (`worker-identity.json`) is held too (CX3/IR5); a busy worker cannot
move its staging or data directory (IR3); the worker API port cannot change
under paired workers (IR4) — held at the port the pairings were made on,
whatever role a save asks for (CX14); an empty ffprobe is fine when PATH has
one (IR6); a save racing another answers 409 (IR12). CX15/CX16: every guard
compares the save with the retained state on disk (`retained`): the workflow
stores and the worker identities under the saved, running and requested data
directories, read directly whether or not a host runs; state that cannot be
read refuses the guarded change (「无法确认是否有影片在处理，暂不允许切换」).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
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
HELD_STORE = "工作目录已有媒体数据"
HELD_PAIRING = "工作目录已有工作机配对"
PORT_PAIRED = "已有工作机配对在此端口，改端口需先让它们重新配对"
STAGING_PAIRED = "已配对的工作机改暂存目录需要重新加入"
OPERATION_BUSY = "正在保存其他设置，请稍后再试"
UNKNOWN = "无法确认是否有影片在处理，暂不允许切换"  # §9 CX15/CX16
WORKER_BUSY = "本机还有派来的影片或进行中的任务，请先暂停领取并等待完成"
STORE_TIMEOUT = 2.0  # seconds: the guard looks at the store, never waits on it
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


def detect_ffprobe() -> str | None:
    """§9 IR6: the ffprobe the pipeline finds by itself when the field is
    empty (`local_host`, `exec_restore`: `shutil.which`)."""
    return shutil.which("ffprobe")


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
    running: WorkflowSettings,
    state: Retained,
) -> None:
    """R14: the fields the new role uses, each with its own message; then
    the retained state's field holds, whatever the role (§9 CX3/CX14/CX16):
    the data directory, the paired staging directory and the pairing port."""
    role = role_of(new)
    errors: dict[str, str] = {}
    for name in PATHS:
        raw = getattr(new, name)
        if raw and (why := _path_error(raw)):
            errors[name] = why
    if role != "off":
        _validate_role(new, config, errors)
    if "data_dir" not in errors:
        held = _data_dir_held(new, (old, running), config_dir)
        if held:
            errors["data_dir"] = held
    if "staging_dir" not in errors and any(
        _moved(new.staging_dir, root) for root in state.paired
    ):
        errors["staging_dir"] = STAGING_PAIRED
    if any(port != new.worker_api_port for port in state.ports):
        errors.setdefault("worker_api_port", PORT_PAIRED)
    if errors:
        raise Refused(422, "设置有误", fields=errors)


def _validate_role(
    new: WorkflowSettings, config: AgentConfig, errors: dict[str, str]
) -> None:
    from taskpaw_v3.workflow.staging import StagingError, validate_dir

    role = role_of(new)
    needed: tuple[str, ...] = ()
    if role == "controller":  # §9 IR6: ffprobe on PATH needs no entry
        needed = ("inbox_root",) if detect_ffprobe() else ("inbox_root", "ffprobe_path")
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


def _data_dir_held(
    new: WorkflowSettings,
    references: tuple[WorkflowSettings | None, ...],
    config_dir: Path | None,
) -> str | None:
    """§9 CX3/IR5: the data directory cannot leave one (saved or running)
    that holds a workflow store or a worker's pairing."""
    from taskpaw_v3.workflow.remote_worker import IDENTITY_NAME, PENDING_NAME

    after = default_data_dir(new, config_dir)
    for reference in references:
        if reference is None:
            continue
        before = default_data_dir(reference, config_dir)
        if before is None or (after is not None and _same_path(after, before)):
            continue
        if (before / STORE_NAME).exists():
            return HELD_STORE
        if (before / IDENTITY_NAME).exists() or (before / PENDING_NAME).exists():
            return HELD_PAIRING
    return None


def _same_path(a: Path, b: Path) -> bool:
    """§9 CX9: filesystem identity when both exist (`samefile`); otherwise
    the names, normalised by the platform (`normcase` folds case only on
    Windows, so `Data` and `data` stay two directories on a case-sensitive
    filesystem)."""
    a, b = a.expanduser(), b.expanduser()
    try:
        if a.exists() and b.exists():
            return os.path.samefile(a, b)
    except OSError:
        pass

    def name(path: Path) -> str:
        return os.path.normcase(os.path.abspath(path)).rstrip("\\/")

    return name(a) == name(b)


def _moved(new: str | None, old: str | None) -> bool:
    if not new or not old:
        return bool(new) != bool(old)
    return not _same_path(Path(new), Path(old))


# ── the retained state: THE baseline of every guard (§9 CX15/CX16) ─────
@dataclass
class Retained:
    """What the disk retains under every data directory the saved, running
    and requested settings name — never the previous agent.yaml value or
    the requested role, and whether or not a host runs (or failed to start).
    `store_known` / `worker_known` False: some state could not be read, and
    unknown is never idle."""

    films: int = 0  # with an unresolved compute assignment, an open route or archive
    remote: int = 0  # of which on a GPU worker
    store_known: bool = True
    ports: set[int] = field(default_factory=set)  # the retained pairings' port
    paired: list[str] = field(default_factory=list)  # identity staging dirs
    # a busy worker: (its data directory, its staging directory)
    busy: list[tuple[Path | None, str | None]] = field(default_factory=list)
    worker_known: bool = True


def _directories(
    names: tuple[WorkflowSettings, ...], config_dir: Path | None
) -> list[Path]:
    found: list[Path] = []
    for settings in names:
        directory = default_data_dir(settings, config_dir)
        if directory is not None and not any(_same_path(directory, d) for d in found):
            found.append(directory)
    return found


def _open_store(path: Path) -> sqlite3.Connection:
    """Read-only, with a short busy timeout: a look, never a wait."""
    uri = f"{path.resolve().as_uri()}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=STORE_TIMEOUT)


def _read_store(directory: Path) -> tuple[set[str], set[str], bool]:
    """(films in flight, of which on a GPU worker, pairs a worker) from the
    store in `directory`; `sqlite3.Error` when it cannot be read (locked,
    corrupt, not a workflow store)."""
    try:
        controller = json.loads(
            (directory / "local-identity.json").read_text(encoding="utf-8")
        ).get("worker_id")
    except (OSError, ValueError, AttributeError):
        controller = None  # unknown: every worker's work counts as remote
    compute = ",".join(f"'{stage}'" for stage in _COMPUTE)
    conn = _open_store(directory / STORE_NAME)
    try:
        conn.execute("BEGIN")  # one snapshot for every read
        assignments = conn.execute(
            "SELECT s.film_id,a.worker_id FROM assignments a"
            " JOIN stages s ON s.job_id=a.job_id"
            f" WHERE a.resolved_at IS NULL AND s.stage IN ({compute})"
        ).fetchall()
        routes = conn.execute(
            "SELECT film_id,worker_id,kind FROM film_routes WHERE state='open'"
        ).fetchall()
        archives = conn.execute(
            "SELECT film_id FROM operations"
            " WHERE kind='archive' AND resolved_at IS NULL"
        ).fetchall()
        (pairs,) = conn.execute(
            "SELECT COUNT(*) FROM workers"
            " WHERE enrollment_state != 'retired' AND worker_id != ?",
            (controller or "",),
        ).fetchone()
    finally:
        conn.close()
    films: set[str] = set()
    remote: set[str] = set()
    for film, worker in assignments:
        films.add(film)
        if worker is not None and worker != controller:
            remote.add(film)
    for film, worker, kind in routes:
        films.add(film)
        if kind != "local" and worker != controller:
            remote.add(film)
    films.update(film for (film,) in archives)
    return films, remote, bool(pairs)


def _controller_state(
    directories: list[Path], running_port: int, state: Retained
) -> None:
    """The workflow store under each directory: the films in flight and the
    port the non-retired workers were paired on (`pairing-port.json`; a store
    paired before that record falls back to the running port, an unreadable
    store counts as paired)."""
    from taskpaw_v3.workflow.devices import pairing_port

    films: set[str] = set()
    remote: set[str] = set()
    for directory in directories:
        if not (directory / STORE_NAME).exists():
            continue
        try:
            flight, far, pairs = _read_store(directory)
        except sqlite3.Error:
            state.store_known, pairs = False, True
        else:
            films |= flight
            remote |= far
        if pairs:
            state.ports.add(pairing_port(directory) or running_port)
    state.films, state.remote = len(films), len(remote)


def _worker_state(
    directories: list[Path],
    host: Any,
    running: WorkflowSettings,
    config_dir: Path | None,
    state: Retained,
) -> None:
    """`worker-identity.json` and its pending file under each directory: the
    staging directory chosen at 加入, busy by the one conservative
    `worker_busy` over it; plus a live host's in-memory work (routes, a run,
    held children) over the staging directory it runs on."""
    from taskpaw_v3.workflow.remote_dispatch import worker_busy
    from taskpaw_v3.workflow.remote_worker import (
        IDENTITY_NAME,
        PENDING_NAME,
        WorkerIdentity,
    )

    for directory in directories:
        for name in (IDENTITY_NAME, PENDING_NAME):
            try:
                identity = WorkerIdentity.read(directory / name)
            except (OSError, ValueError):
                state.worker_known = False
                continue
            if identity is None:
                continue
            root = identity.staging_dir
            if not isinstance(root, str) or not root:
                state.worker_known = False
                continue
            state.paired.append(root)
            if worker_busy(Path(root), None):
                state.busy.append((directory, root))
    if host is None:
        return
    summary = host.summary()
    if summary.get("routes") or summary.get("busy"):
        ident = getattr(host, "identity", None)
        settings = getattr(host, "settings", running)
        live: str | None = (
            ident.staging_dir if ident is not None else settings.staging_dir
        )
        data = getattr(host, "data_dir", None) or default_data_dir(running, config_dir)
        state.busy.append((data, live))


def retained(
    names: tuple[WorkflowSettings, ...],
    running: WorkflowSettings,
    config_dir: Path | None,
    worker_host: Any = None,
    *,
    worker: bool = True,
) -> Retained:
    """§9 CX15/CX16: the retained authoritative state every guard compares
    a save with, read directly (read-only, short timeouts)."""
    state = Retained()
    directories = _directories(names, config_dir)
    _controller_state(directories, running.worker_api_port, state)
    if worker:
        _worker_state(directories, worker_host, running, config_dir, state)
    return state


def refuse_busy(
    new: WorkflowSettings, state: Retained, config_dir: Path | None
) -> None:
    """R16 against the retained state: leaving 主控 while any film is in
    flight, turning 允许 GPU 工作机接入 off while one is on a GPU worker, or a
    busy worker leaving its role or moving its staging or data directory
    (§9 IR3). Unknown state refuses the change (「无法确认…」)."""
    role = role_of(new)
    if not state.store_known and (role != "controller" or not new.remote_workers):
        raise Refused(409, UNKNOWN, code="workflow_busy")
    if (state.films and role != "controller") or (
        state.remote and not new.remote_workers
    ):
        raise Refused(
            409,
            f"还有 {state.films} 部影片在处理中（{state.remote} 部在 GPU 工作机上），"
            "请先暂停派发并等待完成",
            code="workflow_busy",
        )
    if not state.worker_known:
        raise Refused(409, UNKNOWN, code="workflow_busy")
    after = default_data_dir(new, config_dir)
    for data, root in state.busy:
        if (
            role != "worker"
            or _moved(new.staging_dir, root)
            or _moved(str(after) if after else None, str(data) if data else None)
        ):
            raise Refused(409, WORKER_BUSY, code="workflow_busy")


# ── routes ──────────────────────────────────────────────────────────────
def register_workflow_settings(
    app: FastAPI,
    config: AgentConfig,
    admin: MonitorAdmin | None,
    *,
    config_dir: Path | None,
    worker_provider: Callable[[], Any] | None,
) -> None:
    def worker() -> Any:
        return worker_provider() if worker_provider is not None else None

    def saved() -> WorkflowSettings:
        if admin is None:
            return config.workflow
        value = admin.desired_workflow()
        return value if isinstance(value, WorkflowSettings) else config.workflow

    def busy() -> dict[str, Any]:
        """§9 CX15: what the retained stores hold, whatever the host does."""
        state = retained(
            (saved(), config.workflow), config.workflow, config_dir, worker=False
        )
        return {
            "films": state.films,
            "remote": state.remote,
            "unknown": not state.store_known,
        }

    @app.get("/control/workflow/settings")
    def workflow_settings() -> dict[str, Any]:
        current = saved()
        return {
            "saved": view(current),
            "running": view(config.workflow),
            "restart_required": current != config.workflow,
            "busy": busy(),
            "presets": presets(),
            # §9 IR6: what an empty ffprobe field uses (None: not on PATH)
            "ffprobe_detected": detect_ffprobe(),
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
                new, running = validated.workflow, config.workflow
                state = retained((old, running, new), running, config_dir, worker())
                validate(new, old, validated, config_dir, running, state)
                refuse_busy(new, state, config_dir)

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
        except ValueError as exc:  # the merged config failed the model (never echoed)
            if str(exc) == "operation_busy":  # §9 IR12: another save holds the lock
                return JSONResponse(
                    status_code=409,
                    content={"detail": OPERATION_BUSY, "code": "operation_busy"},
                )
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
