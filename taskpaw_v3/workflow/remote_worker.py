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
IR6). An ordinary error is shown, backed off and retried; only `stop()`, or
a refusal of every identity the worker holds, ends the loop (IR8, CX14).
P2-2 runs no stage, claims nothing.

The identity file holds the secret like the local identity file (home LAN,
single user, P0); it is written atomically and never logged, and `repr`
hides it (S-6).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, cast

from pydantic import SecretStr

from taskpaw_v3.core.config import WorkflowSettings
from taskpaw_v3.core.net import bind_is_global, bind_is_wildcard
from taskpaw_v3.core.state import atomic_json

from .executors import ExecRuntime, StageExecutor
from .fleet_live import PROTOCOL
from .keep_awake import KeepAwake
from .local_gpu import LocalGpu
from .remote_dispatch import (
    HELD,
    UNSETTLED,
    RemoteDispatcher,
    WorkerRuntime,
    worker_busy,
)
from .staging import Staging, StagingError, validate_dir
from .worker_client import ApiError, WorkerClient, backoff

log = logging.getLogger("taskpaw.workflow.remote_worker")

IDENTITY_NAME = "worker-identity.json"
STATE_NAME = "worker-state.json"  # P2-3 §11 IR10: 暂停领取 across restarts
PENDING_NAME = "worker-identity.pending.json"  # IR7: a pairing not yet confirmed
UNSAVED_REASON = "身份文件未保存，重试中"
REVOKED_REASON = "凭据已撤销，请重新加入"
REFUSED = frozenset({401, 403})  # CX14: this credential is no longer accepted
PENDING_REASON = "新的配对待确认"  # CX13: a pending pairing not yet settled
ACTIVE_SECONDS = 5.0  # with an open route (R7)
IDLE_SECONDS = 15.0
PENDING_SECONDS = 60.0  # CX13: how often an unsettled pairing is checked again
PENDING_TIMEOUT = 5.0  # CX13: one check never holds the loop longer
PROBE_SECONDS = 60.0  # CX4: tier and ASR readiness follow the disk
TIERS = ("standard", "good", "high")
_TICKET = re.compile(r"[A-Za-z0-9_-]{32,256}\Z")


class JoinError(ValueError):
    """加入 refused; the message is UI text and never echoes the code."""


class _Switch(Exception):
    """CX13: a pending identity was confirmed mid-session; run on it."""


class _Busy(Exception):
    """P2-3 §11 CX1/CX3: the previous session's dispatcher still has live
    processes; no new one runs until they are gone (retried with backoff)."""


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
    def read(cls, path: Path) -> WorkerIdentity | None:
        """None when absent; `ValueError` when the file is not an identity
        (an `OSError` other than a missing file propagates). P2-4 §9 CX16:
        the settings guard reads it strictly — unreadable is not unpaired."""
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            data = json.loads(text)
            data["secret"] = SecretStr(data["secret"])
            data["ticket"] = SecretStr(data["ticket"]) if data.get("ticket") else None
            return cls(**data)
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError("not a worker identity") from exc

    @classmethod
    def load(cls, path: Path) -> WorkerIdentity | None:
        try:
            return cls.read(path)
        except ValueError:
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
ExecutorFactory = Callable[[WorkerRuntime], Sequence[StageExecutor]]


def worker_executors(
    settings: WorkflowSettings, runtime: ExecRuntime, gpu: LocalGpu | None
) -> list[StageExecutor]:
    """The worker's restore and ASR executors from its tool settings — the
    same for the probe (profiles) and the dispatcher (runs), so a claim's
    tools always match the approved profile."""
    from .exec_asr import AsrExecutor
    from .exec_restore import RestoreExecutor, resolve_gpu_preset

    executors: list[StageExecutor] = []
    if settings.jasna_exe_path:
        auto = settings.ltx_gpu == "auto"
        preset = resolve_gpu_preset(gpu) if auto and gpu else settings.ltx_gpu
        try:
            executors.append(
                RestoreExecutor(
                    runtime,
                    settings.jasna_exe_path,
                    preset,
                    preset_auto=auto,
                    ffprobe=settings.ffprobe_path,
                )
            )
        except ValueError:
            log.warning("worker restore: invalid Jasna settings")
    if settings.whisperjav_exe_path:
        try:
            executors.append(AsrExecutor(runtime, settings.whisperjav_exe_path))
        except ValueError:
            log.warning("worker ASR: invalid WhisperJAV settings")
    return executors


