"""P2-2 (#293): a GPU worker's side — join, identity, probe, boot, heartbeat.

`WorkerHost` starts after the agent is ready, is stoppable and joins its
thread (constitution §4, S-7). 加入 takes the code `TP1:<host>:<port>:
<pairing_id>:<ticket>` from the controller's 设备 page; the worker chooses its
own credential and writes it (`worker-identity.pending.json`) **before**
sending the pair request, so a lost reply is recovered through `GET /self`
(R3); a working identity is replaced only once the new pairing succeeded
(IR7). The probe is posted only when identity, VRAM or the proposed profiles
differ from the controller's record (R4): a worker's own post de-verifies it.
It is re-run every 60 s for the tier and ASR readiness, and at every beat
while the GPU cannot be read (CX4, IR8). The boot is re-posted with the same
id until reconciled (R6, C6) while heartbeats run — every 5 s with a route,
15 s idle, backing off up to 30 s while the controller is unreachable (R7, R8,
IR6). An ordinary error is shown, backed off and retried; only a revoked
credential or `stop()` ends the loop (IR8). P2-2 runs no stage, claims nothing.

The identity file holds the secret like the local identity file (home LAN,
single user, P0); it is written atomically and never logged, and `repr`
hides it (S-6).
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, cast

from pydantic import SecretStr

from taskpaw_v3.core.config import WorkflowSettings
from taskpaw_v3.core.net import bind_is_global, bind_is_wildcard
from taskpaw_v3.core.state import atomic_json

from .fleet_live import PROTOCOL
from .staging import Staging, StagingError, validate_dir
from .worker_client import ApiError, WorkerClient, backoff

log = logging.getLogger("taskpaw.workflow.remote_worker")

IDENTITY_NAME = "worker-identity.json"
PENDING_NAME = "worker-identity.pending.json"  # IR7: a pairing not yet confirmed
ACTIVE_SECONDS = 5.0  # with an open route (R7)
IDLE_SECONDS = 15.0
PROBE_SECONDS = 60.0  # CX4: tier and ASR readiness follow the disk
TIERS = ("standard", "good", "high")
_TICKET = re.compile(r"[A-Za-z0-9_-]{32,256}\Z")


class JoinError(ValueError):
    """加入 refused; the message is UI text and never echoes the code."""


@dataclass(frozen=True, repr=False)
class PairingCode:
    host: str
    port: int
    pairing_id: str
    ticket: SecretStr


def parse_code(code: str) -> PairingCode:
    """`TP1:<host>:<port>:<pairing_id>:<ticket>`, an IPv6 host in brackets.
    A wildcard or a public (global) host is refused."""
    bad = JoinError("配对码无效")
    value = (code or "").strip()
    if not value.startswith("TP1:"):
        raise bad
    rest = value[4:]
    if rest.startswith("["):
        host, sep, rest = rest[1:].partition("]")
        if not sep or not rest.startswith(":"):
            raise bad
        rest = rest[1:]
    else:
        host, sep, rest = rest.partition(":")
        if not sep:
            raise bad
    parts = rest.split(":")
    if len(parts) != 3 or not host:
        raise bad
    port_text, pairing_id, ticket = parts
    try:
        if not port_text.isdigit() or str(uuid.UUID(pairing_id)) != pairing_id:
            raise ValueError
        port = int(port_text)
    except ValueError:
        raise bad from None
    if not 0 < port < 65536 or not _TICKET.fullmatch(ticket):
        raise bad
    if bind_is_wildcard(host) or bind_is_global(host):
        raise JoinError("配对码中的主控地址必须是局域网地址")
    return PairingCode(host, port, pairing_id, SecretStr(ticket))


@dataclass(repr=False)
class WorkerIdentity:
    """The worker's enrollment: where the controller is and its credential."""

    controller_host: str
    controller_port: int
    pairing_id: str
    credential_id: str
    secret: SecretStr
    staging_dir: str
    ticket: SecretStr | None = None  # until the pairing is confirmed
    worker_id: str | None = None
    joined_at: str = field(default_factory=lambda: _now_text())

    def __repr__(self) -> str:
        return f"WorkerIdentity(worker_id={self.worker_id!r})"

    @property
    def credential(self) -> tuple[str, SecretStr]:
        return self.credential_id, self.secret

    def save(self, path: Path) -> None:
        data = asdict(self)
        data["secret"] = self.secret.get_secret_value()
        data["ticket"] = self.ticket.get_secret_value() if self.ticket else None
        atomic_json(path, data)

    @classmethod
    def load(cls, path: Path) -> WorkerIdentity | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["secret"] = SecretStr(data["secret"])
            data["ticket"] = SecretStr(data["ticket"]) if data.get("ticket") else None
            return cls(**data)
        except FileNotFoundError:
            return None
        except (ValueError, KeyError, TypeError):
            log.warning("unreadable worker identity ignored; join again")
            return None


