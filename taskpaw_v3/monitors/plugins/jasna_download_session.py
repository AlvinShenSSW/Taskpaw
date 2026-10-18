"""Small download state machine. Owner serializes calls with its launch lock."""

from __future__ import annotations

import json
import queue
import uuid
from pathlib import Path

from taskpaw_v3.monitors.plugins.jasna_download import FILES, worker_argv
from taskpaw_v3.monitors.subs.child import ChildProcess, Eof, asr_env


class _ProgressQueue(queue.Queue):
    def __init__(self) -> None:
        super().__init__(maxsize=64)

    def put(
        self, item: object, block: bool = True, timeout: float | None = None
    ) -> None:
        # Reader threads must never block on a slow/offline UI. The terminal
        # message follows progress, so dropping oldest preserves completion.
        with self.not_full:
            if self._qsize() == self.maxsize:
                self._get()
            self._put(item)
            self.not_empty.notify()


class DownloadSession:
    def __init__(self) -> None:
        self.status: dict = {}
        self.child: ChildProcess | None = None
        self._lines = _ProgressQueue()
        self._complete = False
        self._error = ""
        self._eof = False

    def request(self, destination: Path, missing: list[str], variant: str) -> None:
        self.status = dict(
            state="awaiting",
            request_id=uuid.uuid4().hex,
            destination=str(destination.resolve()),
            files=list(missing),
            variant=variant,
            total_bytes=sum(FILES[n][0] for n in missing),
            downloaded_bytes=0,
        )

    def respond(self, request_id: str, approved: bool) -> bool:
        if self.child is not None:
            return False
        if (
            self.status.get("state") not in ("awaiting", "error", "paused")
            or self.status.get("request_id") != request_id
        ):
            return False
        self.status["state"] = "approved" if approved else "paused"
        # Even a repeated decline is stale; paused UI offers Download later.
        self.status["request_id"] = uuid.uuid4().hex
        return True

    def _fail(self, message: str) -> None:
        self.status.update(state="error", error=message, request_id=uuid.uuid4().hex)

    def tick(self) -> bool:
        """True only on verified worker completion; owner rechecks missing files."""
        if self.status.get("state") == "approved":
            request = self.status
            payload = json.dumps({k: request[k] for k in ("destination", "files")})
            self._lines = _ProgressQueue()
            self._complete = False
            self._error = ""
            self._eof = False
            self.status.pop("error", None)
            try:
                self.child = ChildProcess(
                    worker_argv(), env=asr_env(), stdin_pipe=True, line_sink=self._lines
                )
                # Stop may exhaust the owner's lock budget while Popen is slow.
                # A child returned after that Stop still belongs to us.
                if self.status is not request:
                    self.stop(0.1)
                    return False
                self.child.write_line(payload)
                if self.status is not request:
                    self.stop(0.1)
                    return False
                self.status["state"] = "downloading"
            except (OSError, ValueError) as exc:
                # A failed write can leave a child: retain ownership for Stop.
                if self.child is not None:
                    self.child.close_stdin()
                if self.status is request:
                    self._fail(f"Cannot start downloader ({type(exc).__name__}).")
        if not self.status:
            return False
        child = self.child
        if child is None:
            return False
        rc = child.poll()
        if rc is not None:
            child.join_readers(timeout=0.2)
        while True:
            try:
                line = self._lines.get_nowait()
            except queue.Empty:
                break
            if isinstance(line, Eof):
                self._eof = True
            if not isinstance(line, str):
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("state") == "complete":
                self._complete = True
            if message.get("state") == "error":
                self._error = str(message.get("error", "Download failed."))[:300]
            value = message.get("downloaded_bytes")
            if isinstance(value, int) and not isinstance(value, bool):
                self.status["downloaded_bytes"] = min(
                    self.status["total_bytes"], max(0, value)
                )
            if message.get("file") in self.status["files"]:
                self.status["file"] = message["file"]
        if rc is None or not self._eof:
            return False
        child.close_stdin()
        self.child = None
        if rc == 0 and self._complete:
            self.status = {}
            return True
        self._fail(
            self._error
            or "Download did not complete. Check disk space and network, then retry."
        )
        return False

    def stop(self, timeout: float) -> bool:
        self.status = {}  # invalidate approval before touching the child
        if self.child is None:
            return True
        child = self.child
        child.close_stdin()
        stopped = child.terminate_tree(timeout=max(0, timeout))
        child.join_readers(timeout=0.2)
        if stopped:
            self.child = None
        return stopped
