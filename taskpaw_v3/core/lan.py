"""#299 R4/R5: this machine's LAN addresses, the save-time bind probe and the
startup self-heal of a stale `bind_host`.

Addresses come from psutil (no network traffic): IPv4, private, not loopback
or link-local, on an interface that is up and is not a virtual or VPN adapter
(WSL / Hyper-V vEthernet, VirtualBox, VMware, Tailscale, ZeroTier; and never
the 100.64.0.0/10 shared range). The primary one is the address the routing
table picks for an outside destination (a UDP `connect`, which sends nothing);
without a route the first address is primary but not `routed`. The self-heal
moves a bind only to a `routed` address, so a heal that runs before Wi-Fi or
DHCP is up never lands on a virtual adapter. Every reader is a module function
so tests replace it — no test depends on this host's NICs.
"""

from __future__ import annotations

import errno
import ipaddress
import logging
import re
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
# #299 review IR1: adapters a home LAN address never lives on
VIRTUAL_NIC = re.compile(
    r"vethernet|wsl|hyper-v|virtualbox|vmware|vmnet|tailscale|zerotier",
    re.IGNORECASE,
)
SHARED_RANGE = ipaddress.ip_network("100.64.0.0/10")  # CGNAT / Tailscale


@dataclass(frozen=True)
class Address:
    ip: str
    nic: str
    primary: bool = False
    # the default route's source address (a heal may move the bind here)
    routed: bool = False

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
        and value not in SHARED_RANGE
    )


def lan_candidate(ip: str, nic: str) -> bool:
    """A private IPv4 address on a real (not virtual / VPN) adapter."""
    return _private_v4(ip) and not VIRTUAL_NIC.search(nic or "")


def local_addresses(
    interfaces: Optional[Callable[[], dict[str, list[str]]]] = None,
    route_source: Optional[Callable[[], Optional[str]]] = None,
) -> list[Address]:
    """This machine's private IPv4 LAN addresses, the primary one first."""
    try:
        table = (interfaces or _interfaces)()
    except Exception:  # psutil failure: nothing is known
        log.warning("could not list the network interfaces")
        return []
    pairs = [
        (ip, nic) for nic, ips in table.items() for ip in ips if lan_candidate(ip, nic)
    ]
    try:
        source = (route_source or _route_source)()
    except Exception:
        source = None
    routed = next((ip for ip, _ in pairs if ip == source), None)
    primary = routed if routed is not None else (pairs[0][0] if pairs else None)
    out = [Address(ip, nic, ip == primary, ip == routed) for ip, nic in pairs]
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
    longer has (DHCP gave it another): the default route's source address
    (only a LAN address on a real adapter, review IR1) unless another candidate
    shares the old address's /24 (CX11). None: keep
    it (LAN off, loopback, still local, unknown, or no routed LAN address yet
    — e.g. Wi-Fi not up, only a virtual adapter). A wildcard or public bind
    is never healed — the exposure guard refuses it."""
    if not stale_bind(lan_access, bind_host, local):
        return None
    found = (addresses or local_addresses)()
    routed = next((a.ip for a in found if a.routed), None)
    if routed is None:
        return None
    # CX11: a full-tunnel VPN can own the default route under a name the
    # blacklist misses; stay on the saved address's /24 when we can.
    same = _same_subnet(found, bind_host)
    pick = same or routed
    return pick if pick != bind_host else None


def _same_subnet(found: list[Address], old: str) -> Optional[str]:
    """The first candidate in `old`'s /24 (the interface table carries no
    netmask), preferring the primary one."""
    try:
        net = ipaddress.ip_network(f"{old.strip().strip('[]')}/24", strict=False)
    except ValueError:
        return None
    for a in found:
        if ipaddress.ip_address(a.ip) in net:
            return a.ip
    return None


def stale_bind(
    lan_access: bool,
    bind_host: str,
    local: Optional[Callable[[str], Optional[bool]]] = None,
) -> bool:
    """A LAN `bind_host` this machine is known not to have (any more)."""
    if not lan_access or bind_is_loopback(bind_host):
        return False
    if bind_is_wildcard(bind_host) or bind_is_global(bind_host):
        return False
    return (local or is_local)(bind_host) is False
