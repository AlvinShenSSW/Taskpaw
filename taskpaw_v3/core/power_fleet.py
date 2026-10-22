"""The 功耗 tab's machines: this agent plus its peers (PWR-3, #289).

Design: docs/specs/2026-10-22-power-estimation.md, v2 PW06/PW08. Every figure
is an estimate (估算).

- **Peers** are `{name, address}` in agent.yaml `power.peers`; requests carry
  this agent's own api_token (the Hub's single polling token already has to
  match every agent's). Only the fixed paths `/status` and `/power/history`
  are fetched, read-only, through `ProxyHandler({})` and `NoRedirectHandler`
  (as jellyfin.py), with a 2 s timeout and a 256 KB body cap. A public (WAN)
  IP literal is refused (`core.net.bind_is_global`); a hostname is resolved
  once per request (bounded) and refused when it resolves to a public
  address, and the request goes to the address checked (IR04). On the trusted
  home LAN a mistyped private address still receives the token (accepted).
- **Cache.** A background thread queues every peer's `/status` every 5 s
  (peers in parallel) and, apart from it, each history range every minute
  (a failing one backs off up to 5 min), so the cache is never older than
  10 s while peers answer. Routes return the cache and never wait on a peer.
  A history must be equal-length `t` / `avg_w` lists of at most 168 numbers
  or nulls, else it is unavailable (IR05). A peer that is this agent (its
  `server_id`, else machine name) is listed as `self` and not summed (IR06).
- A peer without power support answers `/status` without `power` (or 404 on
  the history): 「无功耗数据（需升级 TaskPaw）」. A Mac running only the Hub
  has no agent and so no row in P0 (PW08).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from typing import Any, Callable, Optional

from taskpaw_v3.core.http import NoRedirectHandler
from taskpaw_v3.core.net import bind_is_global, bind_is_wildcard
from taskpaw_v3.core.power import ACTIVITIES

log = logging.getLogger("taskpaw.power.fleet")

DEFAULT_PORT = 5680
TIMEOUT = 2.0
MAX_BODY = 256 * 1024
STATUS_SECONDS = 5.0
HISTORY_SECONDS = 60.0
STALE_SECONDS = 30.0  # older than this, a peer's live figure is not summed
RANGES = ("24h", "7d", "30d")
MAX_POINTS = 168  # the longest history a peer may send (core.power RANGES)
HISTORY_RETRY_MAX = 300.0  # a failing history is retried at most this far apart
STATUS_WORKERS = 4
HISTORY_WORKERS = 2
_FORBIDDEN = set("/\\?#@ \t\r\n")


# ── addresses ───────────────────────────────────────────────────────────────
def parse_address(address: str) -> tuple[str, int]:
    """`host[:port]` or `[v6][:port]` → (host, port); ValueError otherwise.

    No scheme, path, credentials or query: the paths fetched are fixed. A
    wildcard or a public (WAN) IP literal is refused; a hostname cannot be
    classified here and is allowed, like the bind guard."""
    value = (address or "").strip()
    if not value or "://" in value or _FORBIDDEN & set(value):
        raise ValueError("power peer address must be host[:port]")
    port = DEFAULT_PORT
    if value.startswith("["):
        host, sep, rest = value[1:].partition("]")
        if not sep or (rest and not rest.startswith(":")):
            raise ValueError("power peer address must be host[:port]")
        port_text = rest[1:] if rest else ""
    elif value.count(":") > 1:  # a bare IPv6 literal
        host, port_text = value, ""
    else:
        host, _, port_text = value.partition(":")
    if port_text:
        if not port_text.isdigit():
            raise ValueError("power peer port must be a number")
        port = int(port_text)
    if not host or not 0 < port < 65536:
        raise ValueError("power peer address must be host[:port]")
    if bind_is_wildcard(host):
        raise ValueError("power peer address must name one machine")
    if bind_is_global(host):
        raise ValueError("power peer address must be on the LAN, not public")
    return host, port


def peer_url(address: str, path: str) -> str:
    host, port = parse_address(address)
    try:
        shown = f"[{host}]" if ipaddress.ip_address(host).version == 6 else host
    except ValueError:
        shown = host
    return f"http://{shown}:{port}{path}"


# ── one bounded GET ─────────────────────────────────────────────────────────
class PeerError(Exception):
    """A peer request failed; `code` names why, never the peer's own text."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _build_opener() -> urllib.request.OpenerDirector:
    # The empty ProxyHandler keeps a LAN request away from http_proxy.
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirectHandler()
    )


