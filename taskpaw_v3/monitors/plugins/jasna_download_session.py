"""Small download state machine. Owner serializes calls with its launch lock.

The Jasna monitor and the Media Center workflow each own a session in the same
agent process; a destination folder is downloaded into by one of them at a time.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import queue
import threading
import uuid
import weakref
from pathlib import Path

from taskpaw_v3.monitors.plugins.jasna_download import FILES, worker_argv
from taskpaw_v3.monitors.subs.child import ChildProcess, Eof, asr_env

log = logging.getLogger(__name__)

# Destinations a live worker downloads into, across every session (H3 D08),
# each with its owning session so a later claimant can reap an exited worker.
_ACTIVE: dict[str, weakref.ref[DownloadSession]] = {}
_ACTIVE_LOCK = threading.Lock()
BUSY = "LTX models are already downloading into this folder; retry when it finishes."


def _key(destination: str) -> str:
    return os.path.normcase(os.path.normpath(destination))


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
        self._claimed: str | None = None  # the destination this worker holds
        self._leftovers: tuple[str, list[str]] | None = None  # to clean on stop

    def _claim(self, destination: str) -> bool:
        key = _key(destination)
        with _ACTIVE_LOCK:
            holder = _ACTIVE.get(key)
            if holder is not None:
                owner = holder()
                if owner is None:
                    del _ACTIVE[key]  # the session is gone: nothing can reap it
                elif owner is self or not owner._worker_exited():
                    return False
                else:  # a stopped worker ended after nobody ticked it any more
                    owner._release_locked()
            _ACTIVE[key] = weakref.ref(self)
        self._claimed = destination
        return True

    def _worker_exited(self) -> bool:
        child = self.child
        return child is not None and child.poll() is not None

    def _release(self) -> None:
        """Free the destination once no worker of ours can write to it, then
        delete the partial files a stopped worker left (H3 D09)."""
        with _ACTIVE_LOCK:
            self._release_locked()

    def _release_locked(self) -> None:
        claimed, self._claimed = self._claimed, None
        leftovers, self._leftovers = self._leftovers, None
        if claimed is not None:
            holder = _ACTIVE.get(_key(claimed))
            if holder is not None and holder() is self:  # never a new owner's
                del _ACTIVE[_key(claimed)]
        if leftovers is None or _key(leftovers[0]) in _ACTIVE:
            return  # another session downloads there now: its files
        self._clear_parts(*leftovers)

    @staticmethod
    def _clear_parts(destination: str, names: list[str]) -> None:
        for name in names:
            try:
                parts = list(Path(destination).glob(f".{glob.escape(name)}.*.part"))
            except OSError as e:
                log.warning("jasna download: cannot list partials of %s: %s", name, e)
                continue
            for part in parts:  # one locked partial must not keep the others
                try:
                    part.unlink(missing_ok=True)
                except OSError as e:
                    log.warning("jasna download: cannot delete %s: %s", part, e)

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
            if not self._claim(request["destination"]):
                self._fail(BUSY)  # the other owner's worker writes there (D08)
                return False
            # the folder is ours: partials of an earlier crashed worker are stale
            self._clear_parts(request["destination"], request["files"])
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
                else:
                    self._release()
                if self.status is request:
                    self._fail(f"Cannot start downloader ({type(exc).__name__}).")
        if not self.status:
            # A stop whose kill timed out kept the child; reap it once it ends,
            # or its destination claim would be held for the whole process.
            if self.child is not None and self.child.poll() is not None:
                self.child.join_readers(timeout=0.2)
                self.child.close_stdin()
                self.child = None
                self._release()
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
        self._release()
        if rc == 0 and self._complete:
            self.status = {}
            return True
        self._fail(
            self._error
            or "Download did not complete. Check disk space and network, then retry."
        )
        return False

    def stop(self, timeout: float) -> bool:
        request = self.status
        if request.get("destination") and request.get("files"):
            self._leftovers = (request["destination"], list(request["files"]))
        self.status = {}  # invalidate approval before touching the child
        if self.child is None:
            self._release()
            return True
        child = self.child
        child.close_stdin()
        stopped = child.terminate_tree(timeout=max(0, timeout))
        child.join_readers(timeout=0.2)
        if stopped:
            self.child = None
            self._release()
        return stopped