def probe_body(
    executors: Sequence[StageExecutor], gpu: LocalGpu, staging: Staging
) -> dict[str, Any]:
    """The controller's `ProbeBody` for these executors on this GPU."""
    from .local_gpu import machine_identity
    from .models import ProfileCapability

    profiles = []
    digests = []
    for executor in executors:
        tools = executor.tools()
        digests.append(f"{tools.profile}:{tools.config_digest}")
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
    fingerprint = hashlib.sha256("|".join(sorted(digests)).encode()).hexdigest()
    return {
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
        "config_digest": fingerprint,  # CX9: a setting change needs 核验
    }


def default_probe(settings: WorkflowSettings, staging: Staging) -> Probe | None:
    """GPU and identity, free disk, and the restore/ASR executors' profiles;
    the executors are built only for `tools()` and `max_resolution()`."""
    from taskpaw_v3.monitors.plugins import jasna

    from .exec_restore import RestoreExecutor
    from .local_gpu import query_gpu

    gpu = query_gpu()
    if gpu is None:
        return None
    unbound = cast(ExecRuntime, None)  # never used by tools()/max_resolution()
    executors = worker_executors(settings, unbound, gpu)
    restore = next((e for e in executors if isinstance(e, RestoreExecutor)), None)
    tiers: tuple[str, ...] = ()
    if restore is not None and Path(settings.jasna_exe_path or "").is_file():
        fast = restore.cfg.ltx_fast and jasna.fast_supported(jasna.query_gpus())
        folder = str(Path(settings.jasna_exe_path or "").parent)
        tiers = tuple(
            tier for tier in TIERS if not jasna.ltx_missing_files(folder, tier, fast)
        )
    asr_ready = (
        any(e.stage == "asr" for e in executors)
        and Path(settings.whisperjav_exe_path or "").is_file()
    )
    return Probe(probe_body(executors, gpu, staging), tiers, asr_ready)