def _now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── the probe (R4) ──────────────────────────────────────────────────────
@dataclass(frozen=True)
class Probe:
    """What the worker proposes (`body`, the controller's `ProbeBody`) and
    what it can run now (heartbeat `tiers_ready`, `asr_ready`)."""

    body: Mapping[str, Any]
    tiers_ready: tuple[str, ...]
    asr_ready: bool


ProbeFn = Callable[[WorkflowSettings, Staging], "Probe | None"]


def default_probe(settings: WorkflowSettings, staging: Staging) -> Probe | None:
    """GPU and identity, free disk, and the restore/ASR executors' profiles.

    The executors are built only for `tools()` and `max_resolution()`; their
    runtime arrives with remote execution (P2-3, `ExecRuntime`)."""
    from taskpaw_v3.monitors.plugins import jasna

    from .exec_asr import AsrExecutor
    from .exec_restore import RestoreExecutor, resolve_gpu_preset
    from .executors import StageExecutor
    from .local_gpu import machine_identity, query_gpu
    from .local_runtime import LocalRuntime
    from .models import ProfileCapability

    gpu = query_gpu()
    if gpu is None:
        return None
    unbound = cast(LocalRuntime, None)  # never used by tools()/max_resolution()
    executors: list[StageExecutor] = []
    tiers: tuple[str, ...] = ()
    restore = None
    if settings.jasna_exe_path:
        auto = settings.ltx_gpu == "auto"
        preset = resolve_gpu_preset(gpu) if auto else settings.ltx_gpu
        try:
            restore = RestoreExecutor(
                unbound, settings.jasna_exe_path, preset, preset_auto=auto
            )
            executors.append(restore)
        except ValueError:
            log.warning("worker restore probe: invalid Jasna settings")
    if restore is not None and Path(settings.jasna_exe_path or "").is_file():
        fast = restore.cfg.ltx_fast and jasna.fast_supported(jasna.query_gpus())
        folder = str(Path(settings.jasna_exe_path or "").parent)
        tiers = tuple(
            tier for tier in TIERS if not jasna.ltx_missing_files(folder, tier, fast)
        )
    asr_ready = False
    if settings.whisperjav_exe_path:
        try:
            executors.append(AsrExecutor(unbound, settings.whisperjav_exe_path))
            asr_ready = Path(settings.whisperjav_exe_path).is_file()
        except ValueError:
            log.warning("worker ASR probe: invalid WhisperJAV settings")
    profiles = []
    for executor in executors:
        tools = executor.tools()
        width, height = executor.max_resolution(gpu.vram_mib)
        profiles.append(
            ProfileCapability(
                profile_id=tools.profile,
                profile_revision=tools.profile_revision,
                tool=tools.tool,
                tool_version=tools.tool_version,
                model=tools.model,
                model_version=tools.model_version,
                allowed_stages=(executor.stage,),
                max_width=width,
                max_height=height,
            ).model_dump(mode="json", exclude={"schema_version"})
        )
    body = {
        "source": "nvidia-smi",
        "cpu": "worker",
        "ram_mib": 1,
        "gpu_model": gpu.name,
        "vram_mib": gpu.vram_mib,
        "device_identity": machine_identity(),
        "gpu_identity": gpu.uuid,
        "free_disk_bytes": staging.free(),
        "model_ready": bool(profiles),
        "profiles": profiles,
    }
    return Probe(body, tiers, asr_ready)


def probe_changed(probe: Mapping[str, Any], record: Mapping[str, Any]) -> bool:
    """R4: identity, VRAM or the proposed profiles differ from the record."""
    cap = record.get("capability") or {}
    old = cap.get("probed") or {}
    if not old:
        return True
    keys = ("device_identity", "gpu_identity", "vram_mib")

    def profiles(value: Mapping[str, Any]) -> list[Any]:
        return [
            {k: v for k, v in p.items() if k != "schema_version"}
            for p in value.get("profiles") or []
        ]

    return any(probe.get(k) != old.get(k) for k in keys) or profiles(probe) != profiles(
        old
    )


