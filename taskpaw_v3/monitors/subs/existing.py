"""Existing subtitles, recognised from ONE folder listing (#191).

The owner's rules (2026-09-25): a film counts as already subtitled — and is
skipped entirely — when

- **(a)** `<stem>.srt` exists (any subtitle extension), or
- **(b)** `<stem>.<tags>.srt` exists and no tag is Japanese, or
- **(c)** (only where the caller enables it) its folder holds only this one
  video and any subtitle without a Japanese tag that no other video owns.

`judge` is pure and total: names in, the matching names out — no I/O, and it
never raises (garbage input is ignored). The listing helpers are the only I/O
here: `list_names` never raises (an unreadable folder is None, F14) and
`list_names_missing_ok` (Jasna's planning listing) raises on every failure
except a genuinely missing folder (F13/F18).

Names are compared as `norm(s)` = NFC + casefold. A subtitle is attributed to
the video in the listing (any common video extension, plus the caller's own
filter and `extra_videos`) whose normalised stem is the LONGEST one equal to
its base or followed by a `.` there (F2/F17): `Movie.part2.srt` belongs to
`Movie.part2.mp4`, never to `Movie.mp4`; `X-cd1.srt` never to `X-cd10.mp4`.
"""

from __future__ import annotations

import functools
import logging
import ntpath
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Union

from taskpaw_v3.monitors.subs.job import (
    SUBTITLE_EXISTS,
    SUBTITLE_UNREADABLE,
    SkipReason,
)

log = logging.getLogger("taskpaw.subs.existing")

SUBTITLE_EXTENSIONS = (".srt", ".ass", ".ssa", ".vtt")
#: Common video containers — Jasna's CLI accepts exactly these. Any of them
#: owns its subtitles for attribution, whatever the caller processes (F17).
VIDEO_EXTENSIONS = frozenset({".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm"})


def qualifies(exts: set[str] | frozenset[str], name: str) -> bool:
    """A video a task processes (#179/#191): extension (without the dot) in
    `exts`, case-insensitive; no `.tmp.` in the name (a staging/temp file,
    e.g. Jasna's `x-破解.tmp.mp4`); not a macOS `._` AppleDouble file.
    Shared by avsubs and Jasna so both filter videos identically."""
    suffix = os.path.splitext(name)[1][1:].casefold()
    return (
        bool(suffix)
        and suffix in exts
        and ".tmp." not in name.casefold()
        and not name.startswith("._")
    )


#: A subtitle carrying one of these tags is Japanese and never counts as
#: Chinese (F3).
JAPANESE_TAGS = frozenset(
    {"ja", "jp", "jpn", "jap", "japanese", "日语", "日文", "日本語"}
)
_TAG_SEP = re.compile(r"[._-]")

PathLike = Union[str, "os.PathLike[str]"]


@functools.lru_cache(maxsize=1 << 16)
def norm(name: str) -> str:
    """A name as the rules compare it: NFC, casefolded. Cached: a planning
    pass judges every film against the same listing."""
    return unicodedata.normalize("NFC", name).casefold()


# CX2/CX3: names are matched the way the platform's default filesystem
# identifies files — Windows (NTFS): case-insensitive but normalisation-
# sensitive (an NFC and an NFD name are two files); macOS (APFS/HFS+): case-
# and normalisation-insensitive; elsewhere (Linux): exact bytes.
_PLATFORM = sys.platform


def entry_key(name: str) -> str:
    """A name as this platform's default filesystem identifies it (CX2/CX3)
    — used to find a restored file in a listing, so it agrees with the
    filesystem lookups of restore planning."""
    if _PLATFORM == "win32":
        return ntpath.normcase(name)
    if _PLATFORM == "darwin":
        return unicodedata.normalize("NFC", name).lower()
    return name


def _tags(text: str) -> list[str]:
    return [t for t in _TAG_SEP.split(text) if t]


def _japanese(tags: Iterable[str]) -> bool:
    return not JAPANESE_TAGS.isdisjoint(tags)


@dataclass(frozen=True)
class Existing:
    """What one listing says about one video: `chinese` — the name of a
    subtitle that makes it already subtitled (rule a, b or c), else None;
    `ja_transcript` — the name of its `<stem>.ja.srt`, else None."""

    chinese: Optional[str] = None
    ja_transcript: Optional[str] = None


def judge(
    video_name: str,
    names: Iterable[str],
    is_video: Callable[[str], bool],
    *,
    rule_c: bool,
    extra_videos: Iterable[str] = (),
) -> Existing:
    """Whether `video_name` already has subtitles, judged from the entry
    `names` of its folder (AC1). `is_video` is the caller's filter (its
    extension set, no `.tmp.`, no `._`) — rule (c)'s "only video"; for
    attribution any common video extension counts too, and `extra_videos`
    name videos that do not exist yet (Jasna's not-yet-restored media). Never
    raises: anything unexpected is logged and reads as "no subtitles"."""
    try:
        return _judge(video_name, names, is_video, rule_c, extra_videos)
    except Exception:
        log.exception("subs: judging the subtitles of %r failed", video_name)
        return Existing()


