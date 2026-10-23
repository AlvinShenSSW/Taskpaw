"""P2-2 (#293): a controller host and enrolled remote workers over the real
worker API (FastAPI TestClient), through the public routes only."""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi.testclient import TestClient
from pydantic import SecretStr

from taskpaw_v3.agent.server.worker_api import create_worker_app
from taskpaw_v3.workflow import devices
from taskpaw_v3.workflow.auth import AuthProof, authenticate
from taskpaw_v3.workflow.executors import SyntheticExecutor
from taskpaw_v3.workflow.fleet_live import PROTOCOL, FleetLive
from taskpaw_v3.workflow.local_runtime import LocalRuntime

VERSION = "9.9.9"


class StepClock:
    """Real time plus an offset that only grows (the store refuses a step back)."""

    def __init__(self) -> None:
        self.offset = timedelta()

    def now(self) -> datetime:
        return datetime.now(timezone.utc) + self.offset

    def advance(self, delta: timedelta) -> None:
        self.offset += delta


@dataclass
class Host:
    """What the worker API reads from the controller's workflow host."""

    runtime: LocalRuntime | None
    fleet: FleetLive | None = None
    transfers: Any = None
    dispatcher: Any = None
    state: str = "running"
    reason: str = ""
    # P2-3: 停止并重新排队's attempts, and the finishes the API resolved
    cancels: set[str] = field(default_factory=set)
    resolved: list[tuple[str, str, bool]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.fleet is None and self.runtime is not None:
            self.fleet = FleetLive(self.runtime.clock.now(), VERSION)

    def remote_resolved(self, rt: Any, job: Any, name: str, outcome: Any) -> None:
        self.resolved.append((job.film_id, job.stage, outcome.ok))


def client(host: Host) -> TestClient:
    return TestClient(create_worker_app(lambda: host))


def parse(code: str) -> tuple[str, str]:
    """(pairing_id, ticket) of a `TP1:<host>:<port>:<pairing>:<ticket>` code."""
    _, _, _, pairing_id, ticket = code.rsplit(":", 4)
    return pairing_id, ticket


def new_secret() -> str:
    return secrets.token_urlsafe(32)


@dataclass
class Joined:
    worker_id: str
    credential_id: str
    secret: str = field(repr=False)
    pairing: dict[str, Any] = field(default_factory=dict)

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.credential_id}.{self.secret}"}

    def auth(self, rt: LocalRuntime) -> AuthProof:
        return authenticate(rt.store, self.credential_id, SecretStr(self.secret))


def add(rt: LocalRuntime, name: str = "5060 Ti-1") -> dict[str, Any]:
    return devices.add_device(rt, name, "192.0.2.31", host="127.0.0.1", port=5682)


def pair_body(code: str, credential_id: str, secret: str) -> dict[str, str]:
    pairing_id, ticket = parse(code)
    return {
        "pairing_id": pairing_id,
        "ticket": ticket,
        "credential_id": credential_id,
        "secret": secret,
    }


def join(api: TestClient, rt: LocalRuntime, name: str = "5060 Ti-1") -> Joined:
    issued = add(rt, name)
    credential_id, secret = str(uuid.uuid4()), new_secret()
    response = api.post(
        "/worker/v1/pair", json=pair_body(issued["code"], credential_id, secret)
    )
    assert response.status_code == 200, response.text
    assert response.json()["worker_id"] == issued["worker_id"]
    return Joined(issued["worker_id"], credential_id, secret, response.json())


def profiles(
    stages: tuple[str, ...] = ("restore", "asr"), limit: tuple[int, int] = (1920, 1080)
) -> list[dict[str, Any]]:
    values = []
    for stage in stages:
        tools = SyntheticExecutor(stage).tools()  # type: ignore[arg-type]
        values.append(
            {
                "profile_id": tools.profile,
                "profile_revision": tools.profile_revision,
                "tool": tools.tool,
                "tool_version": tools.tool_version,
                "model": tools.model,
                "model_version": tools.model_version,
                "allowed_stages": [stage],
                "max_width": limit[0],
                "max_height": limit[1],
            }
        )
    return values


def probe(worker: str = "w", *, vram: int = 16311, **kw: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "source": "nvidia-smi",
        "cpu": "synthetic",
        "ram_mib": 32768,
        "gpu_model": "NVIDIA GeForce RTX 5060 Ti",
        "vram_mib": vram,
        "device_identity": f"device-{worker}",
        "gpu_identity": f"gpu-{worker}",
        "free_disk_bytes": 900 * 10**9,
        "model_ready": True,
        "profiles": profiles(),
    }
    values.update(kw)
    return values


def boot_id() -> str:
    return str(uuid.uuid4())


def beat(boot: str, **kw: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "boot_id": boot,
        "version": VERSION,
        "protocol": PROTOCOL,
        "paused": False,
        "gpu": {
            "name": "NVIDIA GeForce RTX 5060 Ti",
            "vram_mib": 16311,
            "hold": "none",
        },
        "disk": {"free": 900 * 10**9, "staging_used": 0, "cap": 500 * 10**9},
        "tiers_ready": ["standard", "good", "high"],  # OD7: no asr_ready
        "staged": [],
        "current": None,
        "transfers": [],
    }
    values.update(kw)
    return values


def version(rt: LocalRuntime, worker_id: str) -> int:
    return rt.store.get_worker(rt.operator, worker_id).worker.version


def enrol(
    api: TestClient,
    rt: LocalRuntime,
    name: str = "5060 Ti-1",
    *,
    restore: bool = True,
    boot: str | None = None,
) -> tuple[Joined, str]:
    """Pair, probe, 核验并启用 and boot one remote worker over the API."""
    joined = join(api, rt, name)
    posted = api.post(
        "/worker/v1/capabilities", json=probe(joined.worker_id), headers=joined.headers
    )
    assert posted.status_code == 200, posted.text
    devices.approve(
        rt,
        joined.worker_id,
        version(rt, joined.worker_id),
        restore=restore,
    )
    boot = boot or boot_id()
    booted = api.post("/worker/v1/boot", json={"boot_id": boot}, headers=joined.headers)
    reply = booted.json()
    assert (reply["status"], reply["reconciled"]) == ("reconciled", True), reply
    return joined, boot