def probe_changed(probe: Mapping[str, Any], record: Mapping[str, Any]) -> bool:
    """R4: identity, VRAM, the proposed profiles or the executors' config
    fingerprint differ from the record."""
    cap = record.get("capability") or {}
    old = cap.get("probed") or {}
    if not old:
        return True
    # CX9: a stored probe without a fingerprint compares as changed (once)
    keys = ("device_identity", "gpu_identity", "vram_mib", "config_digest")

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
    from taskpaw_v3.core.power import BUSY_GPU_PCT

    sample = gpu_sample() or {}
    holder = gpu_lease.holder()
    util = sample.get("util_pct")
    if holder is not None:
        hold = "workflow" if holder[0] == "workflow" else "manual"
    elif isinstance(util, (int, float)) and util >= BUSY_GPU_PCT:
        hold = "unknown"  # 「GPU 有未知占用」: busy with no lease holder (R6)
    else:
        hold = "none"
    return {
        "used_mib": sample.get("mem_used_mb"),
        "util": util,
        "power_w": sample.get("power_w"),
        "hold": hold,
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
        pending_seconds: float = PENDING_SECONDS,
        staging_free: Callable[[Path], int] | None = None,
        executor_factory: ExecutorFactory | None = None,
        lease: Any = None,
        power: Callable[[], Any] | None = None,
        keep_awake: KeepAwake | None = None,
        dispatch_poll: float = 5.0,
        stop_settle: float = 30.0,
    ) -> None:
        """`staging_free`: the staging disk's free bytes (tests inject it so
        no result depends on the host's real disk)."""
        from taskpaw_v3 import __version__

        self.settings, self.data_dir = settings, data_dir
        self.identity_path = data_dir / IDENTITY_NAME
        self.pending_path = data_dir / PENDING_NAME
        self.probe_seconds, self.pending_seconds = probe_seconds, pending_seconds
        self._pending_due = 0.0  # monotonic; the next check of an unsettled pairing
        self._probe_due = 0.0  # monotonic; 0: probe at the next beat
        self._join_lock = threading.Lock()  # IR7: one join at a time
        self._closed = False  # CX10: set by stop(); nothing spawns after it
        self._probe, self._gpu_live = probe, gpu_live
        self._staging_free = staging_free
        # P2-3: remote execution (R6–R14)
        self._executor_factory = executor_factory or (
            lambda runtime: worker_executors(settings, runtime, runtime.gpu)
        )
        self._lease, self._power = lease, power
        self.keep_awake = keep_awake or KeepAwake()
        self.dispatch_poll, self.stop_settle = dispatch_poll, stop_settle
        self.dispatcher: RemoteDispatcher | None = None
        # §11 CX3: the session the dispatcher is bound to (credential, staging)
        self._dispatcher_key: tuple[str, ...] | None = None
        self._probe_gpu: LocalGpu | None = None  # §11 CX4: the last GPU read
        self.state_path = data_dir / STATE_NAME
        # R12: 暂停领取 on the worker's own monitor; §11 IR10: kept on disk
        self.local_paused = _load_paused(self.state_path)
        self._hold = "none"
        self._client_factory = client_factory
        self.version = version or __version__
        self.active, self.idle, self.backoff_base = (
            active_seconds,
            idle_seconds,
            backoff_base,
        )
        # R6; §11 IR1: a new one with every new dispatcher (a new session
        # identity or after a revoke), so the boot settles what came before
        self.boot_id = str(uuid.uuid4())
        self.state, self.reason = "unjoined", "尚未加入媒体工作流"
        self.identity: WorkerIdentity | None = None
        self._unsaved = False  # a paired identity not yet in the identity file
        self._pending_check: WorkerIdentity | None = None  # not yet settled
        self._refused: set[str] = set()  # CX14: credential ids refused 401/403
        self.worker_id: str | None = None
        self.routes: list[dict[str, Any]] = []
        self.paused = False
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
        self._pending_check = WorkerIdentity.load(self.pending_path)
        if self.identity is None and self._pending_check is None:
            return
        self.worker_id = self.identity.worker_id if self.identity else None
        self._spawn()

    def _spawn(self) -> None:
        if self._closed:  # CX10: shut down while a join was in flight
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="workflow-worker", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> bool:
        """S-7: stop for good and join the loop; False if it did not end in
        time. An in-flight join is waited for (CX10): it can no longer spawn."""
        self._closed = True
        self._stop.set()
        if not self._join_lock.acquire(timeout=timeout):
            log.error("media workflow join did not return in time")
            return False
        try:
            halted = self._halt(timeout)
            return self._stop_dispatcher(timeout) and halted
        finally:
            self._join_lock.release()

    def _stop_dispatcher(self, timeout: float = 60.0) -> bool:
        dispatcher = self.dispatcher
        return dispatcher is None or dispatcher.stop(timeout)

    def set_paused(self, paused: bool) -> None:
        """R12: 暂停领取 — no claim; in-flight work, uploads and prefetch go on.
        The heartbeat reports it; the controller treats it like 停领 (S8).
        §11 IR10: the controller's own 暂停派发 stays in force, and the choice
        is written to `worker-state.json` (a failed write keeps it in memory)."""
        self.local_paused = bool(paused)
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            atomic_json(self.state_path, {"paused": self.local_paused})
        except OSError:
            log.warning("cannot save 暂停领取; it lasts until the restart")
        dispatcher = self.dispatcher
        if dispatcher is not None:
            dispatcher.observe(
                {"routes": self.routes, "paused": self.paused},
                reconciled=self.reconciled,
                tiers=self._tiers,
                asr_ready=self._asr_ready,
                hold=self._hold,
                paused=self.local_paused,
            )
            if self.local_paused:  # §11 CX10: no claim starts after this returns
                dispatcher.acknowledge_pause()

    def _halt(self, timeout: float) -> bool:
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
        return Staging(
            validate_dir(root),
            self.settings.staging_cap_gb,
            free_bytes=self._staging_free,
        )

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
        if self._closed:
            raise JoinError("工作流已停止")
        # CX11: everything that can fail on disk runs before the loop is
        # stopped, so a refused write leaves the working worker untouched
        try:
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
        except OSError:
            log.exception("cannot write the pending worker identity")
            raise JoinError("无法写入身份文件") from None
        if not self._halt(30.0):
            raise JoinError("工作流仍在停止，请稍后再试")
        self._pending_check = None  # CX13: this join's pending file supersedes it
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
                # CX16: a first join is held only while the pair may have landed
                self._resume(old, identity, held=exc.transient)
                raise
        identity.worker_id, identity.ticket = worker_id, None
        # the old credential is gone on the controller: from here the worker
        # runs on this identity even if the file cannot be written yet
        self._pending_check = None
        self._refused.discard(identity.credential_id)  # CX14: accepted again
        self.identity, self.worker_id = identity, worker_id
        self._unsaved = True
        self.reconciled = False
        self.state, self.reason = "connecting", "正在连接主控"
        self._promote()  # IR7: only now; a failure is retried at each beat
        log.info("joined the media workflow as worker %s", worker_id)
        if self._closed:  # CX10: the pairing stands; nothing starts after stop()
            self.state, self.reason = "stopped", "已停止"
        self._spawn()
        return self.status()

    def _promote(self) -> bool:
        """Write the paired identity and rename it over the identity file;
        False (shown, retried at the next beat) when the disk refuses."""
        identity = self.identity
        if not self._unsaved or identity is None:
            return True
        try:
            identity.save(self.pending_path)
            os.replace(self.pending_path, self.identity_path)
        except OSError:
            log.warning("cannot save the worker identity; retrying at the next beat")
            self.reason = UNSAVED_REASON
            return False
        self._unsaved = False
        return True

    def _resume(
        self,
        old: WorkerIdentity | None,
        pending: WorkerIdentity,
        *,
        held: bool = True,
    ) -> None:
        """IR7: a failed re-join leaves the working identity running. CX14:
        the pair may still have landed (a lost reply), so its identity is held
        as pending and settled by the loop like one found at start. CX16: a
        first join (no working identity) is held the same way; the loop runs
        with no saved identity until the pending one settles; `held` is False
        when the controller refused the pair itself, so nothing can have
        landed."""
        if old is not None and old.worker_id:
            self.identity, self.worker_id = old, old.worker_id
        elif not held:
            return
        else:
            self.identity, self.worker_id = None, None
        self._pending_check, self._pending_due = pending, 0.0
        if self._closed:  # CX10
            self.state, self.reason = "stopped", "已停止"
            return
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

    def _pause(self, seconds: float) -> bool:
        """A wait inside a session. CX13/CX14: an unsettled pending identity
        is checked whenever due, so neither a healthy nor an unreachable saved
        identity ever holds it back; a confirmation raises `_Switch`."""
        pending = self._pending_check
        if pending is not None and time.monotonic() >= self._pending_due:
            self._pending_due = time.monotonic() + self.pending_seconds
            if self._check_pending(pending) == "promoted":
                raise _Switch
        return self._wait(seconds)

    def _retry(self, call: Callable[[], Any]) -> Any:
        """Transient failures back off up to 30 s; any other error, a refusal
        included, goes to the loop."""
        attempt = 0
        while not self._stop.is_set():
            try:
                return call()
            except ApiError as exc:
                if not exc.transient:
                    raise
                self.reason = "无法连接主控，正在重试"
                delay = exc.retry_after or backoff(attempt, base=self.backoff_base)
                attempt += 1
                if self._pause(delay):
                    return None
        return None

    def _revoked(self) -> None:
        """R8/crash matrix 10: the credential is gone — the running Job is
        cancelled and settled, its outcome kept; nothing more is sent with
        it. A join afterwards builds a new dispatcher with a new boot id
        (§11 IR1), which settles or collects the kept attempt file. A Job
        whose processes outlive the stop keeps this dispatcher, and the GPU,
        until a later stop confirms them gone (§11 CX1)."""
        # first: 「凭据已撤销」 means the Job settled
        if self._stop_dispatcher():
            self.dispatcher = None
        self.state, self.reason = "revoked", REVOKED_REASON
        self._stop.set()

    def _usable(self, identity: WorkerIdentity | None) -> bool:
        return identity is not None and identity.credential_id not in self._refused

    def _run(self) -> None:
        try:
            self._loop()
        finally:
            self.keep_awake.hold(False)  # R11: cleared by the thread that set it

    def _loop(self) -> None:
        """IR8, CX14 — one rule. A 401/403 refuses only the identity it was
        sent with and a transient error refuses none; the loop ends for good
        only when every identity the worker holds is refused, or on `stop()`.
        Until then it retries with backoff: a usable saved identity runs while
        a pending one waits (CX13), a refused one hands over to the pending
        one, and a confirmed pending one is promoted and run. Any other error
        is shown, logged, backed off and the session starts again."""
        failures = 0
        while not self._stop.is_set():
            current, pending = self.identity, self._pending_check
            usable = self._usable(current)
            try:
                if pending is not None and (
                    not usable or time.monotonic() >= self._pending_due
                ):
                    self._pending_due = time.monotonic() + self.pending_seconds
                    if self._check_pending(pending) != "unsettled":
                        continue  # promoted or dropped: choose again
                    if not usable:
                        self.state = "connecting"
                        self.reason = "无法连接主控，正在重试"
                        failures += 1
                        if self._wait(backoff(failures - 1, base=self.backoff_base)):
                            return
                        continue
                if current is None:  # CX16: a first join that never landed
                    self.state, self.reason = "unjoined", "加入失败，请重新加入"
                    self._stop.set()
                    return
                if not usable:
                    self._revoked()  # every identity it held was refused
                    return
                self._session(self._client())
                failures = 0
                continue
            except _Switch:  # CX13: a pending pairing was confirmed
                failures = 0
                continue
            except ApiError as exc:
                if exc.status in REFUSED and current is not None:
                    self._refused.add(current.credential_id)
                    continue
                reason = f"主控拒绝了请求（{exc.code}），正在重试"
            except _Busy as exc:
                reason = f"{exc.args[0] if exc.args else UNSETTLED}，正在重试"
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

    def _check_pending(self, pending: WorkerIdentity) -> str:
        """One bounded `/self` with the pending credential: "promoted" (200),
        "dropped" (refused) or "unsettled" (any other answer; it stays)."""
        client = self._client_factory(
            pending.controller_host,
            pending.controller_port,
            pending.credential,
            timeout=PENDING_TIMEOUT,
        )
        try:
            me = client.self_view()
        except ApiError as exc:
            if exc.status not in REFUSED:
                return "unsettled"
            self._pending_check = None
            self._drop_pending(pending)
            return "dropped"
        pending.worker_id, pending.ticket = me["worker"]["worker_id"], None
        self._pending_check = None
        self.identity, self.worker_id = pending, pending.worker_id
        self._unsaved = True
        self.reconciled, self._probe_due = False, 0.0
        self._promote()
        return "promoted"

    def _drop_pending(self, checked: WorkerIdentity) -> None:
        current = WorkerIdentity.load(self.pending_path)
        if current is not None and current.credential_id != checked.credential_id:
            return  # a join wrote a newer one meanwhile
        try:
            self.pending_path.unlink(missing_ok=True)
        except OSError:
            log.warning("cannot remove a refused pending worker identity")

    def _session(self, client: WorkerClient) -> None:
        """Runs until `stop()`; a refusal or other error is raised to the loop,
        a confirmed pending identity raises `_Switch`."""
        me = self._retry(client.self_view)
        if me is None:
            return
        self.worker_id = me["worker"]["worker_id"]
        staging = self.staging()
        self._refresh(client, staging, me["worker"])
        dispatcher = self._ensure_dispatcher(staging)
        if self._retry(lambda: dispatcher.recover(client)) is None:
            return
        if self._boot_once(client) is None:
            return
        dispatcher.start()
        failures = 0
        while not self._stop.is_set():
            try:
                reply = client.heartbeat(self._beat(staging))
            except ApiError as exc:
                if exc.transient:
                    self.state, self.reason = "connecting", "无法连接主控，正在重试"
                elif exc.status in (413, 422):  # IR15: say what was refused
                    self.state = "failed"
                    self.reason = f"主控拒绝了心跳（{exc.code}），正在重试"
                else:
                    raise
                failures += 1
                if self._pause(backoff(failures - 1, base=self.backoff_base)):
                    return
                continue
            failures = 0
            self._promote()
            self._apply(reply)
            # R11: from this long-lived thread only
            self.keep_awake.hold(bool(self.routes) or dispatcher.claimed())
            if reply.get("reconcile_needed") or not self.reconciled:
                if self._boot_once(client) is None:  # IR6: beats go on meanwhile
                    return
            if self._pause(self.active if self.routes else self.idle):
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
            self._tiers, self._asr_ready = (), False  # CX8: nothing is ready
            return
        self._probe_due = time.monotonic() + self.probe_seconds
        self._tiers, self._asr_ready = probe.tiers_ready, probe.asr_ready
        self._probe_gpu = _gpu_of(probe.body)
        self._bind_gpu()
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
        """R6/R8: post the same boot id with the local attempt files; True
        once reconciled (C6), None when the loop must end. Unreconciled, the
        next beat posts it again (IR6)."""
        dispatcher = self.dispatcher
        reports = dispatcher.reports() if dispatcher is not None else []
        reply = self._retry(lambda: client.boot(self.boot_id, reports))
        if reply is None:
            return None
        self.reconciled = bool(reply.get("reconciled"))
        if dispatcher is not None:
            dispatcher.booted(reply)
        return self.reconciled

    def _ensure_dispatcher(self, staging: Staging) -> RemoteDispatcher:
        """The dispatcher of this session. §11 CX3: one bound to another
        identity or staging folder (a re-join, a confirmed pending pairing),
        or one already stopped (a revoke), is quiesced first — its Job is
        stopped and settled — and a new one is built with this session's
        client, staging and a new boot id (IR1). Live processes it cannot
        settle raise `_Busy`: nothing new runs beside them (CX1)."""
        ident = self.identity
        assert ident is not None
        key = (
            ident.credential_id,
            ident.controller_host,
            str(ident.controller_port),
            str(staging.root),
        )
        old = self.dispatcher
        if old is not None and (old.halted or self._dispatcher_key != key):
            if not old.stop(self.stop_settle):
                raise _Busy(HELD if old.reason == HELD else UNSETTLED)
            self.dispatcher = old = None
        if old is None:
            self.boot_id, self.reconciled = str(uuid.uuid4()), False
            gpu = self._probe_gpu
            runtime = WorkerRuntime(staging.root, gpu)
            # §11 CX4: no executors before a GPU reading; bound once it comes
            executors = self._executor_factory(runtime) if gpu is not None else []
            old = self.dispatcher = RemoteDispatcher(
                staging,
                self._bound_client(ident),
                {e.stage: e for e in executors},
                runtime,
                boot_id=self.boot_id,
                lease=self._lease if self._lease is not None else _gpu_lease(),
                power=self._power,
                poll=self.dispatch_poll,
                stop_settle=self.stop_settle,
                backoff_base=self.backoff_base,
            )
            self._dispatcher_key = key
        return old

    def _bound_client(self, ident: WorkerIdentity) -> Callable[[], WorkerClient]:
        """§11 CX3: the dispatcher's calls always use the identity of the
        session that built it, never one switched in later."""
        return lambda: self._client_factory(
            ident.controller_host, ident.controller_port, ident.credential
        )

    def _bind_gpu(self) -> None:
        """§11 CX4: a GPU read after the dispatcher was built (or a changed
        card) rebuilds its executors, only between attempts; tried again at
        every probe until it can."""
        dispatcher, gpu = self.dispatcher, self._probe_gpu
        if dispatcher is None or gpu is None or dispatcher.halted:
            return
        if dispatcher.runtime.gpu == gpu and dispatcher.executors:
            return
        if dispatcher.rebind(gpu, self._executor_factory):
            log.info("worker executors bound to %s", gpu.name)

    def _apply(self, reply: Mapping[str, Any]) -> None:
        with self._lock:
            self.routes = list(reply.get("routes") or [])
            self.paused = bool(reply.get("paused"))
            self.last_heartbeat = datetime.now(timezone.utc)
        dispatcher = self.dispatcher
        if dispatcher is not None:
            dispatcher.observe(
                reply,
                reconciled=self.reconciled,
                tiers=self._tiers,
                asr_ready=self._asr_ready,
                hold=self._hold,
                paused=self.local_paused,
            )
        if self._unsaved:
            reason = UNSAVED_REASON
        elif self._pending_check is not None:
            reason = PENDING_REASON
        elif not self.reconciled:
            reason = "等待对账"
        elif not self._probe_body:
            reason = "无法读取本机 GPU（nvidia-smi），正在重试"
        else:
            reason = "在线"
        self.state, self.reason = "running", reason

    def _beat(self, staging: Staging) -> dict[str, Any]:
        live = dict(self._gpu_live())
        self._hold = str(live.get("hold", "none"))
        body = self._probe_body
        dispatcher = self.dispatcher
        return {
            "boot_id": self.boot_id,
            "version": self.version,
            "protocol": PROTOCOL,
            "paused": self.local_paused,  # R12
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
            "staged": dispatcher.staged() if dispatcher else [],
            "current": dispatcher.current() if dispatcher else None,
            "transfers": dispatcher.transfers() if dispatcher else [],
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
            "paused": self.local_paused,  # R12: 暂停领取 on this worker
            "controller_paused": self.paused,  # 暂停派发 on the controller
            "pending": self._pending_check is not None,
            **self._monitor(),
        }

    def summary(self) -> dict[str, Any]:
        """R16 `/status.workflow_worker`: no secret, no path, no film name."""
        with self._lock:
            routes = len(self.routes)
            seen = self.last_heartbeat
        dispatcher = self.dispatcher
        current = dispatcher.current() if dispatcher else None
        connected = (
            self.state == "running"
            and seen is not None
            and (datetime.now(timezone.utc) - seen).total_seconds() < 60
        )
        return {
            "state": self.state,
            "reason": self.reason,
            "connected": connected,
            "routes": routes,
            "current": None
            if current is None
            else {
                "film_id": current["film_id"],
                "stage": current["stage"],
                "phase": current["phase"],
            },
            "paused": self.local_paused,
            # P2-4 R9: the controller's 判定已停止 reads this on /status
            "busy": self._busy(dispatcher),
        }

    def _busy(self, dispatcher: RemoteDispatcher | None) -> bool:
        """§9 CX1/CX13: the one `worker_busy` rule; before a dispatcher exists
        (no controller reached since a restart) the attempts folder on disk
        decides alone — unsettled work whose children may still run."""
        ident = self.identity
        root = ident.staging_dir if ident else self.settings.staging_dir
        return worker_busy(Path(root) if root else None, dispatcher)

    def _monitor(self) -> dict[str, Any]:
        """§6: films, the running step, transfers, staging and the GPU hold."""
        dispatcher = self.dispatcher
        staging: dict[str, Any] = {  # bytes, as the dispatcher's staging reports
            "used": None,
            "cap": self.settings.staging_cap_gb * 10**9,
        }
        if dispatcher is not None:
            try:
                staging = {
                    "used": dispatcher.staging.used(),
                    "cap": dispatcher.staging.cap,
                }
            except OSError:
                pass
        return {
            "films": dispatcher.films() if dispatcher else [],
            "current": _monitor_current(dispatcher.current() if dispatcher else None),
            "transfers": dispatcher.transfers() if dispatcher else [],
            "dispatch_reason": dispatcher.reason if dispatcher else None,
            # §11 IR3: what needs the operator (a refused failed finish)
            "dispatch_notice": dispatcher.notice if dispatcher else None,
            "staging": staging,
            "gpu": {
                "name": self._probe_body.get("gpu_model"),
                "vram_mib": self._probe_body.get("vram_mib"),
                "hold": self._hold,
            },
        }


