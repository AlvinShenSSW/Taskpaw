"""WhisperJAV 1.9.3 driver: engine presets, argv, owned-flag rule, outcome (#177).

Facts verified on the prototype (spec review §12): `--language japanese`,
presets below, `--no-signature` drops the appended signature cue, argparse
accepts unambiguous prefix abbreviations, rc 0 for `empty`/`suspect`, and the
run writes `whisperjav_run.json` next to its outputs. The subtitle path is
always taken from that manifest (`files[0].output`), never computed (D5).
"""

from __future__ import annotations

import hashlib
import json
import logging
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

from taskpaw_v3.monitors.subs import srt
from taskpaw_v3.monitors.subs.srt import Cue, SrtError

log = logging.getLogger("taskpaw.subs.whisperjav")

Engine = Literal["anime-whisper", "large-v3", "large-v2", "qwen3", "custom"]
ENGINES: tuple[Engine, ...] = (
    "anime-whisper",
    "large-v3",
    "large-v2",
    "qwen3",
    "custom",
)
DEFAULT_ENGINE: Engine = "anime-whisper"
PRESETS: dict[str, tuple[str, ...]] = {
    "anime-whisper": ("--mode", "qwen", "--qwen-generator", "anime-whisper"),
    "large-v3": ("--mode", "balanced", "--model", "large-v3"),
    "large-v2": ("--mode", "balanced"),
    "qwen3": ("--mode", "qwen"),
    "custom": (),
}
OWNED_FLAGS = (
    "--output-dir",
    "--output-format",
    "--language",
    "--temp-dir",
    "--no-signature",
)
PRESET_FLAGS = ("--mode", "--model", "--qwen-generator")
# WhisperJAV's own translation options carry an API key on argv (D14).
FORBIDDEN_PREFIX = "--translate"

MANIFEST_NAME = "whisperjav_run.json"
_TAIL_SEP = " | "


def owned_flags_in(extra: str, engine: str) -> list[str]:
    """The flag names in `extra` that TaskPaw owns (exact, `--flag=…`, or an
    argparse prefix of ≥ 4 chars) or that are forbidden (`--translate*`).
    Raises ValueError for unbalanced quotes (shlex)."""
    protected = OWNED_FLAGS + (PRESET_FLAGS if engine != "custom" else ())
    hits: list[str] = []
    for token in shlex.split(extra):
        name = token.split("=", 1)[0]
        if not name.startswith("--"):
            continue
        if name.startswith(FORBIDDEN_PREFIX) or any(
            name == flag or (len(name) >= 4 and flag.startswith(name))
            for flag in protected
        ):
            hits.append(name)
    return hits


def build_argv(
    exe: str, source: Path, out_dir: Path, tmp_dir: Path, engine: str, extra: str
) -> list[str]:
    """The verified argv (§12); `extra` is appended last (shlex-split)."""
    return [
        exe,
        str(source),
        *PRESETS[engine],
        "--language",
        "japanese",
        "--output-dir",
        str(out_dir),
        "--output-format",
        "srt",
        "--temp-dir",
        str(tmp_dir),
        "--no-signature",
        *shlex.split(extra),
    ]


def manifest_path(out_dir: Path) -> Path:
    return out_dir / MANIFEST_NAME


def attempt_dir(staging_root: Path, relpath: str, attempt: int) -> Path:
    """`<staging_root>/<sha1(relpath)[:12]>/attempt-<n>` — one fresh output
    directory per ASR attempt."""
    digest = hashlib.sha1(relpath.encode("utf-8")).hexdigest()[:12]
    return staging_root / digest / f"attempt-{attempt}"


@dataclass(frozen=True)
class AsrOutcome:
    kind: Literal["succeeded", "no_speech", "failed"]
    cues: tuple[Cue, ...]
    detail: str


def _failed(detail: str) -> AsrOutcome:
    return AsrOutcome("failed", (), detail)


def _with_tail(detail: str, tail: str) -> str:
    return f"{detail}{_TAIL_SEP}{tail}" if tail else detail


def _entry_detail(entry: dict[str, Any]) -> str:
    for key in ("detail", "error"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _first_entry(out_dir: Path) -> Optional[dict[str, Any]]:
    path = manifest_path(out_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:  # unreadable / not JSON / not UTF-8
        log.info("whisperjav manifest unreadable (%s)", type(e).__name__)
        return None
    if not isinstance(data, dict):
        return None
    files = data.get("files")
    if not isinstance(files, list) or not files or not isinstance(files[0], dict):
        return None
    return files[0]


def _resolve_output(out_dir: Path, output: str) -> Path:
    """Absolute → as-is; relative → ONLY `out_dir / <name>` — never the process
    CWD, where a same-named file would be read as this media's subtitles."""
    p = Path(output)
    if p.is_absolute():
        return p
    return out_dir / p.name


def read_outcome(out_dir: Path, rc: Optional[int], tail: str) -> AsrOutcome:
    """Map an exited WhisperJAV run (its attempt `out_dir`, exit code and
    bounded output tail) to an `AsrOutcome`. Never raises."""
    if rc is None:
        return _failed("no exit code")
    if rc != 0:
        return _failed(f"exit code {rc}: {tail}")
    entry = _first_entry(out_dir)
    if entry is None:
        return _failed(_with_tail("no manifest", tail))
    state = entry.get("state")
    detail = _entry_detail(entry)
    if state == "empty":
        return AsrOutcome("no_speech", (), detail or "no speech")
    if state in ("done", "suspect"):
        output = entry.get("output")
        if not isinstance(output, str) or not output.strip():
            return _failed(_with_tail("manifest names no output", tail))
        path = _resolve_output(out_dir, output)
        if not path.is_file():
            return _failed(_with_tail(f"output missing: {path.name}", tail))
        try:
            cues = srt.load(path)
        except SrtError as e:
            return _failed(f"unparseable srt: {e}")
        except OSError as e:
            return _failed(f"output missing: {path.name} ({type(e).__name__})")
        if not cues:
            return AsrOutcome("no_speech", (), detail or "no speech")
        note = f"suspect: {detail}" if state == "suspect" else ""
        return AsrOutcome("succeeded", tuple(cues), note)
    return _failed(_with_tail(f"manifest state {state}: {detail}", tail))
