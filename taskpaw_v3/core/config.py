"""Config models for the V3 agent and Hub (design §7).

Pydantic for server-side validation; YAML on disk. Cross-platform default paths
live in the launcher/service entrypoints, not here, so these models stay pure
and testable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field, field_validator


def _valid_port(p: int) -> int:
    if not (1 <= p <= 65535):
        raise ValueError("port must be between 1 and 65535")
    return p


class AgentConfig(BaseModel):
    """A V3 agent's local config (`agent.yaml`)."""

    server_id: str = Field(..., min_length=1)
    machine: str = Field(..., min_length=1)
    # Network-facing read API (Hub polls this). Secure default = loopback;
    # the operator must set this to the LAN nic IP to expose it to the Hub
    # (constitution §2: no public/WAN exposure — never default to 0.0.0.0).
    bind_host: str = "127.0.0.1"
    bind_port: int = 5680
    # Local control API (start/stop, edit config) — loopback only.
    control_host: str = "127.0.0.1"
    control_port: int = 5681
    # Bearer token; empty = auth disabled (V2 parity).
    api_token: str = ""
    # Monitor instances. Opaque dicts here; #17 defines the plugin schema.
    monitors: list[dict[str, Any]] = Field(default_factory=list)
    # Auto-run a host_metrics self-monitor on this agent (§5b: every agent).
    host_metrics: bool = True

    @field_validator("server_id", "machine")
    @classmethod
    def _ident_not_blank(cls, v: str) -> str:
        # Stable identity strings — strip and reject whitespace-only so they can't
        # produce nonsensical derived names (e.g. host_metrics "-host") (Kimi).
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @field_validator("bind_host")
    @classmethod
    def _norm_bind_host(cls, v: str) -> str:
        # Normalize what's persisted and handed to claim_port/loopback_url: trim
        # whitespace + surrounding brackets so "  192.168.1.10  " / "[::1]" don't
        # pass the exposure guard but then fail socket.bind/getaddrinfo (#114/Kimi).
        return v.strip().strip("[]")

    @field_validator("bind_port", "control_port")
    @classmethod
    def _ports(cls, v: int) -> int:
        return _valid_port(v)

    @field_validator("control_host")
    @classmethod
    def _control_is_loopback(cls, v: str) -> str:
        # Numeric loopback only — "localhost" can resolve to a family that
        # mismatches the bind probe, so reject it to avoid a false port-free.
        if v not in {"127.0.0.1", "::1"}:
            raise ValueError("control_host must be a numeric loopback address (127.0.0.1 / ::1)")
        return v


class HubConfig(BaseModel):
    """The Hub's config."""

    machine: str = "hub"
    bind_host: str = "127.0.0.1"
    bind_port: int = 5690
    poll_interval: int = 60
    # Inbound Bearer for the Hub's OWN read API (/status, /events); empty = auth
    # disabled (V2 parity, like AgentConfig.api_token). Distinct from
    # polling_token below (#106).
    api_token: str = ""
    # Bearer sent to agents when polling (must match each agent's api_token).
    polling_token: str = ""
    openclaw_enabled: bool = False
    openclaw_url: str = "http://127.0.0.1:18789/hooks/wake"
    openclaw_token: str = ""
    # Run a host_metrics self-monitor for the Hub's own machine (§5b.2).
    self_monitor: bool = True
    # Local data dir holding hub.db + status.md. Defaults to V2's location so
    # OpenClaw's scripts (which read these files directly) work unchanged (#38).
    data_dir: str = "~/.taskpaw-hub"
    # Write status.md each poll (OpenClaw compat). Disable if not consumed.
    write_status_md: bool = True
    # Drop status_log rows older than this many days (bounded history). 0 = keep all.
    status_log_retention_days: int = Field(7, ge=0)

    @field_validator("bind_host")
    @classmethod
    def _norm_bind_host(cls, v: str) -> str:
        # Same normalization as the agent: trim whitespace + brackets so the value
        # the guard classifies is the value handed to claim_port/loopback_url (#114).
        return v.strip().strip("[]")

    @field_validator("bind_port")
    @classmethod
    def _ports(cls, v: int) -> int:
        return _valid_port(v)

    @field_validator("data_dir")
    @classmethod
    def _data_dir_abs(cls, v: str) -> str:
        # Must resolve to an absolute, `..`-free path: a relative data_dir would
        # put hub.db/status.md at a cwd-dependent location, breaking the V2-path
        # contract and the legacy-db guard (Kimi). `~` is allowed (expanded).
        v = v.strip()
        if not v:
            raise ValueError("data_dir must not be blank")
        p = Path(v).expanduser()
        if not p.is_absolute() or ".." in p.parts:
            raise ValueError("data_dir must be an absolute path (no '..')")
        return v


def load_yaml(model: type[BaseModel], path: Path) -> BaseModel:
    """Load + validate a config model from YAML."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return model(**data)


def save_yaml(cfg: BaseModel, path: Path) -> None:
    """Atomically persist a config model to YAML (tmp + fsync + replace)."""
    import os

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = yaml.safe_dump(cfg.model_dump(), sort_keys=False, allow_unicode=True)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())  # durable before replace (no empty file on power-loss)
    os.replace(tmp, path)