def _int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and value >= 0 else None


def _gpu_of(body: Mapping[str, Any]) -> LocalGpu | None:
    if not body.get("gpu_identity"):
        return None
    return LocalGpu(
        str(body.get("gpu_model")),
        str(body.get("gpu_identity")),
        int(body.get("vram_mib") or 0),
    )


def _load_paused(path: Path) -> bool:
    """§11 IR10: the saved 暂停领取 (absent or unreadable: not paused)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        log.warning("unreadable worker state ignored")
        return False
    return isinstance(data, dict) and data.get("paused") is True


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


def _gpu_lease() -> Any:
    from taskpaw_v3.core import gpu_lease

    return gpu_lease


_STEP_KEYS = ("console", "params", "timing")  # as the board puts them on the step


def _monitor_current(current: dict[str, Any] | None) -> dict[str, Any] | None:
    """The running stage for the worker's 媒体处理 tab, with its step shaped
    like the board's running 修复 step, so the console reuses the same
    LtxProgress, 本片参数 and 用时 cards (§6)."""
    if current is None:
        return None
    ltx = current.get("ltx") if isinstance(current.get("ltx"), dict) else None
    detail = ltx or {}
    started = datetime.now(timezone.utc).timestamp() - float(current["elapsed_s"])
    step = {
        "key": current["stage"],
        "state": "running",
        "note": None,
        "progress": current.get("progress") or None,
        "ltx": None
        if ltx is None
        else {k: v for k, v in ltx.items() if k not in _STEP_KEYS},
        "current_file": detail.get("current_file"),
        "started_at": datetime.fromtimestamp(started, timezone.utc).isoformat(),
        "console": detail.get("console"),
        "params": detail.get("params"),
        "timing": detail.get("timing"),
    }
    return {**current, "step": step, "started_at": step["started_at"]}
