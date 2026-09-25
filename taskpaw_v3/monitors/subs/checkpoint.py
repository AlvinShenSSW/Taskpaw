"""`CheckpointStore`: the translator's per-film checkpoint (#192 AC3).

One JSON file per film under `<agent data dir>/subs-checkpoints/`, named by
its key = sha256(`srt.serialize(cues)`) — equal for WhisperJAV's fresh cues
and the reloaded `.ja.srt` (serialize renumbers and normalises, C4/A4). The
file holds, per cue, its translation `zh`, the provider label `by` that made
it, and `refused_by` — the labels that refused it. LABELS only
(`model_label`: model + host): never a key, a userinfo or a port.

- **Writes** are atomic (a unique tmp file in the same folder, fsync,
  `os.replace`): a crash leaves the previous checkpoint or the new one, and
  two writers of one film (an unsupported overlap) can lose progress but
  never corrupt the file.
- **Never raises**: a write failure returns False (the translator raises one
  notice per run and goes on in memory); a corrupt file, an unknown version
  or a cue-count mismatch is renamed `<key>.bad` with a warning and the film
  starts over.
- **`directory=None`** = memory only: nothing is read or written (every test,
  and a run without a config file — C5).
- **Prune**: files of ours untouched for `CHECKPOINT_MAX_AGE_S` go at the
  translator's start.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence, Union

from taskpaw_v3.monitors.subs import srt
from taskpaw_v3.monitors.subs.srt import Cue

log = logging.getLogger("taskpaw.subs.checkpoint")

CHECKPOINT_VERSION = 1
CHECKPOINTS_DIRNAME = "subs-checkpoints"  # under the agent's data dir
CHECKPOINT_MAX_AGE_S = 30 * 24 * 3600

_KEY = re.compile(r"[0-9a-f]{64}")
# Our own files only: `<key>.json`, `<key>.bad` and the `.<key>.*.tmp` a
# crashed write may leave behind. Prune never touches anything else.
_OURS = re.compile(r"(?:[0-9a-f]{64}\.(?:json|bad)|\.[0-9a-f]{64}\..*\.tmp)")


@dataclass(frozen=True)
class SavedCue:
    """One cue's persisted state: `zh` (None = not done), `by` (the label
    that translated it) and `refused_by` (labels that refused it)."""

    zh: Optional[str] = None
    by: Optional[str] = None
    refused_by: tuple[str, ...] = ()


class _Corrupt(ValueError):
    pass


def _valid_key(key: object) -> bool:
    return isinstance(key, str) and _KEY.fullmatch(key) is not None


def _cue_from(obj: object) -> SavedCue:
    if not isinstance(obj, dict):
        raise _Corrupt("cue is not an object")
    zh = obj.get("zh")
    by = obj.get("by")
    refused = obj.get("refused_by", [])
    if zh is not None and not isinstance(zh, str):
        raise _Corrupt("zh is not a string")
    if by is not None and not isinstance(by, str):
        raise _Corrupt("by is not a string")
    if not isinstance(refused, list) or not all(isinstance(r, str) for r in refused):
        raise _Corrupt("refused_by is not a list of labels")
    # A blank translation is never "done" (the engine never produces one).
    done = zh if isinstance(zh, str) and zh.strip() else None
    return SavedCue(zh=done, by=by, refused_by=tuple(dict.fromkeys(refused)))


def _cue_to(c: SavedCue) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if c.zh is not None:
        out["zh"] = c.zh
    if c.by is not None:
        out["by"] = c.by
    if c.refused_by:
        out["refused_by"] = list(c.refused_by)
    return out


class CheckpointStore:
    def __init__(self, directory: Union[Path, str, None]) -> None:
        self._dir: Optional[Path] = Path(directory) if directory is not None else None

    @property
    def directory(self) -> Optional[Path]:
        return self._dir

    @staticmethod
    def key(cues: Iterable[Cue]) -> str:
        """sha256 (hex) of `srt.serialize(cues)` — the film's identity."""
        return hashlib.sha256(srt.serialize(cues).encode("utf-8")).hexdigest()

    def _path(self, key: str, suffix: str) -> Optional[Path]:
        if self._dir is None or not _valid_key(key):
            return None
        return self._dir / f"{key}{suffix}"

    def load(self, key: str, n: int) -> Optional[list[SavedCue]]:
        """The saved states of the film with `n` cues, or None (no directory,
        no file, unreadable, or corrupt — then renamed `.bad`). Never raises."""
        path = self._path(key, ".json")
        if path is None:
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as e:
            log.warning("checkpoint %s: unreadable (%s)", path.name, type(e).__name__)
            return None
        except UnicodeDecodeError:
            self._set_aside(path, "not UTF-8")
            return None
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                raise _Corrupt("not an object")
            if data.get("v") != CHECKPOINT_VERSION:
                raise _Corrupt("unknown version")
            if data.get("key") != key:
                raise _Corrupt("another film's key")
            cues = data.get("cues")
            if not isinstance(cues, list) or len(cues) != n:
                raise _Corrupt("cue count mismatch")
            return [_cue_from(c) for c in cues]
        except (ValueError, RecursionError) as e:  # JSON errors + _Corrupt
            reason = str(e) if isinstance(e, _Corrupt) else "invalid JSON"
            self._set_aside(path, reason)
            return None

    def _set_aside(self, path: Path, reason: str) -> None:
        log.warning(
            "checkpoint %s: %s — starting over (kept as .bad)", path.name, reason
        )
        try:
            os.replace(path, path.with_suffix(".bad"))
        except OSError as e:
            log.warning(
                "checkpoint %s: could not rename (%s)", path.name, type(e).__name__
            )

    def save(self, key: str, film: str, cues: Sequence[SavedCue]) -> bool:
        """Atomically write the film's states. True when written (or when
        there is no directory: memory only); False on any failure — logged,
        never raised."""
        if self._dir is None:
            return True
        path = self._path(key, ".json")
        if path is None:
            log.warning("checkpoint: invalid key — not saved")
            return False
        body = {
            "v": CHECKPOINT_VERSION,
            "key": key,
            "film": str(film),
            "cues": [_cue_to(c) for c in cues],
        }
        tmp: Optional[str] = None
        try:
            text = json.dumps(body, ensure_ascii=False)
            self._dir.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=f".{key}.", suffix=".tmp", dir=self._dir)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            tmp = None
            return True
        except (OSError, ValueError, TypeError) as e:
            log.warning("checkpoint %s: write failed (%s)", path.name, type(e).__name__)
            return False
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError as e:
                    log.warning(
                        "checkpoint %s: tmp cleanup failed (%s)",
                        path.name,
                        type(e).__name__,
                    )

    def delete(self, key: str) -> None:
        """Remove the film's checkpoint (after its zh publish). Never raises."""
        path = self._path(key, ".json")
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            log.warning(
                "checkpoint %s: delete failed (%s)", path.name, type(e).__name__
            )

    def prune(
        self, max_age_s: float = CHECKPOINT_MAX_AGE_S, now: Optional[float] = None
    ) -> int:
        """Remove our files untouched for `max_age_s` (wall clock: file
        mtimes). Returns how many went. Never raises."""
        if self._dir is None:
            return 0
        cutoff = (time.time() if now is None else now) - max_age_s
        removed = 0
        try:
            entries = list(self._dir.iterdir())
        except FileNotFoundError:
            return 0
        except OSError as e:
            log.warning("checkpoint prune: unreadable folder (%s)", type(e).__name__)
            return 0
        for p in entries:
            if not _OURS.fullmatch(p.name):
                continue
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError as e:
                log.warning(
                    "checkpoint prune: %s not removed (%s)", p.name, type(e).__name__
                )
        if removed:
            log.info("checkpoint prune: %d old file(s) removed", removed)
        return removed
