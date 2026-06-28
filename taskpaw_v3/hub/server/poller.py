"""Hub poller: the end-to-end loop (poll → store → OpenClaw outbox).

Carries forward the V2 #14 contract exactly:
- poll `/events?ack=<durable last id>`; fall back to `/events` on 404 (legacy);
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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _agent_base_url(ip: str, port: int) -> str:
    """Build a base URL, bracketing literal IPv6 addresses."""
    host = f"[{ip}]" if ":" in ip else ip
    return f"http://{host}:{port}"

from .openclaw import send_payload
from .store import HubStore

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
    ) -> None:
        self.store = store
        self.openclaw_url = openclaw_url
        self.get_active = get_active   # openclaw_enabled AND token
        self.get_token = get_token     # OpenClaw token
        # Bearer sent to agents when polling. Falls back to the SQLite config row
        # for back-compat; callers should pass one that also honors HubConfig.
        self.get_polling_token = get_polling_token or (
            lambda: self.store.get_config("polling_token", "")
        )
        self.http_timeout = http_timeout
        self._acks_lock = threading.Lock()
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
                seed[row["id"]] = {
                    "reachable": bool(row.get("reachable")),   # honor the column, don't assume
                    "status_json": row.get("status_json"),
                    "last_seen": row.get("last_seen") or row.get("timestamp"),
                }
        except Exception as e:
            log.warning("Could not seed status snapshot: %s", e)
        return seed

    def status_snapshot(self) -> list[dict]:
        """Current status of each ENABLED server for status.md (registration
        order). Servers not yet polled this run show reachable=False/no data.

        Best-effort: the server list (DB) and the in-memory snapshot are read
        separately, so a server removed between the two reads may briefly appear
        stale in one status.md render — acceptable for a human-readable snapshot."""
        with self._snap_lock:
            snaps = {k: dict(v) for k, v in self._status_snapshot.items()}
        out: list[dict] = []
        for s in self.store.list_servers():
            if not s["enabled"]:
                continue
            snap = snaps.get(s["id"], {})
            out.append({
                "name": s["name"],
                "reachable": bool(snap.get("reachable", False)),
                "status_json": snap.get("status_json"),
                "last_seen": snap.get("last_seen"),
            })
        return out

    def snapshot_acks(self) -> dict[int, int]:
        """Thread-safe copy of the ack cursor for the API thread (/status)."""
        with self._acks_lock:
            return dict(self.last_event_ids)

    # ── ack cursor persistence ───────────────────────────────────────────
    def _load_acks(self) -> dict[int, int]:
        raw = self.store.get_config("last_event_ids", "")
        if not raw:
            return {}
        try:
            return {int(k): int(v) for k, v in json.loads(raw).items()}
        except Exception as e:
            log.error("Failed to load last_event_ids: %s", e)
            return {}

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
            resp = urllib.request.urlopen(req, timeout=self.http_timeout)
            body = resp.read().decode("utf-8")
            try:
                json.loads(body)  # validate it's JSON before persisting
            except json.JSONDecodeError as e:
                # Reachable but returned junk → a misconfigured agent, not an
                # outage. Log distinctly with a body snippet (Kimi).
                log.warning("Bad /status JSON from %s: %s | body[:120]=%r",
                            server.get("name"), e, body[:120])
                return False, None
            return True, body
        except urllib.error.HTTPError as e:
            # Surface 401/403 distinctly — a token mismatch is a config/security
            # problem, not a down agent (Kimi).
            if e.code in (401, 403):
                log.warning("Auth failed polling %s (HTTP %s) — check polling_token",
                            server.get("name"), e.code)
            else:
                log.warning("HTTP %s fetching status from %s", e.code, server.get("name"))
            return False, None
        except Exception as e:
            log.warning("Failed to fetch status from %s: %s", server.get("name"), e)
            return False, None

    def fetch_events(self, server: dict) -> list[dict]:
        base = _agent_base_url(server["ip"], server["port"])
        last_id = self.last_event_ids.get(server["id"], -1)
        q = urllib.parse.urlencode({"ack": last_id})
        try:
            try:
                req = urllib.request.Request(f"{base}/events?{q}", headers=self._auth_headers())
                resp = urllib.request.urlopen(req, timeout=self.http_timeout)
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
                req = urllib.request.Request(f"{base}/events", headers=self._auth_headers())
                resp = urllib.request.urlopen(req, timeout=self.http_timeout)
            events = json.loads(resp.read().decode("utf-8")).get("events", [])
            return [e for e in events if e.get("id", -1) > last_id]
        except Exception as e:
            log.warning("Failed to fetch events from %s: %s", server.get("name"), e)
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
        for row in self.store.due_deliveries(now=now, limit=10):
            try:
                attempts = int(row["attempts"])
                created_at = datetime.fromisoformat(row["created_at"])
                payload = json.loads(row["payload_json"])
            except Exception as e:
                log.error("Dropping malformed outbox row id=%s: %s", row.get("id"), e)
                self.store.delete_delivery(row["id"])
                continue

            if attempts >= 10 or now - created_at > timedelta(hours=24):
                reason = "attempt cap" if attempts >= 10 else "age>24h"
                if self.store.mark_delivery_dead_letter(row["id"], attempts, reason):
                    self.emit_local_alert(f"OpenClaw delivery dead-lettered id={row['id']}: {reason}")
                continue

            try:
                send_payload(self.openclaw_url, self.get_token(), payload, self.http_timeout)
                self.store.delete_delivery(row["id"])
            except Exception as e:
                attempts += 1
                if attempts >= 10:
                    if self.store.mark_delivery_dead_letter(row["id"], attempts, str(e)):
                        self.emit_local_alert(f"OpenClaw delivery dead-lettered id={row['id']}: {e}")
                else:
                    self.store.mark_delivery_failed(
                        row["id"], attempts, str(e), now + timedelta(seconds=self._retry_delay(attempts))
                    )

    # ── one poll cycle ───────────────────────────────────────────────────
    def poll_once(self) -> None:
        active = self.get_active()
        if active:
            self.drain_outbox()

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
                    "reachable": True, "status_json": status_json, "last_seen": now_str}
            else:
                # Keep the last good payload + last_seen for status.md; don't
                # pollute status_log with an empty row.
                self._status_snapshot[server["id"]] = {
                    "reachable": False,
                    "status_json": prev.get("status_json"),
                    "last_seen": prev.get("last_seen")}

        new_events = self.fetch_events(server)
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
                    server_name=server["name"], kind="event",
                    payload_json=json.dumps({"text": msg}),
                    dedupe_key=f"{server['id']}:{ev.get('id')}",
                )
            max_id = max(max_id, ev.get("id", max_id))

        with self._acks_lock:  # serialize vs snapshot_acks() (API thread)
            prev = self.last_event_ids.get(server["id"])
            self.last_event_ids[server["id"]] = max_id
            if not self._persist_acks():
                # Roll back the in-memory ack so the next poll re-fetches.
                if prev is None:
                    self.last_event_ids.pop(server["id"], None)
                else:
                    self.last_event_ids[server["id"]] = prev
