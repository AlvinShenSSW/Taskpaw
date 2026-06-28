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
            json.loads(body)  # validate it's JSON before persisting
            return True, body
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
        # Snapshot the agent's status every poll (reachable + raw /status JSON)
        # so status_log / status.md stay fresh for OpenClaw — independent of
        # whether there are new events (#38).
        reachable, status_json = self.fetch_status(server)
        self.store.log_status(server["id"], reachable, status_json)

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
