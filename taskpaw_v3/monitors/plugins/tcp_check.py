"""`tcp_check` monitor — is a TCP port accepting connections? (§4.2, §5.1 ③)

Used for OpenD (`127.0.0.1:11111`) and any "is the gateway listening" check.
Connect-based (not process-name) so it's robust across platforms where the
process name differs (macOS `moomoo_OpenD.app` vs Linux `./OpenD`).
"""

from __future__ import annotations

import socket
from typing import Optional

from taskpaw_v3.monitors.base import (
    BaseMonitorConfig,
    EventEmitter,
    MonitorInstance,
    MonitorPlugin,
    MonitorStatus,
)


class TcpCheckConfig(BaseMonitorConfig):
    host: str = "127.0.0.1"
    port: int
    connect_timeout: float = 5.0  # cap per attempt (documented, configurable)


def tcp_listening(host: str, port: int, timeout: float) -> bool:
    """True if any resolved address for host:port accepts a connection.

    Uses getaddrinfo so hostnames that resolve to IPv6-only (or multiple)
    addresses are tried with the correct family.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for family, socktype, proto, _canon, sockaddr in infos:
        try:
            with socket.socket(family, socktype, proto) as s:
                s.settimeout(timeout)
                s.connect(sockaddr)
                return True
        except OSError:
            continue
    return False


class TcpCheckInstance(MonitorInstance):
    def __init__(self, instance_id: str, config: TcpCheckConfig) -> None:
        super().__init__(instance_id, config)
        self._prev_up: Optional[bool] = None

    def check(self, emit: EventEmitter) -> MonitorStatus:
        cfg: TcpCheckConfig = self.config  # type: ignore[assignment]
        up = tcp_listening(cfg.host, cfg.port, cfg.connect_timeout)
        target = f"{cfg.host}:{cfg.port}"
        # Alert on a down state at startup too, not only on a healthy→down change.
        if not up and self._prev_up in (None, True):
            emit("alert", f"{cfg.name} down", f"{target} not accepting connections")
        elif up and self._prev_up is False:
            emit("done", f"{cfg.name} listening", f"{target} is up again")
        self._prev_up = up
        return MonitorStatus(
            state="ok" if up else "error",
            detail="listening" if up else "no connection",
            metrics={"listening": up},
        )


class TcpCheckPlugin(MonitorPlugin):
    type_id = "tcp_check"
    display_name = "TCP port listening"
    category = "service"
    config_version = 1

    @classmethod
    def config_model(cls) -> type[BaseMonitorConfig]:
        return TcpCheckConfig

    def create(self, instance_id: str, config: BaseMonitorConfig) -> MonitorInstance:
        return TcpCheckInstance(instance_id, config)  # type: ignore[arg-type]
