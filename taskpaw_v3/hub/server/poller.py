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

import json
import logging
import random
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from taskpaw_v3.core.http import NoRedirectHandler
from taskpaw_v3.core.state import MAX_EVENT_ID, StateError, integer, parse_cursor

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
        self._snap_lock = threading.Lock()
        self._status_snapshot: dict[int, dict] = self._seed_snapshot()

    def _seed_snapshot(self) -> dict[int, dict]:
        """Seed from the persisted last-good status so status.md after a restart
        reflects known state instead of showing every server OFFLINE until the
        first poll (Kimi). status_log holds only successful rows, so a present
        row means it was last reachable."""
        seed: dict[int, dict] = {}
        try:
            for row in self.store.latest_statuses():
                if row.get("status_json") is None:
                    continue  # never polled
                ts = row.get("last_seen") or row.get("timestamp")
                reachable = bool(row.get("reachable"))
                if reachable and self.seed_fresh_seconds and ts:
                    # A stale last-success (older than ~a poll) shouldn't seed
                    # ONLINE — the agent may have stopped while the Hub was off.
                    try:
                        age = (
                            datetime.now() - datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                        ).total_seconds()
                        if age > self.seed_fresh_seconds:
                            reachable = False
                    except (ValueError, TypeError):
                        pass
                seed[row["id"]] = {
                    "reachable": reachable,
                    "status_json": row.get("status_json"),
                    # Parse once at seed time so the first /status (pre-poll) is
                    # also parse-free (#107).
                    "parsed_status": self._parse_status(row.get("status_json")),
                    "last_seen": ts,
                }
        except Exception as e:
            # Don't fail hub startup on a transient read race, but make a real
            # corrupt-store/schema problem visible (Kimi).
            log.error("Could not seed status snapshot: %s", e)
        return seed

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
                }
            )
        return out

    @staticmethod
    def _parse_status(raw: Optional[str]) -> Optional[dict]:
        """Parse an agent's /status JSON to a dict, or None if absent/unparseable.
        Centralizes the dict-only rule so the parse happens once at write time
        (#107), not on every /status request."""
        if not raw:
            return None
        try:
            loaded = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return loaded if isinstance(loaded, dict) else None

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
                "snapshot": snap.get("parsed_status"),
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
            self.store.commit_event_cursor(sid, new_binding, acks)
            with self._acks_lock:
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

    def fetch_status(self, server: dict) -> tuple[bool, Optional[str]]:
        """GET the agent's /status. Returns (reachable, raw_json_or_None). A bad
        response / unreachable host → (False, None), not an exception (#38)."""
        base = _agent_base_url(server["ip"], server["port"])
        try:
            req = urllib.request.Request(f"{base}/status", headers=self._auth_headers())
            with _opener.open(req, timeout=self.http_timeout) as resp:
                body = resp.read().decode("utf-8")
            try:
                json.loads(body)  # validate it's JSON before persisting
            except json.JSONDecodeError as e:
                # Reachable but returned junk → a misconfigured agent, not an
                # outage. Log distinctly with a body snippet (Kimi).
                log.warning(
                    "Bad /status JSON from %s: %s | body[:120]=%r",
                    server.get("name"),
                    e,
                    body[:120],
                )
                return False, None
            return True, body
        except urllib.error.HTTPError as e:
            # Surface 401/403 distinctly — a token mismatch is a config/security
            # problem, not a down agent (Kimi).
            if e.code in (401, 403):
                log.warning(
                    "Auth failed polling %s (HTTP %s) — check polling_token",
                    server.get("name"),
                    e.code,
                )
            else:
                log.warning(
                    "HTTP %s fetching status from %s", e.code, server.get("name")
                )
            return False, None
        except Exception as e:
            log.warning("Failed to fetch status from %s: %s", server.get("name"), e)
            return False, None

    def fetch_events(self, server: dict, current_status: object = None) -> list[dict]:
        sid = server["id"]
        try:
            if current_status is None:
                reachable, raw = self.fetch_status(server)
                current_status = self._parse_status(raw) if reachable else None
            if current_status is None:
                raise StateError("current_status_unavailable")
            cursor = self._admit_cursor(server, current_status)
            last_id = self.last_event_ids.get(sid, -1)
            q = urllib.parse.urlencode(
                {
                    "ack": last_id,
                    "cursor_stream": cursor["stream_id"],
                    "cursor_boot": cursor["boot_id"],
                }
            )
            base = _agent_base_url(server["ip"], server["port"])
            req = urllib.request.Request(
                f"{base}/events?{q}", headers=self._auth_headers()
            )
            with _opener.open(req, timeout=self.http_timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
            if not isinstance(data, dict):
                raise StateError("invalid_cursor_response")
            proof = parse_cursor(data.get("event_cursor"))
            if (
                any(
                    proof[k] != cursor[k]
                    for k in ("server_id", "stream_id", "boot_id", "resume_floor")
                )
                or proof["next_event_id"] < cursor["next_event_id"]
                or proof["offered_highwater"] < cursor["offered_highwater"]
            ):
                raise StateError("event_cursor_mismatch")
            events = _events_from_payload(data)
            if events is None or not isinstance(data.get("events"), list):
                raise StateError("invalid_cursor_response")
            for event in events:
                if (
                    not isinstance(event, dict)
                    or integer(event.get("id"), 1, MAX_EVENT_ID)
                    > proof["offered_highwater"]
                ):
                    raise StateError("invalid_cursor_response")
            self._channel(sid)
            return [event for event in events if event["id"] > last_id]
        except StateError as exc:
            self._channel(sid, exc.reason)
        except urllib.error.HTTPError as exc:
            exc.close()
            self._channel(sid, "event_http_refused")
        except (ValueError, UnicodeError):
            self._channel(sid, "invalid_cursor_response")
        except Exception as exc:
            log.warning("Failed to fetch events from %s: %s", server.get("name"), exc)
            self._channel(sid, "event_fetch_failed")
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
        active = self.get_active()
        for server in self.store.list_servers():
            if not server["enabled"]:
                continue
            try:
                self._poll_server(server, active)
            except Exception as e:
                # One bad agent must not stall polling for the rest (Kimi).
                log.error("Polling server %s failed: %s", server.get("name"), e)

        if active:
            self.drain_outbox()

        try:
            self.store.prune_dead_letters()
        except Exception as e:
            log.error("Dead-letter prune failed: %s", e)

    def _poll_server(self, server: dict, active: bool) -> None:
        # Snapshot the agent's status (reachable + raw /status JSON) so status.md
        # stays fresh for OpenClaw — independent of whether there are new events.
        reachable, status_json = self.fetch_status(server)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if reachable:
            # V2 only wrote status_log on a SUCCESSFUL poll, so OpenClaw reading
            # the latest row directly always sees the last GOOD status — never a
            # failure placeholder during an outage (#38 review).
            self.store.log_status(server["id"], True, status_json)
        with self._snap_lock:
            prev = self._status_snapshot.get(server["id"], {})
            if reachable:
                self._status_snapshot[server["id"]] = {
                    "reachable": True,
                    "status_json": status_json,
                    # Parse once here (#107) so /status reads are parse-free.
                    "parsed_status": self._parse_status(status_json),
                    "last_seen": now_str,
                }
            else:
                # Keep the last good payload + last_seen for status.md; don't
                # pollute status_log with an empty row.
                self._status_snapshot[server["id"]] = {
                    "reachable": False,
                    "status_json": prev.get("status_json"),
                    "parsed_status": prev.get("parsed_status"),
                    "last_seen": prev.get("last_seen"),
                }

        if not reachable:
            self._channel(server["id"], "current_status_unavailable")
            return
        new_events = self.fetch_events(server, self._parse_status(status_json))
        if not new_events:
            return

        max_id = self.last_event_ids.get(server["id"], -1)
        for ev in new_events:
            self.store.store_event(server["id"], ev)
            if active:
                msg = f"TaskPaw Event | {server['name']}: {ev.get('message', 'Unknown event')}"
                # Idempotent: a crash before ack-persist re-fetches the same
                # event; the dedupe key keeps OpenClaw from being double-sent.
                self.store.enqueue_delivery(
                    server_name=server["name"],
                    kind="event",
                    payload_json=json.dumps({"text": msg}),
                    dedupe_key=f"{server['id']}:{ev.get('id')}",
                )
            max_id = max(max_id, ev.get("id", max_id))

        with self._acks_lock:  # serialize vs snapshot_acks() (API thread)
            prev_ack = self.last_event_ids.get(server["id"])
            self.last_event_ids[server["id"]] = max_id
            if not self._persist_acks():
                # Roll back the in-memory ack so the next poll re-fetches.
                if prev_ack is None:
                    self.last_event_ids.pop(server["id"], None)
                else:
                    self.last_event_ids[server["id"]] = prev_ack
