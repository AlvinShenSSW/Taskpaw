"""Hub poller: the end-to-end loop (poll → store → OpenClaw outbox).

Retains the V2 #14 persistence/outbox ordering after cursor admission:
- sample current `/status`, then poll `/events?ack=<durable last id>` only for
  verified durable lineage; legacy agents remain visible through status;
- store the event (idempotent) AND enqueue the outbox row, THEN advance + persist
  the ack — at-least-once (a crash re-fetches, never loses);
- drain the outbox with exponential backoff; dead-letter after 10 attempts or
  >24h with exactly one local alert; prune dead letters after 7 days;
- enqueue only when OpenClaw is **active** (enabled AND token) so the outbox
  can't fill with undeliverable rows.
"""

from __future__ import annotations

import copy
import json
import logging
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from taskpaw_v3.core.http import NoRedirectHandler
from taskpaw_v3.core.state import StateError, parse_cursor

from .upstream_worker import Transport, UpstreamError, decode_status

_opener = urllib.request.build_opener(NoRedirectHandler())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _agent_base_url(ip: str, port: int) -> str:
    """Build a base URL, bracketing literal IPv6 addresses."""
    host = f"[{ip}]" if ":" in ip else ip
    return f"http://{host}:{port}"


def _events_from_payload(data: object) -> Optional[list]:
    """Extract the events array from an agent's /events JSON, tolerant of shape.

    The V3 agent returns the canonical `{"events": [...]}`, but a foreign/older agent
    (or one behind a proxy) may return a bare JSON list. Accept both; return None for
    any other shape so the caller can log a clear diagnostic instead of crashing on
    `list.get` (the "'list' object has no attribute 'get'" report). Element validation
    (dict, id) is left to the caller."""
    if isinstance(data, dict):
        events = data.get("events", [])
        return events if isinstance(events, list) else []
    if isinstance(data, list):
        return data
    return None


from .openclaw import send_payload  # noqa: E402
from .outbox_migration import RowError, validate_row  # noqa: E402
from .store import HubStore  # noqa: E402

log = logging.getLogger("taskpaw.hub.poller")