_OPENER = _build_opener()
_getaddrinfo = socket.getaddrinfo  # replaced in tests


def resolve_host(host: str, port: int, timeout: float = TIMEOUT) -> str:
    """The one address a peer's hostname is fetched at (IR04).

    Resolved once per request, bounded by `timeout` (getaddrinfo itself has no
    timeout, so it runs on a daemon thread), and refused when any answer is a
    public or wildcard address. The request then goes to that address, so the
    check and the connection see the same answer. An IP literal is used as is
    (`parse_address` already classified it)."""
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    result: list[Any] = []

    def run() -> None:
        try:
            result.append(_getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except OSError as exc:
            result.append(exc)

    worker = threading.Thread(target=run, name="power-peer-dns", daemon=True)
    worker.start()
    worker.join(timeout)
    if not result or isinstance(result[0], OSError):
        raise PeerError("unreachable")
    addresses = [str(info[4][0]) for info in result[0]]
    if not addresses:
        raise PeerError("unreachable")
    if any(bind_is_global(a) or bind_is_wildcard(a) for a in addresses):
        raise PeerError("invalid_address")
    return addresses[0]


def _read_capped(resp: Any, deadline: float) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        if time.monotonic() >= deadline:
            raise PeerError("timeout")
        chunk = resp.read1(MAX_BODY + 1 - size)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_BODY:
            raise PeerError("too_large")


def fetch_json(
    url: str,
    token: str,
    timeout: float = TIMEOUT,
    opener: Optional[urllib.request.OpenerDirector] = None,
) -> Any:
    """GET a peer's JSON with this agent's Bearer token; PeerError otherwise.

    A hostname is resolved first (`resolve_host`) and the request is sent to
    that address with the original `Host`; the whole call stays in `timeout`."""
    deadline = time.monotonic() + timeout
    headers = {"Accept": "application/json"}
    if token.strip():
        headers["Authorization"] = f"Bearer {token.strip()}"
    parts = urllib.parse.urlsplit(url)
    host, port = parts.hostname or "", parts.port or DEFAULT_PORT
    address = resolve_host(host, port, timeout)
    if address != host:
        headers["Host"] = parts.netloc
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        shown = f"[{address.replace('%', '%25')}]" if ip.version == 6 else address
        url = urllib.parse.urlunsplit(parts._replace(netloc=f"{shown}:{port}"))
    left = max(0.05, deadline - time.monotonic())
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        resp = (opener or _OPENER).open(request, timeout=left)
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code in (401, 403):
            raise PeerError("unauthorized") from None
        if exc.code == 404:
            raise PeerError("not_found") from None
        raise PeerError("http_error") from None
    except (urllib.error.URLError, OSError, socket.timeout):
        raise PeerError("unreachable") from None
    try:
        with resp:
            body = _read_capped(resp, deadline)
    except (OSError, socket.timeout):
        raise PeerError("unreachable") from None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise PeerError("invalid") from None


# ── the cache ───────────────────────────────────────────────────────────────
Fetch = Callable[[str, str], Any]
Own = Callable[[], tuple[str, str]]  # this agent's (server_id, machine)


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number and abs(number) != float("inf") else None


def _live(value: Any) -> Optional[dict[str, Any]]:
    """A peer's `/status.power`, kept to its known numeric fields."""
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {
        key: _num(value.get(key))
        for key in ("w", "gpu_w", "cpu_w", "other_w", "today_wh", "week_wh", "month_wh")
    }
    if out["w"] is None:
        return None
    activity = value.get("activity")
    out["activity"] = activity if isinstance(activity, str) else None
    out["estimated"] = True
    return out


def _series(value: Any, size: int) -> bool:
    """A list of `size` finite numbers or nulls."""
    return (
        isinstance(value, list)
        and len(value) == size
        and all(v is None or _num(v) is not None for v in value)
    )


_SERIES = ("covered_s", "avg_w", "wh", "gpu_wh", "cpu_wh", "other_wh")


def _history(value: Any) -> Optional[dict[str, Any]]:
    """A peer's `/power/history` (IR05, Codex r2): the whole shape the page
    reads. `t` is at most MAX_POINTS numbers; every series (`covered_s`,
    `avg_w`, `wh`, `gpu_wh`, `cpu_wh`, `other_wh`, and each activity of
    `activity_wh`) is as long as `t` with numbers or nulls; `totals` has a
    number `wh` and a number per activity. Anything else is unavailable
    (None), never drawn."""
    if not isinstance(value, dict):
        return None
    t = value.get("t")
    if not isinstance(t, list) or len(t) > MAX_POINTS:
        return None
    if not all(_num(v) is not None for v in t):
        return None
    if not all(_series(value.get(key), len(t)) for key in _SERIES):
        return None
    acts = value.get("activity_wh")
    if not isinstance(acts, dict) or not all(
        _series(acts.get(k), len(t)) for k in ACTIVITIES
    ):
        return None
    totals = value.get("totals")
    if not isinstance(totals, dict) or _num(totals.get("wh")) is None:
        return None
    by_activity = totals.get("activity_wh")
    if not isinstance(by_activity, dict) or any(
        _num(by_activity.get(k)) is None for k in ACTIVITIES
    ):
        return None
    return value


def _text(value: Any, limit: int = 128) -> Optional[str]:
    return value[:limit] if isinstance(value, str) else None


def _new_entry(name: str, address: str) -> dict[str, Any]:
    return {
        "name": name,
        "address": address,
        "history": {},  # range → the last valid history (or None)
        "history_due": {},  # range → monotonic time of the next fetch
        "history_fails": {},  # range → failures in a row (the backoff)
        "history_missing": False,  # the peer answered 404: an older TaskPaw
    }


class PowerFleet:
    """The peers' cache; `snapshot` never blocks on a peer.

    Each cycle (`interval`, 5 s) queues every peer's `/status` that is not
    still in flight; a status with power queues each history range that is
    due (`history_every`, 60 s; after a failure 60 s, 120 s, … up to
    `HISTORY_RETRY_MAX`). Statuses and histories run on separate pools, so a
    slow history never delays a status. `stop` cancels queued requests, starts
    no new one and returns once the requests in flight (each bounded by
    TIMEOUT) are done, within its budget (constitution §4)."""

    def __init__(
        self,
        peers: Callable[[], list[Any]],
        token: Callable[[], str],
        *,
        fetch: Optional[Fetch] = None,
        own: Optional[Own] = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        interval: float = STATUS_SECONDS,
        history_every: float = HISTORY_SECONDS,
    ) -> None:
        self._peers, self._token = peers, token
        self._fetch: Fetch = fetch or (lambda url, tok: fetch_json(url, tok))
        self._own: Own = own or (lambda: ("", ""))
        self._clock, self._wall = clock, wall
        self._interval, self._history_every = interval, history_every
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._statuses = ThreadPoolExecutor(STATUS_WORKERS, "power-peer")
        self._histories = ThreadPoolExecutor(HISTORY_WORKERS, "power-peer-history")
        self._pending: dict[tuple[Any, ...], Future[Any]] = {}  # in flight, by job

    # ── lifecycle ───────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is None and not self._stop.is_set():
            self._thread = threading.Thread(
                target=self._loop, name="power-fleet", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> bool:
        """Stop within `timeout`: no new request, queued ones cancelled, the
        loop and the requests in flight joined. False when one outlived it."""
        deadline = time.monotonic() + timeout
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(0.0, deadline - time.monotonic()))
        for pool in (self._statuses, self._histories):
            pool.shutdown(wait=False, cancel_futures=True)
        with self._lock:
            running = list(self._pending.values())
        _done, left = wait_futures(running, max(0.0, deadline - time.monotonic()))
        return not left and (thread is None or not thread.is_alive())

    def _loop(self) -> None:
        while not self._stop.is_set():
            started = self._clock()
            try:
                self.refresh_once(wait=False)
            except Exception:
                log.exception("power peers refresh failed")
            self._stop.wait(max(0.5, self._interval - (self._clock() - started)))

    # ── refresh ─────────────────────────────────────────────────────────
    def _configured(self) -> list[tuple[str, str]]:
        try:
            peers = list(self._peers() or ())
        except Exception:
            log.exception("power peers unavailable")
            return []
        return [(str(p.name), str(p.address)) for p in peers]

    def _submit(
        self,
        pool: ThreadPoolExecutor,
        job: tuple[Any, ...],
        fn: Callable[..., Any],
        *args: Any,
    ) -> Optional[Future[Any]]:
        """Queue `fn` unless the same job is still in flight or we stopped."""
        with self._lock:
            if self._stop.is_set() or job in self._pending:
                return None
            try:
                future = pool.submit(fn, *args)
            except RuntimeError:  # shut down meanwhile
                return None
            self._pending[job] = future

        def done(f: Future[Any]) -> None:
            with self._lock:
                if self._pending.get(job) is future:
                    del self._pending[job]
            if not f.cancelled() and f.exception() is not None:
                log.error("power peer %s failed", job[0], exc_info=f.exception())

        future.add_done_callback(done)
        return future

    def refresh_once(self, wait: bool = True) -> None:
        """One cycle. `wait` (tests, a direct call): return once its statuses
        and the histories they queued are done; the loop never waits."""
        peers = self._configured()
        with self._lock:  # forget removed peers
            for key in [k for k in self._cache if k not in peers]:
                del self._cache[key]
        if not peers or self._stop.is_set():
            return
        token = self._token() or ""
        futures = [
            self._submit(self._statuses, ("status", peer), self._refresh, peer, token)
            for peer in peers
        ]
        if not wait:
            return
        queued: list[Future[Any]] = []
        for future in futures:
            if future is not None and not future.cancelled():
                queued += future.result()  # a bug surfaces to the caller
        wait_futures(queued)
        for history in queued:
            if not history.cancelled():
                history.result()

    def _entry(self, peer: tuple[str, str]) -> dict[str, Any]:
        """The peer's cache entry (created on first use); hold `_lock`."""
        entry = self._cache.get(peer)
        if entry is None:
            entry = self._cache[peer] = _new_entry(*peer)
        return entry

    def _update(self, peer: tuple[str, str], **fields: Any) -> dict[str, Any]:
        # a peer removed meanwhile is pruned by the next refresh and never
        # listed by `snapshot`, which reads only the configured peers
        with self._lock:
            entry = self._entry(peer)
            entry.update(fields)
            return dict(entry)

    def _is_self(self, status: dict[str, Any]) -> bool:
        """IR06: the peer is this agent (its own address entered as a peer)."""
        try:
            server_id, machine = self._own()
        except Exception:
            log.exception("this agent's identity unavailable")
            return False
        theirs = _text(status.get("server_id"))
        if server_id and theirs:
            return theirs == server_id
        return bool(machine) and _text(status.get("machine")) == machine

    def _refresh(self, peer: tuple[str, str], token: str) -> list[Future[Any]]:
        """The peer's `/status`; returns the history requests it queued."""
        _name, address = peer
        if self._stop.is_set():
            return []
        try:
            url = peer_url(address, "/status")
        except ValueError:
            self._update(
                peer, state="invalid_address", error="invalid_address", power=None
            )
            return []
        try:
            status = self._fetch(url, token)
        except PeerError as exc:
            # the last live figure is not shown as current; the histories
            # already read stay (they are history)
            self._update(
                peer,
                state=exc.code,
                error=exc.code,
                checked_at=self._wall(),
                power=None,
            )
            return []
        if not isinstance(status, dict):
            self._update(
                peer,
                state="invalid",
                error="invalid",
                checked_at=self._wall(),
                power=None,
            )
            return []
        seen = dict(
            machine=_text(status.get("machine")),
            version=_text(status.get("version"), 32),
            os=_text(status.get("os")),
            checked_at=self._wall(),
            checked_mono=self._clock(),
            error=None,
        )
        if self._is_self(status):
            self._update(peer, **seen, state="self", power=None, history={})
            return []
        live = _live(status.get("power")) if "power" in status else None
        with self._lock:
            entry = self._entry(peer)
            entry.update(seen)
            if "power" not in status or (live is not None and entry["history_missing"]):
                entry.update(state="no_power", power=None)
            else:
                entry.update(state="ok" if live is not None else "off", power=live)
            # a peer that answered 404 is probed again at the backoff cadence
            # (Codex r2): once it has the history (upgraded) the flag clears
            probe = entry["state"] == "ok" or (
                live is not None and entry["history_missing"]
            )
            due = [
                r
                for r in RANGES
                if probe and self._clock() >= entry["history_due"].get(r, 0.0)
            ]
        queued = [
            self._submit(
                self._histories,
                ("history", peer, r),
                self._refresh_history,
                peer,
                token,
                r,
            )
            for r in due
        ]
        return [f for f in queued if f is not None]

    def _refresh_history(self, peer: tuple[str, str], token: str, range_: str) -> None:
        if self._stop.is_set():
            return
        url = peer_url(peer[1], f"/power/history?range={range_}")
        try:
            history: Optional[dict[str, Any]] = _history(self._fetch(url, token))
            code = None if history is not None else "invalid"
        except PeerError as exc:
            history, code = None, exc.code
        with self._lock:
            entry = self._entry(peer)
            fails = entry["history_fails"]
            if code is None:
                entry["history"][range_] = history
                entry["history_missing"] = False
                fails[range_] = 0
                entry["history_due"][range_] = self._clock() + self._history_every
                return
            if code == "not_found":  # an older TaskPaw without the history
                entry.update(history_missing=True, state="no_power", power=None)
            elif code == "invalid":
                entry["history"][range_] = None  # never draw what failed to validate
            fails[range_] = fails.get(range_, 0) + 1
            # the exponent is capped: 2 ** 2000 would overflow the float
            backoff = self._history_every * 2 ** min(fails[range_] - 1, 16)
            entry["history_due"][range_] = self._clock() + min(
                backoff, HISTORY_RETRY_MAX
            )

    # ── reads ───────────────────────────────────────────────────────────
    def snapshot(self, range_: str = "24h") -> list[dict[str, Any]]:
        """Every configured peer as last seen: state, live estimate and the
        range's history (None before the first answer)."""
        peers = self._configured()
        now = self._clock()
        with self._lock:
            cached = {key: dict(self._cache.get(key) or {}) for key in peers}
            for entry in cached.values():
                entry["history"] = dict(entry.get("history") or {})
        out = []
        for (name, address), entry in cached.items():
            seen = entry.get("checked_mono")
            age = None if seen is None else max(0.0, now - seen)
            out.append(
                {
                    "name": name,
                    "address": address,
                    "state": entry.get("state", "pending"),
                    "error": entry.get("error"),
                    "machine": entry.get("machine"),
                    "version": entry.get("version"),
                    "os": entry.get("os"),
                    "power": entry.get("power"),
                    "history": entry["history"].get(range_),
                    "age_s": None if age is None else round(age, 1),
                    "stale": age is None or age > STALE_SECONDS,
                }
            )
        return out


# ── process-wide holder (set by the launcher) ───────────────────────────────
_holder_lock = threading.Lock()
_fleet: Optional[PowerFleet] = None


def set_fleet(fleet: Optional[PowerFleet]) -> None:
    global _fleet
    with _holder_lock:
        _fleet = fleet


def get_fleet() -> Optional[PowerFleet]:
    with _holder_lock:
        return _fleet


def cluster_power() -> Optional[dict[str, Any]]:
    """媒体处理中心's 集群功耗: this machine's live estimate plus every fresh
    peer's; None when there is nothing to sum."""
    from taskpaw_v3.core import power

    parts: list[dict[str, Any]] = []
    sampler = power.get_sampler()
    if sampler is not None:
        try:
            local = sampler.live()
        except Exception:
            log.exception("local live power unavailable")
            local = None
        if local is not None:
            parts.append(local)
    fleet = get_fleet()
    peers = fleet.snapshot() if fleet is not None else []
    parts += [
        p["power"]
        for p in peers
        if p["state"] == "ok" and p["power"] and not p["stale"]
    ]
    if not parts:
        return None

    def total(key: str) -> float:
        return round(sum(_num(p.get(key)) or 0.0 for p in parts), 1)

    return {
        "w": total("w"),
        "gpu_w": total("gpu_w"),
        "cpu_w": total("cpu_w"),
        "other_w": total("other_w"),
        "today_wh": round(sum(_num(p.get("today_wh")) or 0.0 for p in parts), 3),
        "machines": len(parts),
        "peers": len(peers),
        "estimated": True,
    }


def _reset_for_tests() -> None:
    set_fleet(None)
