"""P3 E1/E2 (#304): 片源丢失 — an INBOX source that vanished after intake.

A film that has not started a stage still reads its INBOX members (the input
manifest's items under its package folder) through the copy the intake kept,
and archives or cleans up against them later. When any of them is gone the
film enters 片源丢失 with `{paths, found_at}`: the dispatcher skips it, the
scheduler holds it (`routing_snapshot(hold=...)`), and nothing is deleted. The
check is a plain existence test, at most once per film per `INTERVAL_SECONDS`;
`force` (重新检查) runs it at once and a returned source clears the state.

The state is in memory: a restart finds it again with the first check, so the
time shown is when this controller first saw the files missing.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import board
from . import models as m

if TYPE_CHECKING:
    from .local_runtime import LocalRuntime

log = logging.getLogger("taskpaw.workflow.source_watch")

INTERVAL_SECONDS = 60.0
NOTE = "INBOX 里的文件已不存在 · 不再派发"
_PENDING = ("pending", "skipped")


def started(film: m.BoardFilm) -> bool:
    """Whether any stage after intake has begun (a retry waiting after a
    failure counts: the film no longer depends on its INBOX source to start)."""
    return any(
        s.stage != "intake"
        and (s.status not in _PENDING or s.first_started_at is not None)
        for s in film.stages
    )


class _Unknown(Exception):
    """The INBOX could not be reached: neither lost nor found."""


class SourceWatch:
    def __init__(
        self,
        runtime: LocalRuntime,
        inbox: Path | None,
        *,
        interval: float = INTERVAL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self.runtime, self.inbox, self.interval = runtime, inbox, interval
        self._monotonic, self._on_change = monotonic, on_change
        self._lock = threading.Lock()
        self._lost: dict[str, dict[str, Any]] = {}
        self._checked: dict[str, float] = {}
        self._unknown: set[str] = set()  # the last check could not tell

    # ── reads ───────────────────────────────────────────────────────────
    def ids(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._lost)

    def lost(self) -> dict[str, dict[str, Any]]:
        """Film → `{paths, found_at}` of every film in 片源丢失."""
        with self._lock:
            return {k: {**v, "paths": list(v["paths"])} for k, v in self._lost.items()}

    def get(self, film_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._lost.get(film_id)
            return None if value is None else {**value, "paths": list(value["paths"])}

    def unknown(self, film_id: str) -> bool:
        """Whether the last check could not tell (the INBOX share was
        unreachable): the state shown is then the one before it."""
        with self._lock:
            return film_id in self._unknown

    # ── the check ───────────────────────────────────────────────────────
    def check_candidates(self) -> frozenset[str]:
        """The throttled check of every planned film that still waits to
        restore or transcribe (what the scheduler could route), run by the
        routing pass itself so it never depends on the dispatcher thread
        (#304 IR1). Returns the films now in 片源丢失."""
        if self.inbox is None:
            return self.ids()
        snapshot = self.runtime.store.routing_snapshot(self.runtime.operator)
        for film in snapshot.films:
            if film.planned and "pending" in (film.restore, film.asr):
                self.check(film.film_id)
        return self.ids()

    def check(self, film_id: str, *, force: bool = False) -> dict[str, Any] | None:
        """The film's 片源丢失 state after a (throttled) existence check."""
        if self.inbox is None:
            return self.get(film_id)
        now = self._monotonic()
        with self._lock:
            last = self._checked.get(film_id)
            if not force and last is not None and now - last < self.interval:
                return self._copy(film_id)
            self._checked[film_id] = now
            self._unknown.discard(film_id)
        try:
            missing = self._missing(film_id)
        except _Unknown:  # an unreachable share: unknown, not lost
            with self._lock:
                self._unknown.add(film_id)
            return self.get(film_id)
        except Exception:  # a failed check never stops dispatch, nor flags a film
            log.exception("source check of %s failed", film_id)
            return self.get(film_id)
        return self._settle(film_id, missing)

    def forget(self, film_id: str) -> None:
        with self._lock:
            self._lost.pop(film_id, None)
            self._checked.pop(film_id, None)
            self._unknown.discard(film_id)

    def _copy(self, film_id: str) -> dict[str, Any] | None:
        value = self._lost.get(film_id)
        return None if value is None else {**value, "paths": list(value["paths"])}

    def _settle(self, film_id: str, missing: list[str] | None) -> dict[str, Any] | None:
        """`missing`: None when the film needs no check, else the members that
        are gone (none: the source is whole)."""
        with self._lock:
            was = film_id in self._lost
            if missing:
                found_at: datetime = (
                    self._lost[film_id]["found_at"] if was else self.runtime.clock.now()
                )
                self._lost[film_id] = {"paths": missing, "found_at": found_at}
            else:
                self._lost.pop(film_id, None)
            state = self._copy(film_id)
        if was != (state is not None) and self._on_change is not None:
            try:
                self._on_change()
            except Exception:
                log.exception("source watch change hook failed")
        return state

    def _missing(self, film_id: str) -> list[str] | None:
        rt, inbox = self.runtime, self.inbox
        assert inbox is not None  # `check` returns early without one
        if not inbox.is_dir():
            raise _Unknown
        film = board.board_film(rt, film_id)
        if film is None or film.input_revision is None or started(film):
            return None
        manifest = rt.store.get_input_manifest(
            rt.operator, film_id, film.input_revision
        )
        base = inbox / film.package_path if film.package_path else inbox
        gone = []
        for item in manifest.items:
            path = base.joinpath(*item.relative_path.split("/"))
            try:
                if not path.exists():
                    gone.append(str(path))
            except OSError as exc:
                log.exception("source member check of %s failed: %s", film_id, path)
                raise _Unknown from exc
        if gone and not inbox.is_dir():  # the share dropped while we looked
            raise _Unknown
        return gone