class Poller:
    def __init__(
        self,
        store: HubStore,
        openclaw_url: str,
        get_active: Callable[[], bool],
        get_token: Callable[[], str],
        get_polling_token: Optional[Callable[[], str]] = None,
        http_timeout: float = 5.0,
        seed_fresh_seconds: float = 0.0,
    ) -> None:
        self.store = store
        # On restart, only seed a server as ONLINE if its last successful poll is
        # newer than this (≈ a poll interval). status_log holds only successes, so
        # a stale last-success means the agent may have gone down while the Hub
        # was off — don't render it ONLINE (Kimi). 0 = always trust (tests).
        self.seed_fresh_seconds = seed_fresh_seconds
        self.openclaw_url = openclaw_url
        self.get_active = get_active  # openclaw_enabled AND token
        self.get_token = get_token  # OpenClaw token
        # Bearer sent to agents when polling. Falls back to the SQLite config row
        # for back-compat; callers should pass one that also honors HubConfig.
        self.get_polling_token = get_polling_token or (
            lambda: self.store.get_config("polling_token", "")
        )
        self.http_timeout = http_timeout
        self._acks_lock = threading.Lock()
        self._ack_store_invalid = False
        self._channel_lock = threading.Lock()
        self._event_channels: dict[int, dict] = {}
        self.last_event_ids: dict[int, int] = self._load_acks()
        # In-memory current-poll snapshot per server (reachable + last good
        # status + last_seen) — the source for status.md, like V2's in-memory
        # server_statuses. status_log itself only gets SUCCESSFUL polls (#38).
        self._transport = Transport()
        self._batches: dict[int, dict] = {}
        self._fetch_errors: dict[int, str] = {}
        self._snap_lock = threading.Lock()
        self._status_snapshot: dict[int, dict] = self._seed_snapshot()

    def _seed_snapshot(self) -> dict[int, dict]:
        seed = {}
        try:
            self.store.recover_statuses()
            for row in self.store.upstream_statuses():
                # An old offset-less local timestamp does not establish freshness.
                reachable = False
                if row["last_good_at"] and not row["error_code"]:
                    age = (
                        _now() - datetime.fromisoformat(row["last_good_at"])
                    ).total_seconds()
                    reachable = age >= 0 and (
                        not self.seed_fresh_seconds or age <= self.seed_fresh_seconds
                    )
                seed[row["id"]] = {**row, "reachable": reachable}
        except Exception:
            log.error("Status seed failed")
        return seed

    def stop(self) -> None:
        self._transport.cancel()

    def stopped(self) -> bool:
        return self._transport.retry_cleanup() and self._transport.clean()

    @staticmethod
    def _health(snap: dict) -> dict:
        age = None
        error = snap.get("error_code")
        if snap.get("good_monotonic") is not None:
            age = max(0.0, time.monotonic() - snap["good_monotonic"])
        elif snap.get("last_good_at"):
            age = (
                _now() - datetime.fromisoformat(snap["last_good_at"])
            ).total_seconds()
            if age < 0:
                age = None
                error = error or "clock_changed"
        state = (
            "error"
            if error
            else "ok"
            if snap.get("status_json") is not None
            else "never"
        )
        if not snap.get("scan_done", True) and not snap.get("status_json"):
            state = "recovering"
        return {
            "state": state,
            "error_code": error,
            "attempted_at": snap.get("attempted_at"),
            "last_good_at": snap.get("last_good_at"),
            "age_seconds": age,
        }

    def status_snapshot(self) -> list[dict]:
        """Current status of each ENABLED server for status.md (registration
        order). Servers not yet polled this run show reachable=False/no data.

        Contract: called from the poller thread (HubService._loop after a poll).
        The DB write in _poll_server and this read use different locks, which is
        safe only under that single-thread access; if ever exposed to another
        thread, take _snap_lock around both the DB write and the snapshot update.

        Best-effort: the server list (DB) and the in-memory snapshot are read
        separately, so a server removed between the two reads may briefly appear
        stale in one status.md render — acceptable for a human-readable snapshot."""
        servers = self.store.list_servers()
        current_ids = {s["id"] for s in servers}
        with self._snap_lock:
            # Drop entries for removed servers so the dict can't grow without
            # bound under add/remove churn in the long-running poller (Kimi).
            for k in list(self._status_snapshot):
                if k not in current_ids:
                    self._status_snapshot.pop(k, None)
            snaps = {k: dict(v) for k, v in self._status_snapshot.items()}
        out: list[dict] = []
        for s in servers:
            if not s["enabled"]:
                continue
            snap = snaps.get(s["id"], {})
            out.append(
                {
                    "name": s["name"],
                    "reachable": bool(snap.get("reachable", False)),
                    "status_json": snap.get("status_json"),
                    "last_seen": snap.get("last_seen"),
                    "status_health": self._health(snap),
                }
            )
        return out

    @staticmethod
    def _parse_status(raw: Optional[str]) -> Optional[dict]:
        if raw is None:
            return None
        try:
            return decode_status(raw)[0]
        except UpstreamError:
            return None

    def snapshot_acks(self) -> dict[int, int]:
        """Thread-safe copy of the ack cursor for the API thread (/status)."""
        with self._acks_lock:
            return dict(self.last_event_ids)

    def snapshot_statuses(self) -> dict[int, dict]:
        """Thread-safe per-server status snapshot for the API thread (/status).

        Returns `{server_id: {online, last_seen, snapshot}}` where `snapshot` is
        the agent's last good /status parsed to a dict (None if never polled or
        unparseable). Reads under `_snap_lock`, the same lock the poller takes when
        it writes `_status_snapshot` (#96), so it's safe from the API thread.

        Degrades gracefully: a currently-unreachable agent keeps its last good
        snapshot + last_seen with `online=False` (the poller preserves them), so
        the dashboard shows last-known metrics + a stale marker rather than erroring.
        """
        with self._snap_lock:
            snaps = {k: dict(v) for k, v in self._status_snapshot.items()}
        out: dict[int, dict] = {}
        for sid, snap in snaps.items():
            out[sid] = {
                "online": bool(snap.get("reachable", False)),
                "last_seen": snap.get("last_seen"),
                # Pre-parsed at write time (#107) — no json.loads on the request path.
                "snapshot": copy.deepcopy(snap.get("parsed_status")),
                "status_health": self._health(snap),
            }
        return out

    # ── ack cursor persistence ───────────────────────────────────────────
    def _load_acks(self) -> dict[int, int]:
        try:
            return self.store.read_acks()
        except StateError:
            self._ack_store_invalid = True
            log.error("Event cursor config invalid; offline adoption required")
            return {}

    def snapshot_event_channels(self) -> dict[int, dict]:
        with self._channel_lock:
            return {sid: dict(value) for sid, value in self._event_channels.items()}

    def _channel(self, sid: int, reason: str | None = None) -> None:
        value = {
            "state": "paused" if reason else "ready",
            "reason": reason,
            "recovery_hint": "Upgrade/inspect the Agent and use verified offline cursor adoption."
            if reason
            else None,
        }
        with self._channel_lock:
            changed = self._event_channels.get(sid) != value
            self._event_channels[sid] = value
        if changed and reason:
            log.warning("Event channel paused for server %s: %s", sid, reason)

    def _admit_cursor(self, server: dict, status: object) -> dict:
        sid = server["id"]
        if not self._transport.admit_result():
            raise StateError("helper_cancelled")
        if self._ack_store_invalid:
            raise StateError("cursor_store_invalid")
        cursor = parse_cursor(
            status.get("event_cursor") if isinstance(status, dict) else None
        )
        binding = self.store.get_event_cursor(sid)
        identity = {k: cursor[k] for k in ("server_id", "stream_id")}
        floor = self.store.event_floor(sid, self.last_event_ids)
        acks = dict(self.last_event_ids)
        if binding["state"] == "fresh":
            if floor != -1:
                raise StateError("cursor_adoption_required")
            acks[sid] = -1
        elif binding["state"] != "bound":
            raise StateError("cursor_adoption_required")
        else:
            if sid not in acks:
                raise StateError("cursor_store_invalid")
            if binding["identity"] != identity:
                raise StateError("state_identity_changed")
            if binding["boot_id"] != cursor["boot_id"]:
                if cursor["resume_floor"] < floor:
                    raise StateError("cursor_floor_regressed")
            elif (
                binding["resume_floor"] != cursor["resume_floor"]
                or cursor["offered_highwater"] < floor
            ):
                raise StateError("cursor_floor_regressed")
        new_binding = {
            "state": "bound",
            "identity": identity,
            "boot_id": cursor["boot_id"],
            "resume_floor": cursor["resume_floor"],
        }
        if binding != new_binding:
            with self._acks_lock:
                if not self._transport.admit_result():
                    raise StateError("helper_cancelled")
                self.store.commit_event_cursor(sid, new_binding, acks)
                self.last_event_ids = acks
        return cursor

    def _persist_acks(self) -> bool:
        try:
            self.store.set_config("last_event_ids", json.dumps(self.last_event_ids))
            return True
        except Exception as e:
            log.error("Failed to persist last_event_ids: %s", e)
            return False

    # ── HTTP ─────────────────────────────────────────────────────────────
    def _auth_headers(self) -> dict:
        token = self.get_polling_token()
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _request(self, request: dict) -> dict:
        return self._transport.request(request, self.http_timeout)

    def fetch_status(self, server: dict) -> tuple[bool, Optional[str]]:
        result = self._request(
            {
                "kind": "status",
                "url": _agent_base_url(server["ip"], server["port"]) + "/status",
                "headers": self._auth_headers(),
                "timeout": self.http_timeout,
            }
        )
        if result.get("ok"):
            self._fetch_errors.pop(server["id"], None)
            return True, result["raw"]
        reason = result.get("reason", "upstream_failed")
        self._fetch_errors[server["id"]] = reason
        log.warning("Upstream status refused server=%s reason=%s", server["id"], reason)
        return False, None

    def fetch_events(self, server: dict, current_status: object = None) -> list[dict]:
        sid = server["id"]
        self._batches.pop(sid, None)
        try:
            if current_status is None:
                reachable, raw = self.fetch_status(server)
                current_status = self._parse_status(raw) if reachable else None
            if current_status is None:
                raise StateError("current_status_unavailable")
            cursor = self._admit_cursor(server, current_status)
            q = urllib.parse.urlencode(
                {
                    "ack": self.last_event_ids.get(sid, -1),
                    "cursor_stream": cursor["stream_id"],
                    "cursor_boot": cursor["boot_id"],
                }
            )
            result = self._request(
                {
                    "kind": "events",
                    "url": _agent_base_url(server["ip"], server["port"])
                    + "/events?"
                    + q,
                    "headers": self._auth_headers(),
                    "timeout": self.http_timeout,
                }
            )
            if not result.get("ok"):
                reason = result.get("reason", "event_fetch_failed")
                raise StateError(
                    "event_http_refused"
                    if reason in ("http_refused", "http_auth")
                    else reason
                )
            proof = parse_cursor(result["proof"])
            if (
                any(
                    proof[k] != cursor[k]
                    for k in ("server_id", "stream_id", "boot_id", "resume_floor")
                )
                or proof["next_event_id"] < cursor["next_event_id"]
                or proof["offered_highwater"] < cursor["offered_highwater"]
            ):
                raise StateError("event_cursor_mismatch")
            if not self._transport.admit_result():
                raise StateError("helper_cancelled")
            self._batches[sid] = result
            self._channel(sid)
            return [
                e
                for e in result["events"]
                if e["id"] > self.last_event_ids.get(sid, -1)
            ]
        except StateError as exc:
            self._channel(sid, exc.reason)
        except Exception:
            self._channel(sid, "invalid_cursor_response")
        return []

    # ── retry/backoff ────────────────────────────────────────────────────
    def _retry_delay(self, attempts: int) -> float:
        base = min(3600, 30 * (2 ** max(0, attempts - 1)))
        return base * random.uniform(0.8, 1.2)

    def emit_local_alert(self, message: str) -> None:
        log.critical(message)

    def drain_outbox(self) -> None:
        if not self.get_token():
            return
        now = _now()
        try:
            rows = self.store.due_deliveries(now=now, limit=10)
        except Exception:
            log.error("Outbox due query failed")
            return
        for row in rows:
            try:
                try:
                    created, _next_at, payload = validate_row(row)
                except RowError as exc:
                    self.store.quarantine_delivery(row["id"], exc.reason, exc.column)
                    continue
                attempts = row["attempts"]
                created_at = datetime.fromisoformat(created)
                if attempts >= 10 or now - created_at > timedelta(hours=24):
                    reason = "attempt cap" if attempts >= 10 else "age>24h"
                    if self.store.mark_delivery_dead_letter(
                        row["id"], attempts, reason
                    ):
                        self.emit_local_alert(
                            f"OpenClaw delivery dead-lettered id={row['id']}: {reason}"
                        )
                    continue
                try:
                    send_payload(
                        self.openclaw_url, self.get_token(), payload, self.http_timeout
                    )
                except Exception as exc:
                    attempts += 1
                    if attempts >= 10:
                        if self.store.mark_delivery_dead_letter(
                            row["id"], attempts, str(exc)
                        ):
                            self.emit_local_alert(
                                f"OpenClaw delivery dead-lettered id={row['id']}: delivery failed"
                            )
                    else:
                        self.store.mark_delivery_failed(
                            row["id"],
                            attempts,
                            str(exc),
                            now + timedelta(seconds=self._retry_delay(attempts)),
                        )
                else:
                    self.store.delete_delivery(row["id"])
            except Exception:
                # Includes quarantine, age arithmetic, and state persistence.
                # No stored payload or raw exception enters this diagnostic.
                log.error("Outbox row processing failed id=%s", row.get("id"))

    # ── one poll cycle ───────────────────────────────────────────────────
    def poll_once(self) -> None:
        if self._transport._stopped.is_set():
            return
        self.store.recover_statuses()
        recovered = {row["id"]: row for row in self.store.upstream_statuses()}
        with self._snap_lock:
            for sid, row in recovered.items():
                if not self._status_snapshot.get(sid, {}).get(
                    "status_json"
                ) and row.get("status_json"):
                    self._status_snapshot[sid] = {**row, "reachable": False}
        active = self.get_active()
        for server in self.store.list_servers():
            if self._transport._stopped.is_set():
                break
            if not server["enabled"]:
                continue
            try:
                self._poll_server(server, active)
            except Exception:
                log.error("Polling server failed id=%s", server["id"])

        if active:
            self.drain_outbox()

        try:
            self.store.prune_dead_letters()
        except Exception as e:
            log.error("Dead-letter prune failed: %s", e)

    def _poll_server(self, server: dict, active: bool) -> None:
        sid = server["id"]
        reachable, status_json = self.fetch_status(server)
        parsed = self._parse_status(status_json) if reachable else None
        if reachable and parsed is None:
            reachable = False
            self._fetch_errors[sid] = "status_type"
        if not self._transport.admit_result():
            return
        try:
            self.store.record_status(
                sid,
                status_json if reachable else None,
                self._fetch_errors.get(sid, "upstream_failed"),
            )
        except Exception:
            reachable = False
            self._fetch_errors[sid] = "status_store_failed"
        with self._snap_lock:
            prev = self._status_snapshot.get(sid, {})
            if reachable:
                now = _now().isoformat(timespec="microseconds")
                snap = {
                    "status_json": status_json,
                    "parsed_status": parsed,
                    "last_seen": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "last_good_at": now,
                    "attempted_at": now,
                    "error_code": None,
                    "good_monotonic": time.monotonic(),
                    "scan_done": True,
                }
            else:
                snap = {
                    **prev,
                    "attempted_at": _now().isoformat(timespec="microseconds"),
                    "error_code": self._fetch_errors.get(sid, "upstream_failed"),
                }
            self._status_snapshot[sid] = {**snap, "reachable": reachable}
        if not reachable:
            self._channel(sid, "current_status_unavailable")
            return
        self.fetch_events(server, parsed)
        batch = self._batches.pop(sid, None)
        if batch is None or self._transport._stopped.is_set():
            return
        try:
            with self._acks_lock:
                if not self._transport.admit_result():
                    return
                self.last_event_ids = self.store.commit_upstream_batch(
                    server, batch, active
                )
            summary = self.store.quarantine_summary(sid)
            with self._channel_lock:
                self._event_channels[sid]["quarantine"] = summary
        except Exception:
            self._channel(sid, "event_store_failed")
            log.error("Upstream batch transaction failed server=%s", sid)
