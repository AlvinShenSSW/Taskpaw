"""One independently stoppable OpenClaw sender backed by the SQLite outbox.

The drain callback owns database admission/retry decisions. This owner provides
one thread and one HTTP helper at a time; it does not queue notifications itself.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Callable

from .upstream_worker import Transport, UpstreamError

log = logging.getLogger(__name__)
Sender = Callable[[str, str, dict, float], None]


class DeliveryWorker:
    def __init__(
        self,
        drain: Callable[[Sender], None],
        *,
        interval: float = 1.0,
        timeout: float = 5.0,
    ) -> None:
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("delivery interval must be positive")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("delivery timeout must be positive")
        self._drain = drain
        self._interval = interval
        # Transport's cleanup has a separate, at-most-one-second allowance.
        self._request_timeout = min(timeout, 4.0)
        self._transport = Transport()
        self._stop = threading.Event()
        self._lifecycle = threading.Lock()
        self._sender = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def stopped(self) -> bool:
        with self._lifecycle:
            return self._thread is None or not self._thread.is_alive()

    def clean(self) -> bool:
        return self._transport.clean()

    def cancelled(self) -> bool:
        return self._stop.is_set()

    def admit_result(self) -> bool:
        return not self.cancelled() and self._transport.admit_result()

    def start(self) -> bool:
        """Start once; a stopped owner restarts only after complete cleanup."""
        with self._lifecycle:
            if self._thread is not None and self._thread.is_alive():
                return not self._stop.is_set()
            if not self._transport.clean():
                return False
            if self._stop.is_set():
                self._transport = Transport()
                self._stop.clear()
            thread = threading.Thread(
                target=self._run, name="hub-delivery", daemon=False
            )
            self._thread = thread
            thread.start()
            return True

    def request_stop(self) -> None:
        """Signal first so a service can cancel all its owners before joining."""
        with self._lifecycle:
            self._stop.set()
            self._transport.cancel()

    def stop(self, timeout: float = 2.0) -> bool:
        """Join within timeout; retain dirty owners rather than replacing them."""
        deadline = time.monotonic() + max(0, timeout)
        self.request_stop()
        with self._lifecycle:
            thread = self._thread
        if thread is threading.current_thread():
            return False
        if thread is not None and thread.ident is not None:
            thread.join(max(0, deadline - time.monotonic()))
            if thread.is_alive():
                return False
        # A manual test runs on the API's threadpool, but shares this same
        # transport. Its request must finish cleanup before Stop can succeed.
        if not self._sender.acquire(timeout=max(0, deadline - time.monotonic())):
            return False
        try:
            if self._transport.clean():
                return True
            # retry_cleanup owns its own one-second maximum. Do not overrun
            # the caller's remaining budget.
            if deadline - time.monotonic() < 1:
                return False
            return self._transport.retry_cleanup()
        finally:
            self._sender.release()

    def send_payload(
        self, url: str, token: str, payload: dict, timeout: float = 5.0
    ) -> None:
        if not self._sender.acquire(blocking=False):
            raise UpstreamError("delivery_busy")
        try:
            if self.cancelled():
                raise UpstreamError("helper_cancelled")
            if not math.isfinite(timeout) or timeout <= 0:
                raise UpstreamError("request_invalid")
            deadline = min(timeout, self._request_timeout)
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            result = self._transport.request(
                {
                    "kind": "openclaw",
                    "url": url,
                    "headers": headers,
                    "payload": payload,
                    "timeout": deadline,
                },
                deadline,
            )
            if not result.get("ok"):
                raise UpstreamError(result.get("reason", "upstream_failed"))
        finally:
            self._sender.release()

    def _run(self) -> None:
        try:
            while not self.cancelled():
                try:
                    self._drain(self.send_payload)
                except Exception:
                    if not self.cancelled():
                        log.error("OpenClaw outbox drain failed")
                if self._stop.wait(self._interval):
                    break
        finally:
            self._transport.cancel()
            # An in-flight manual test still owns its cleanup; stop() waits
            # for its sender lock. Never race that request from this thread.
            if self._sender.acquire(blocking=False):
                try:
                    if not self._transport.retry_cleanup():
                        log.error("OpenClaw delivery cleanup incomplete")
                finally:
                    self._sender.release()
