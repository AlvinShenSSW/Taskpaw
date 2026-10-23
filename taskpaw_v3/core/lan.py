"""#299 R4/R5: this machine's LAN addresses, the save-time bind probe and the
startup self-heal of a stale `bind_host`.

Addresses come from psutil (no network traffic): IPv4, private, not loopback
or link-local, on an interface that is up. The primary one is the address the
routing table picks for an outside destination (a UDP `connect`, which sends
nothing); without a route the first address is primary. Every reader is a
module function so tests replace it — no test depends on this host's NICs.
"""

from __future__ import annotations

import errno
import ipaddress
import logging
import socket
from dataclasses import dataclass
from typing import Callable, Optional

from taskpaw_v3.core.net import (
    PortBindError,
    PortInUseError,
    bind_is_global,
    bind_is_loopback,
    bind_is_wildcard,
    claim_port,
)

log = logging.getLogger("taskpaw.lan")

NOT_LOCAL = "此地址不属于本机"
IN_USE = "端口已被占用"
UNAVAILABLE = "端口不可用"


@dataclass(frozen=True)
class Address:
    ip: str
    nic: str
    primary: bool = False

    def view(self) -> dict:
        return {"ip": self.ip, "nic": self.nic, "primary": self.primary}


def _interfaces() -> dict[str, list[str]]:
    """{nic: [every IP literal on it]} for the interfaces that are up."""
    import psutil

    stats = psutil.net_if_stats()
    found: dict[str, list[str]] = {}
    for nic, entries in psutil.net_if_addrs().items():
        if nic in stats and not stats[nic].isup:
            continue
        for entry in entries:
            if entry.family in (socket.AF_INET, socket.AF_INET6):
                found.setdefault(nic, []).append(str(entry.address).split("%")[0])
    return found


def _route_source() -> Optional[str]:
    """The source address the OS would use for an outside destination."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # TEST-NET-1; UDP connect sends nothing
            return str(probe.getsockname()[0])
    except OSError:
        return None


def _private_v4(ip: str) -> bool:
    try:
        value = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return (
        value.version == 4
        and value.is_private
        and not value.is_loopback
        and not value.is_link_local
    )


def local_addresses(
    interfaces: Optional[Callable[[], dict[str, list[str]]]] = None,
    route_source: Optional[Callable[[], Optional[str]]] = None,
) -> list[Address]:
    """This machine's private IPv4 addresses, the primary one first."""
    try:
        table = (interfaces or _interfaces)()
    except Exception:  # psutil failure: nothing is known
        log.warning("could not list the network interfaces")
        return []
    pairs = [(ip, nic) for nic, ips in table.items() for ip in ips if _private_v4(ip)]
    source = (route_source or _route_source)()
    primary = next((ip for ip, _ in pairs if ip == source), None)
    if primary is None and pairs:
        primary = pairs[0][0]
    out = [Address(ip, nic, ip == primary) for ip, nic in pairs]
    out.sort(key=lambda a: not a.primary)
    return out


def is_local(
    host: str, interfaces: Optional[Callable[[], dict[str, list[str]]]] = None
) -> Optional[bool]:
    """True when `host` is loopback or on one of this machine's interfaces;
    None when the interfaces cannot be read (unknown is never "not local")."""
    if bind_is_loopback(host):
        return True
    try:
        table = (interfaces or _interfaces)()
    except Exception:
        return None
    try:
        wanted = ipaddress.ip_address(host.strip().strip("[]"))
    except ValueError:
        return None  # a hostname: the bind itself decides
    for ips in table.values():
        for ip in ips:
            try:
                if ipaddress.ip_address(ip) == wanted:
                    return True
            except ValueError:
                continue
    return False


def probe_bind(host: str, port: int) -> Optional[str]:
    """R4: bind and release `host:port` now; None when it works, else the
    field message (「此地址不属于本机」 / 「端口已被占用」 / 「端口不可用」)."""
    try:
        sock = claim_port(host, port, "settings probe")
    except PortInUseError:
        return IN_USE
    except PortBindError as exc:
        cause = exc.__cause__
        codes = {getattr(cause, "errno", None), getattr(cause, "winerror", None)}
        if codes & {errno.EADDRNOTAVAIL, 10049} or isinstance(cause, socket.gaierror):
            return NOT_LOCAL
        return UNAVAILABLE
    except OSError:
        return UNAVAILABLE
    sock.close()
    return None


def healed_bind_host(
    lan_access: bool,
    bind_host: str,
    addresses: Optional[Callable[[], list[Address]]] = None,
    local: Optional[Callable[[str], Optional[bool]]] = None,
) -> Optional[str]:
    """R5: the address to use instead of a LAN `bind_host` this machine no
    longer has (DHCP gave it another): the primary private address. None:
    keep it (LAN off, loopback, still local, unknown, nothing to move to).
    A wildcard or public bind is never healed — the exposure guard refuses it."""
    if not lan_access or bind_is_loopback(bind_host):
        return None
    if bind_is_wildcard(bind_host) or bind_is_global(bind_host):
        return None
    if (local or is_local)(bind_host) is not False:
        return None
    found = (addresses or local_addresses)()
    primary = next((a.ip for a in found if a.primary), None)
    return primary if primary and primary != bind_host else None