class GpuLive(Protocol):
    def __call__(self) -> dict[str, Any]: ...


def default_gpu_live() -> dict[str, Any]:
    """The heartbeat's GPU: the shared nvidia-smi sample and the lease."""
    from taskpaw_v3.core import gpu_lease
    from taskpaw_v3.core.gpu_sample import gpu_sample

    sample = gpu_sample() or {}
    return {
        "used_mib": sample.get("mem_used_mb"),
        "util": sample.get("util_pct"),
        "power_w": sample.get("power_w"),
        "hold": "manual" if gpu_lease.holder() is not None else "none",
    }


# ── the host ────────────────────────────────────────────────────────────
class WorkerHost:
    def __init__(
        self,
        settings: WorkflowSettings,
        data_dir: Path,
        *,
        probe: ProbeFn = default_probe,
        gpu_live: GpuLive = default_gpu_live,
        client_factory: Callable[..., WorkerClient] = WorkerClient,
        version: str | None = None,
        active_seconds: float = ACTIVE_SECONDS,
        idle_seconds: float = IDLE_SECONDS,
        backoff_base: float = 1.0,
        probe_seconds: float = PROBE_SECONDS,
    ) -> None:
        from taskpaw_v3 import __version__

        self.settings, self.data_dir = settings, data_dir
        self.identity_path = data_dir / IDENTITY_NAME
        self.pending_path = data_dir / PENDING_NAME
        self.probe_seconds = probe_seconds
        self._probe_due = 0.0  # monotonic; 0: probe at the next beat
        self._join_lock = threading.Lock()  # IR7: one join at a time
        self._probe, self._gpu_live = probe, gpu_live
        self._client_factory = client_factory
        self.version = version or __version__
        self.active, self.idle, self.backoff_base = (
            active_seconds,
            idle_seconds,
            backoff_base,
        )
        self.boot_id = str(uuid.uuid4())  # one per process start (R6)
        self.state, self.reason = "unjoined", "尚未加入媒体工作流"
        self.identity: WorkerIdentity | None = None
        self.worker_id: str | None = None
        self.routes: list[dict[str, Any]] = []
        self.paused = False
        self.staged: list[dict[str, Any]] = []  # filled by the fetch loop (P2-3)
        self.last_heartbeat: datetime | None = None
        self.reconciled = False
        self._probe_body: Mapping[str, Any] = {}
        self._tiers: tuple[str, ...] = ()
        self._asr_ready = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> None:
        self.identity = WorkerIdentity.load(self.identity_path)
        if self.identity is None:
            return
        self.worker_id = self.identity.worker_id
        self._spawn()

    def _spawn(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="workflow-worker", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> bool:
        """S-7: stop and join the loop; False if it did not end in time."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                log.error("media workflow worker did not stop in time")
                return False
        if self.state not in ("unjoined", "revoked"):
            self.state, self.reason = "stopped", "已停止"
        return True

    def staging(self) -> Staging:
        root = self.identity.staging_dir if self.identity else ""
        return Staging(validate_dir(root), self.settings.staging_cap_gb)

    def _client(self) -> WorkerClient:
        ident = self.identity
        assert ident is not None
        return self._client_factory(
            ident.controller_host, ident.controller_port, ident.credential
        )

    # ── 加入 (R2, R3) ────────────────────────────────────────────────────
    def join(self, code: str, staging_dir: str) -> dict[str, Any]:
        parsed = parse_code(code)
        try:
            root = validate_dir(staging_dir)
            root.mkdir(parents=True, exist_ok=True)
        except StagingError as exc:
            raise JoinError(str(exc)) from None
        except OSError:
            raise JoinError("无法创建暂存目录") from None
        with self._join_lock:
            return self._join(parsed, root)

    def _join(self, parsed: PairingCode, root: Path) -> dict[str, Any]:
        if not self.stop():
            raise JoinError("工作流仍在停止，请稍后再试")
        old = self.identity or WorkerIdentity.load(self.identity_path)
        # the same code again after a lost reply reuses its unconfirmed
        # credential, so /self can prove the pairing (R3)
        same = next(
            (
                found
                for found in (WorkerIdentity.load(self.pending_path), old)
                if found is not None and found.pairing_id == parsed.pairing_id
            ),
            None,
        )
        identity = WorkerIdentity(
            controller_host=parsed.host,
            controller_port=parsed.port,
            pairing_id=parsed.pairing_id,
            credential_id=same.credential_id if same else str(uuid.uuid4()),
            secret=same.secret if same else SecretStr(secrets.token_urlsafe(32)),
            staging_dir=str(root),
            ticket=parsed.ticket,
        )
        self.data_dir.mkdir(parents=True, exist_ok=True)
        identity.save(self.pending_path)  # before sending (R3); `old` stays (IR7)
        self.state, self.reason = "joining", "正在配对"
        client = self._client_factory(
            identity.controller_host, identity.controller_port, identity.credential
        )
        try:
            reply = client.pair(
                identity.pairing_id,
                parsed.ticket,
                identity.credential_id,
                identity.secret,
            )
            worker_id = str(reply["worker_id"])
        except ApiError as exc:
            try:
                worker_id = self._recover(client, exc)
            except JoinError:
                self._resume(old)
                raise
        identity.worker_id, identity.ticket = worker_id, None
        identity.save(self.pending_path)
        os.replace(self.pending_path, self.identity_path)  # IR7: only now
        self.identity, self.worker_id = identity, worker_id
        self.reconciled = False
        self.state, self.reason = "connecting", "正在连接主控"
        log.info("joined the media workflow as worker %s", worker_id)
        self._spawn()
        return self.status()

    def _resume(self, old: WorkerIdentity | None) -> None:
        """IR7: a failed re-join leaves the working identity running."""
        if old is None or not old.worker_id:
            return
        self.identity, self.worker_id = old, old.worker_id
        self.state, self.reason = "connecting", "正在连接主控"
        self._spawn()

    def _recover(self, client: WorkerClient, exc: ApiError) -> str:
        """A lost or refused pair reply: a 200 from /self means paired (R3)."""
        try:
            return str(client.self_view()["worker"]["worker_id"])
        except ApiError:
            pass
        self.state, self.reason = "unjoined", "加入失败"
        if exc.status == 0:
            raise JoinError("无法连接主控，请检查地址与网络") from None
        if exc.status == 401:
            raise JoinError("配对码无效、已使用或已过期") from None
        raise JoinError("主控拒绝了加入（设备已撤销或已退休）") from None

    # ── the loop ─────────────────────────────────────────────────────────
    def _wait(self, seconds: float) -> bool:
        return self._stop.wait(seconds)

    def _retry(self, call: Callable[[], Any]) -> Any:
        """Transient failures back off up to 30 s; a 401 ends the loop."""
        attempt = 0
        while not self._stop.is_set():
            try:
                return call()
            except ApiError as exc:
                if exc.status == 401:
                    self._revoked()
                    return None
                if not exc.transient:
                    raise
                self.reason = "无法连接主控，正在重试"
                delay = exc.retry_after or backoff(attempt, base=self.backoff_base)
                attempt += 1
                if self._wait(delay):
                    return None
        return None

    def _revoked(self) -> None:
        self.state, self.reason = "revoked", "凭据已失效，请重新加入"
        self._stop.set()

    def _run(self) -> None:
        """IR8: only `stop()` or a revoked credential ends the loop; any other
        error is shown, logged, backed off and the session starts again."""
        client = self._client()
        failures = 0
        while not self._stop.is_set():
            try:
                self._session(client)
                return
            except ApiError as exc:
                reason = f"主控拒绝了请求（{exc.code}），正在重试"
            except (StagingError, OSError):
                reason = "暂存目录不可用，正在重试"
            except Exception:
                log.exception("media workflow worker loop failed; retrying")
                reason = "工作流出错（见日志），正在重试"
            log.warning("media workflow worker: %s", reason)
            self.state, self.reason = "failed", reason
            failures += 1
            if self._wait(backoff(failures - 1, base=self.backoff_base)):
                return

    def _session(self, client: WorkerClient) -> None:
        me = self._retry(client.self_view)
        if me is None:
            return
        self.worker_id = me["worker"]["worker_id"]
        staging = self.staging()
        self._refresh(client, staging, me["worker"])
        if self._boot_once(client) is None:
            return
        failures = 0
        while not self._stop.is_set():
            try:
                reply = client.heartbeat(self._beat(staging))
            except ApiError as exc:
                if exc.status == 401:
                    self._revoked()
                    return
                if exc.transient:
                    self.state, self.reason = "connecting", "无法连接主控，正在重试"
                elif exc.status in (413, 422):  # IR15: say what was refused
                    self.state = "failed"
                    self.reason = f"主控拒绝了心跳（{exc.code}），正在重试"
                else:
                    raise
                failures += 1
                if self._wait(backoff(failures - 1, base=self.backoff_base)):
                    return
                continue
            failures = 0
            self._apply(reply)
            if reply.get("reconcile_needed") or not self.reconciled:
                if self._boot_once(client) is None:  # IR6: beats go on meanwhile
                    return
            if self._wait(self.active if self.routes else self.idle):
                return
            if time.monotonic() >= self._probe_due:
                self._refresh(client, staging)

    def _refresh(
        self,
        client: WorkerClient,
        staging: Staging,
        record: Mapping[str, Any] | None = None,
    ) -> None:
        """R4/CX4: probe again; post it only when it differs from the
        controller's record. Without a GPU reading, retry at the next beat."""
        probe = self._probe(self.settings, staging)
        if probe is None:
            self._probe_body, self._probe_due = {}, 0.0
            return
        self._probe_due = time.monotonic() + self.probe_seconds
        self._tiers, self._asr_ready = probe.tiers_ready, probe.asr_ready
        if record is None:
            me = self._retry(client.self_view)
            if me is None:
                return
            record = me["worker"]
        if probe_changed(probe.body, record):
            if self._retry(lambda: client.capabilities(probe.body)) is None:
                return
        self._probe_body = probe.body

    def _boot_once(self, client: WorkerClient) -> bool | None:
        """R6: post the same boot id; True once reconciled (C6), None when the
        loop must end. Unreconciled, the next beat posts it again (IR6)."""
        reply = self._retry(lambda: client.boot(self.boot_id))
        if reply is None:
            return None
        self.reconciled = bool(reply.get("reconciled"))
        return self.reconciled

    def _apply(self, reply: Mapping[str, Any]) -> None:
        with self._lock:
            self.routes = list(reply.get("routes") or [])
            self.paused = bool(reply.get("paused"))
            self.last_heartbeat = datetime.now(timezone.utc)
        if not self.reconciled:
            reason = "等待对账"
        elif not self._probe_body:
            reason = "无法读取本机 GPU（nvidia-smi），正在重试"
        else:
            reason = "在线"
        self.state, self.reason = "running", reason

    def _beat(self, staging: Staging) -> dict[str, Any]:
        live = dict(self._gpu_live())
        body = self._probe_body
        return {
            "boot_id": self.boot_id,
            "version": self.version,
            "protocol": PROTOCOL,
            "paused": False,  # 暂停领取 arrives with the worker monitor (P2-3)
            "gpu": {
                "name": body.get("gpu_model"),
                "vram_mib": body.get("vram_mib"),
                "used_mib": _int(live.get("used_mib")),
                "util": live.get("util"),
                "power_w": live.get("power_w"),
                "hold": live.get("hold", "none"),
            },
            "disk": {
                "free": staging.free(),
                "staging_used": staging.used(),
                "cap": staging.cap,
            },
            "tiers_ready": list(self._tiers),
            "asr_ready": self._asr_ready,
            "staged": list(self.staged),
            "current": None,
            "transfers": [],
        }

    def status(self) -> dict[str, Any]:
        """GET /control/workflow/worker: no secret, no ticket."""
        ident = self.identity
        with self._lock:
            routes = list(self.routes)
            seen = self.last_heartbeat
        return {
            "state": self.state,
            "reason": self.reason,
            "controller": f"{ident.controller_host}:{ident.controller_port}"
            if ident
            else None,
            "worker_id": self.worker_id,
            "credential": None
            if ident is None
            else ("revoked" if self.state == "revoked" else "valid"),
            "staging_dir": ident.staging_dir if ident else self.settings.staging_dir,
            "staging_cap_gb": self.settings.staging_cap_gb,
            "last_heartbeat": seen.isoformat() if seen else None,
            "reconciled": self.reconciled,
            "routes": routes,
            "paused": self.paused,
        }


def _int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and value >= 0 else None


def start_worker_host(
    settings: WorkflowSettings, config_dir: Path | None
) -> WorkerHost | None:
    """A worker's host (role=worker and enabled); None without a data dir."""
    if not settings.enabled or settings.role != "worker":
        return None
    if settings.data_dir:
        data_dir = Path(settings.data_dir)
    elif config_dir is not None:
        data_dir = config_dir / "workflow"
    else:
        return None
    host = WorkerHost(settings, data_dir)
    host.start()
    return host