def _judge(
    video_name: str,
    names: Iterable[str],
    is_video: Callable[[str], bool],
    rule_c: bool,
    extra_videos: Iterable[str],
) -> Existing:
    video = norm(video_name)
    me = os.path.splitext(video)[0]
    if not me:
        return Existing()
    pool = [n for n in names if isinstance(n, str) and n and not n.startswith("._")]
    single = rule_c and _only_video(pool, is_video) == video
    # Unless rule (c) applies, only names that start with the stem matter: an
    # (a)/(b) subtitle does, and so does every stem that could own it instead
    # (a longer prefix of its base).
    listed = [(n, nn) for n in pool for nn in (norm(n),) if single or nn.startswith(me)]
    stems = {me}
    for n, nn in listed:
        stem, ext = os.path.splitext(nn)
        if ext in VIDEO_EXTENSIONS or is_video(n):
            stems.add(stem)
    for v in extra_videos:
        if isinstance(v, str) and v:
            stem = os.path.splitext(norm(v))[0]
            if single or stem.startswith(me):
                stems.add(stem)
    me_tokens = set(_tags(me))
    a = b = c = ja = None
    for n, nn in listed:
        sub_ext = next((e for e in SUBTITLE_EXTENSIONS if nn.endswith(e)), None)
        if sub_ext is None:
            continue
        base = nn[: -len(sub_ext)]
        if not base:
            continue
        owner = _owner(base, stems)
        if owner == me:
            if base == me:
                a = a or n  # (a): no tags at all
            else:
                tags = _tags(base[len(me) + 1 :])  # F12: only after the stem
                if sub_ext == ".srt" and base == me + ".ja":
                    ja = ja or n
                if not _japanese(tags):
                    b = b or n
        # IR1: an attributed subtitle is judged by (a)/(b) only — (c) never
        # re-reads it (else `JA-001.mp4`'s own `JA-001.ja.srt` would count).
        if single and c is None and owner is None:
            if not _japanese(t for t in _tags(base) if t not in me_tokens):
                c = n
    return Existing(a or b or c, ja)


def _only_video(pool: list[str], is_video: Callable[[str], bool]) -> Optional[str]:
    """The normalised name of the ONE qualifying video in `pool` — None when
    there is none or there are several (stops at the second)."""
    found: Optional[str] = None
    for n in pool:
        if is_video(n):
            if found is not None:
                return None
            found = norm(n)
    return found


def _owner(base: str, stems: set[str]) -> Optional[str]:
    """The longest stem equal to `base` or followed by a `.` in it."""
    if base in stems:
        return base
    i = len(base)
    while True:
        i = base.rfind(".", 0, i)
        if i <= 0:
            return None
        if base[:i] in stems:
            return base[:i]


def skip_reason(
    names: Optional[list[str]],
    video_name: str,
    is_video: Callable[[str], bool],
    *,
    rule_c: bool,
    extra_videos: Iterable[str] = (),
) -> Optional[SkipReason]:
    """The AC5 re-check of a film about to be worked on: `subtitle state
    unreadable` when its folder could not be listed (`names` is None), `subtitle
    exists` when `judge` finds its Chinese subtitle, else None (go on)."""
    if names is None:
        return SUBTITLE_UNREADABLE
    got = judge(video_name, names, is_video, rule_c=rule_c, extra_videos=extra_videos)
    return SUBTITLE_EXISTS if got.chinese is not None else None


# ── listings ──────────────────────────────────────────────────────────────
def _scan(folder: PathLike) -> list[str]:
    with os.scandir(folder) as it:
        return [entry.name for entry in it]


def list_names(folder: PathLike) -> Optional[list[str]]:
    """The entry names of `folder` (one `os.scandir`), or None when it cannot
    be listed. Never raises (F14): ANY exception reads as unreadable, so a
    caller that holds the GPU is never stranded by a listing."""
    try:
        return _scan(folder)
    except Exception as e:
        log.warning("subs: cannot list %s (%s: %s)", folder, type(e).__name__, e)
        return None


def list_names_missing_ok(folder: str) -> list[str]:
    """Jasna's planning listing of its output folder (AC3): the entry names;
    `[]` when the folder is genuinely missing; every other failure raises
    `OSError`. On Windows an unreachable server or share (WinError 53/67)
    also surfaces as `FileNotFoundError` (F13), so that only reads as
    "missing" when the folder is not a root (F18: a share or drive root never
    is) and its parent can be listed and does not contain it."""
    try:
        return _scan(folder)
    except FileNotFoundError:
        if not _missing(folder):
            raise
        return []


def _missing(folder: str) -> bool:
    path = Path(folder)
    if path.parent == path or not path.name:
        return False
    try:
        siblings = _scan(path.parent)
    except OSError as e:
        log.warning(
            "subs: cannot list the parent of %s (%s); treating it as unreachable",
            folder,
            type(e).__name__,
        )
        return False
    # Fail closed (F13/F18): any sibling that even LOOKS like the folder
    # (case / Unicode form folded) means it is not missing.
    want = norm(path.name)
    return all(norm(n) != want for n in siblings)
