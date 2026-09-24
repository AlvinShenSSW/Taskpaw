"""Strict SRT parse / serialize (#177).

`parse` accepts what WhisperJAV and ordinary editors write (UTF-8 BOM, CRLF,
`,` or `.` millisecond separator, multi-line cue text) and rejects anything
malformed with `SrtError` rather than guessing. `serialize` always renumbers
from 1 and writes `\\n` line endings with a trailing blank line. An empty file
is a legitimate subtitle (no speech) and parses to `[]`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class Cue:
    index: int
    start_ms: int
    end_ms: int
    text: str


class SrtError(ValueError):
    """The text is not a well-formed SRT document."""


_TS = r"(\d{1,12}):([0-5]\d):([0-5]\d)[,.](\d{3})"
_TIMING = re.compile(rf"{_TS}[ \t]*-->[ \t]*{_TS}[ \t]*")
# Longer digit runs are rejected before int(): Python's int-str digit limit
# would otherwise raise a plain ValueError past the SrtError guards.
_MAX_DIGITS = 12
_BLOCK_SPLIT = re.compile(r"\n(?:[ \t]*\n)+")


def _ms(h: str, m: str, s: str, ms: str) -> int:
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms)


def parse(text: str) -> list[Cue]:
    """Strict parse; raises `SrtError` on the first malformed block."""
    if text.startswith("﻿"):
        text = text[1:]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return []
    cues: list[Cue] = []
    for n, block in enumerate(_BLOCK_SPLIT.split(text.strip("\n")), start=1):
        lines = block.strip("\n").split("\n")
        if len(lines) < 2:
            raise SrtError(f"block {n}: expected an index and a timing line")
        head = lines[0].strip()
        if not (head.isascii() and head.isdigit()) or len(head) > _MAX_DIGITS:
            raise SrtError(f"block {n}: index is not an integer")
        m = _TIMING.fullmatch(lines[1])
        if m is None:
            raise SrtError(f"block {n}: malformed timing line")
        start = _ms(*m.group(1, 2, 3, 4))
        end = _ms(*m.group(5, 6, 7, 8))
        if end < start:
            raise SrtError(f"block {n}: end before start")
        cues.append(Cue(int(head), start, end, "\n".join(lines[2:])))
    return cues


def _ts(ms: int) -> str:
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, milli = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def _cue_text(text: str) -> str:
    """Cue text with no blank line inside (a blank line ends an SRT cue)."""
    return "\n".join(ln for ln in text.splitlines() if ln.strip())


def serialize(cues: Iterable[Cue]) -> str:
    """Renumber 1..n; `\\n` endings; every cue followed by a blank line.
    Defensive (F1): blank/whitespace-only lines inside a cue's text are
    dropped, so no caller can emit a malformed file."""
    parts = [
        f"{i}\n{_ts(c.start_ms)} --> {_ts(c.end_ms)}\n{_cue_text(c.text)}\n\n"
        for i, c in enumerate(cues, start=1)
    ]
    return "".join(parts)


def load(path: Path) -> list[Cue]:
    """Read (`utf-8-sig`) and parse. Undecodable bytes → `SrtError`;
    `OSError` propagates."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as e:
        raise SrtError(f"not UTF-8 ({e.reason})") from None
    return parse(text)
