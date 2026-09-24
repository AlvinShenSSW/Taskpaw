"""`jasna` monitor — managed Jasna video-restore queue (#173).

Jasna (https://github.com/Kruk2/jasna) replaces Lada as the owner's managed
video-restore workload. The plugin keeps the LAYOUT of `lada` (folder-in →
folder-out, operator-clicked Start, queue counts, GPU metrics, optional captured
progress) but is built around Jasna's CLI:

  - **one `jasna.exe` process per video**, sequentially, so a batch can resume,
    skip already-restored files, retry a single file and carry per-file flags;
  - a **resolution tier** per file ("1080p" vs "4k", chosen by pixel count from
    `ffprobe`) selecting the clip size and whether the supporter-only `unet-4x`
    secondary upscaler is used (two tickboxes: 1080p on, 4K off by default);
  - Jasna writes to a **staging** name (`<stem>_restored.tmp.mp4`) and the plugin
    `os.replace()`s it to `<stem>_restored.mp4` only on exit 0, so a killed or
    crashed run never leaves a "done" marker (constitution §2 atomic publish);
  - **outcome-based degrade**: with capture off the plugin cannot read Jasna's
    output, so a failed unet-4x launch is simply retried without unet-4x; if that
    succeeds, unet-4x is disabled for THAT TIER for the rest of the run and one
    alert is emitted.

Passive mode (no exe path) just watches an externally-running `jasna` process,
exactly like lada's passive mode.

Process/reader/terminate recipes and the tqdm progress parser are IMPORTED from
`lada.py` (never duplicated, never modified there). No `shell=True`; the child is
terminated in `stop()` (#40 no-orphan guarantee).

「AV 翻译」(#177, `av_translate`): after each restore the published
`<stem>_restored.mp4` is transcribed by WhisperJAV (a GPU child, serial with the
restores) into `<stem>_restored.ja.srt`, which a background `Translator` (the
`llm-worker` client) turns into `<stem>_restored.srt`. The engine lives in the
shared `taskpaw_v3.monitors.subs` package; this plugin owns planning
(`plan_subs`), policy (retry, degrade), counters, events and settlement. Every
planned subtitle job reaches exactly one terminal state, settled only by the
monitor worker thread under `_launch_lock`; blocking side effects of a
settlement are deferred until the lock is released (D8), and only `_dispatch()`
advances the queue on the exit/poll/settle paths (D25/D29).
"""

from __future__ import annotations

import logging
import os
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Literal, Optional

from pydantic import Field, model_validator

from taskpaw_v3.core import gpu_lease
from taskpaw_v3.core.generation import next_generation
from taskpaw_v3.core.llm import get_llm_settings
from taskpaw_v3.monitors.base import (
    BaseMonitorConfig,
    EventEmitter,
    MonitorInstance,
    MonitorPlugin,
    MonitorStatus,
    State,
)
from taskpaw_v3.monitors.plugins.host_metrics import read_gpu
from taskpaw_v3.monitors.plugins.lada import (
    _CRASH_DETAIL_CHARS,
    _CRASH_DETAIL_LINES,
    _NEW_CONSOLE,
    _NO_WINDOW,
    _RECENT_OUTPUT_LINES,
    _cpu_mem,
    parse_progress_line,
    process_alive,
)

# `Translator` and `ChildProcess` are bound as module-level names and looked up
# at call time, so tests can monkeypatch `J.Translator` / `J.ChildProcess` for
# supervisor-created instances (D10). Nothing here comes from `lada.py` (C1).
from taskpaw_v3.monitors.subs.child import ChildProcess, asr_env
from taskpaw_v3.monitors.subs.job import SubsJob
from taskpaw_v3.monitors.subs.srt import Cue, SrtError
from taskpaw_v3.monitors.subs.translate import (
    CANCELLED,
    RunId,
    TranslateRequest,
    TranslateResult,
    Translator,
    needs_llm_key,
)
from taskpaw_v3.monitors.subs.whisperjav import (
    DEFAULT_ENGINE,
    Engine,
    validate_fields,
)

log = logging.getLogger("taskpaw.monitors.jasna")

Tier = Literal["1080p", "4k"]

#: Containers Jasna's own CLI accepts (its folder mode scans the same set).
JASNA_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm"}

# C5: pixel-count tier rule. `height > 1080` would demote 1920x1200 / 2560x1080
# (cinematic 1080p) to the 4K tier and silently strip unet-4x from them.
_TIER_4K_PIXELS = 1920 * 1080 * 1.5

_FINAL_SUFFIX = "_restored.mp4"
# Keeps the .mp4 suffix so Jasna still picks the mp4 muxer for the staging write.
_STAGING_SUFFIX = "_restored.tmp.mp4"

# Flags the plugin owns: letting them through `jasna_extra_args` would fight the
# dedicated fields (argparse last-wins) and, for --output, break the staging
# rename. `--secondary-restoration` is deliberately NOT here: it is the
# documented last-wins override for the tickboxes.
_OWNED_FLAGS = (
    "--input",
    "--output",
    "--output-pattern",
    "--max-clip-size",
    "--temporal-overlap",
    "--codec",
    "--cq",
    "--detection-model",
)

_PROBE_TIMEOUT = 5.0  # fits the supervisor's 5s stop / 10s reconfigure budgets
_ORPHAN_STAGING_AGE = 60.0  # don't sweep a staging file another run may be writing
_ABORT_AFTER_FAILURES = 3
_DEFAULT_DETECTION_MODEL = "rfdetr-v6"
_LARGE_DETECTION_MODEL = "rfdetr-v6-large"
_COMPILING_HINT = "compiling TensorRT engines (first run, 15-60 min): "

# 「AV 翻译」(#177). The staging root lives INSIDE the output folder (D16):
# `plan_queue` scans the input folder only and `sweep_orphan_staging` only looks
# at files, so neither ever sees it. `.avsubs/tmp` is WhisperJAV's --temp-dir.
_SUBS_STAGING = ".avsubs"
_JA_SUFFIX = "_restored.ja.srt"
_ZH_SUFFIX = "_restored.srt"
# Publish temp names are `<target>.<generation>.tmp` (subs/job.py); Start sweeps
# every generation's leftovers — only once they are old enough (#179 M12): an
# `avsubs` task publishing into the same folder uses the same pattern.
_SUBS_TMP_GLOB = "*_restored*.srt.*.tmp"
_SUBS_TMP_MIN_AGE = 600.0  # seconds (mtime)
_SUBS_DISABLE_AFTER = 3
_ASR_MAX_ATTEMPTS = 2

SubsKind = Literal["full", "translate_only", "none"]
Phase = Literal["restore", "subs", "translate"]


# ── pure helpers (unit-tested without a GPU) ──────────────────────────────
def output_path_for(output_folder: str, video: Path) -> Path:
    """The FINAL (published) output for `video` — the skip/done marker."""
    return Path(output_folder) / f"{video.stem}{_FINAL_SUFFIX}"


def staging_path_for(output_folder: str, video: Path) -> Path:
    """The staging name Jasna writes to; renamed to the final name on exit 0."""
    return Path(output_folder) / f"{video.stem}{_STAGING_SUFFIX}"


def tier_for(width: int, height: int) -> Tier:
    """Resolution tier from the measured pixel count (C5)."""
    return "4k" if width * height > _TIER_4K_PIXELS else "1080p"


def _ffprobe_names() -> tuple[str, ...]:
    if sys.platform == "win32":
        return ("ffprobe.exe", "ffprobe")
    return ("ffprobe", "ffprobe.exe")


def find_ffprobe(exe_dir: Optional[str]) -> Optional[str]:
    """Locate ffprobe the way Jasna itself does: `<exe_dir>/tools/ffprobe`, then
    PATH, then `<exe_dir>/ffprobe`. Jasna hard-requires ffprobe (C4), so a miss
    means the launches will fail anyway — the plugin only degrades the tiering."""
    names = _ffprobe_names()
    try:
        if exe_dir:
            for name in names:
                cand = Path(exe_dir) / "tools" / name
                if cand.is_file():
                    return str(cand)
        found = shutil.which("ffprobe")
        if found:
            return found
        if exe_dir:
            for name in names:
                cand = Path(exe_dir) / name
                if cand.is_file():
                    return str(cand)
    except OSError as e:
        # An unreadable/invalid path (long path, dead network drive) — treat as
        # "not found" and let the caller degrade to the 1080p tier.
        log.warning("jasna: ffprobe lookup under %s failed: %s", exe_dir, e)
    return None


def probe_resolution(video: Path, ffprobe: Optional[str]) -> Optional[tuple[int, int]]:
    """`W, H` of the first video stream, or None on ANY failure (A6 command)."""
    if not ffprobe:
        return None
    try:
        # shell=False (list argv) — constitution §2.
        out = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0",
                str(video),
            ],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as e:
        # ffprobe missing / blocked / slower than the budget. Not silent: logged,
        # and the caller falls back to the 1080p tier with a visible detail.
        log.warning("jasna: ffprobe failed for %s: %s", video, e)
        return None
    if out.returncode != 0:
        return None
    lines = (out.stdout or "").strip().splitlines()
    if not lines:
        return None
    parts = lines[0].split(",")
    if len(parts) < 2:
        return None
    try:
        width, height = int(parts[0].strip()), int(parts[1].strip())
    except ValueError:
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def plan_queue(
    input_folder: str, output_folder: str
) -> tuple[list[Path], int, list[tuple[Path, Path]]]:
    """Sorted, NON-recursive scan of the input folder → (pending, done, collisions).

    `done` counts files whose FINAL output already exists (a staging file does not
    count). `collisions` are later files whose final output path (casefolded)
    collides with an earlier file's — e.g. `a.mp4` + `a.mkv` — which would silently
    overwrite each other; they are excluded from `pending` and reported once.
    No name-based exclusion: the validator guarantees input != output, so the
    plugin's own outputs can never be scanned, and a source library may legitimately
    contain a file called `foo_restored.mp4`.

    Raises `OSError` when the input folder cannot be scanned (missing, not a
    directory, permission denied): an unreadable folder is an ERROR the operator
    must see, not an empty queue that leaves the task silently idle
    (constitution §4; Codex 外门 C-2)."""
    entries = sorted(
        (
            f
            for f in Path(input_folder).iterdir()
            if f.is_file() and f.suffix.lower() in JASNA_VIDEO_EXTENSIONS
        ),
        key=lambda p: p.name,
    )
    pending: list[Path] = []
    done = 0
    collisions: list[tuple[Path, Path]] = []
    seen: dict[str, Path] = {}
    for video in entries:
        final = output_path_for(output_folder, video)
        key = str(final).casefold()
        first = seen.get(key)
        if first is not None:
            collisions.append((video, first))
            continue
        seen[key] = video
        try:
            exists = final.exists()
        except OSError:  # unreadable output entry — treat as "not done yet"
            exists = False
        if exists:
            done += 1
        else:
            pending.append(video)
    return pending, done, collisions


def ja_target_for(output_folder: str, video: Path) -> Path:
    """`<out>/<stem>_restored.ja.srt` — the Japanese transcript (C8: derives from
    the same stem as the restored file, so it can never collide)."""
    return Path(output_folder) / f"{video.stem}{_JA_SUFFIX}"


def zh_target_for(output_folder: str, video: Path) -> Path:
    """`<out>/<stem>_restored.srt` — the Simplified-Chinese subtitle."""
    return Path(output_folder) / f"{video.stem}{_ZH_SUFFIX}"


def _exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:  # unreadable entry — treat as "not there yet"
        return False


def subs_kind(output_folder: str, video: Path) -> SubsKind:
    """What a file needs: an existing zh (a 0-byte one counts) → `none`; an
    existing `.ja.srt` (reused by existence only) → `translate_only`; else
    `full` (ASR + translation)."""
    if _exists(zh_target_for(output_folder, video)):
        return "none"
    if _exists(ja_target_for(output_folder, video)):
        return "translate_only"
    return "full"


@dataclass(frozen=True)
class SubsPlan:
    """`for_pending`: the kind each pending file will need once restored;
    `subs_only`: already-restored files that still need subtitles (sorted,
    handled after every pending restore); `total`: planned subtitle jobs."""

    for_pending: dict[Path, SubsKind]
    subs_only: list[Path]
    total: int


def plan_subs(
    input_folder: str,
    output_folder: str,
    pending: list[Path],
    excluded: Iterable[Path],
) -> SubsPlan:
    """Pure second planning layer (C6/D19/D21) — `plan_queue` is untouched.

    Scans the input folder exactly like `plan_queue` (same extensions, sorted,
    non-recursive), skips `pending` and `excluded` (the collision losers from
    `plan_queue`: a loser's `output_path_for()` is the winner's file, so without
    this it would become a duplicate job on the same media and targets), and
    classifies every other video whose restored output exists. Raises `OSError`
    when the input folder cannot be scanned."""
    pending_set = set(pending)
    skip = pending_set | set(excluded)
    entries = sorted(
        (
            f
            for f in Path(input_folder).iterdir()
            if f.is_file() and f.suffix.lower() in JASNA_VIDEO_EXTENSIONS
        ),
        key=lambda p: p.name,
    )
    for_pending: dict[Path, SubsKind] = {
        v: subs_kind(output_folder, v) for v in pending
    }
    subs_only: list[Path] = []
    for video in entries:
        if video in skip:
            continue
        if not _exists(output_path_for(output_folder, video)):
            continue
        if subs_kind(output_folder, video) != "none":
            subs_only.append(video)
    total = sum(1 for k in for_pending.values() if k != "none") + len(subs_only)
    return SubsPlan(for_pending, subs_only, total)


def _default_spawn(argv: list[str]) -> ChildProcess:
    # Resolved at call time so `J.ChildProcess` can be monkeypatched (D10).
    # stdin=DEVNULL (D15) and a merged stdout/stderr tail are ChildProcess's
    # defaults; the env is scrubbed of the LLM key (`asr_env`, AC10).
    return ChildProcess(argv, env=asr_env())


def _survivor_pids(child: Optional[ChildProcess]) -> list[int]:
    """The ASR child's pid plus the tracked descendants still running (for the
    survivor log line). Never raises; test doubles may lack the tracking."""
    if child is None:
        return []
    pids: list[int] = []
    pid = getattr(child, "pid", None)
    if isinstance(pid, int):
        pids.append(pid)
    running = getattr(child, "_tracked_running", None)
    if callable(running):
        try:
            pids.extend(p for p in running() if p not in pids)
        except Exception as e:  # diagnostics only
            log.debug("jasna: survivor scan failed (%s)", e)
    return pids


def _bounded(text: str) -> str:
    """An alert-sized detail: the head (e.g. `exit code 1: …`) plus the end of
    the tail, never more than `_CRASH_DETAIL_CHARS`."""
    text = (text or "").strip()
    if len(text) > _CRASH_DETAIL_CHARS:
        head = text[:80].rstrip()
        text = f"{head} … {text[-(_CRASH_DETAIL_CHARS - 83) :].lstrip()}"
    return text


def sweep_orphan_staging(
    input_folder: str, output_folder: str, max_age: float = _ORPHAN_STAGING_AGE
) -> list[Path]:
    """Best effort: delete `*_restored.tmp.mp4` files in the output folder that no
    source in the input folder could produce AND that haven't been touched for
    `max_age` seconds (leftovers of an earlier hard stop). A fresh mtime means
    something may still be writing it, so it is kept. Returns what was removed."""
    keep: set[str] = set()
    try:
        for f in Path(input_folder).iterdir():
            if f.is_file() and f.suffix.lower() in JASNA_VIDEO_EXTENSIONS:
                keep.add(str(staging_path_for(output_folder, f)).casefold())
        candidates = [p for p in Path(output_folder).iterdir() if p.is_file()]
    except OSError as e:
        log.warning("jasna: staging sweep skipped (%s)", e)
        return []
    now = time.time()
    removed: list[Path] = []
    for cand in candidates:
        if not cand.name.casefold().endswith(_STAGING_SUFFIX.casefold()):
            continue
        if str(cand).casefold() in keep:
            continue
        try:
            if now - cand.stat().st_mtime < max_age:
                continue
            cand.unlink()
        except OSError as e:
            log.warning("jasna: could not sweep %s: %s", cand, e)
            continue
        removed.append(cand)
    return removed


def large_detector_available(exe_dir: Optional[str]) -> bool:
    """Whether the 4K-grade detector weights ship with this Jasna install."""
    if not exe_dir:
        return False
    try:
        weights = Path(exe_dir) / "model_weights" / f"{_LARGE_DETECTION_MODEL}.onnx"
        return weights.is_file()
    except OSError:
        return False


def engines_present(exe_dir: Optional[str]) -> bool:
    """Whether any TensorRT engine has been compiled yet (A5) — drives the
    "compiling engines" detail hint only."""
    if not exe_dir:
        return False
    try:
        return any((Path(exe_dir) / "model_weights").glob("*.engine"))
    except OSError:
        return False


def is_license_failure(tail: str) -> bool:
    """Jasna's unlicensed-unet-4x message (A3). Used ONLY to sharpen the degrade
    alert text in capture mode — never to decide the degrade itself."""
    return "supporter feature" in (tail or "").casefold()


# ── HEVC sample-entry retag (hev1 → hvc1) ─────────────────────────────────
# Jasna muxes HEVC into MP4 with ffmpeg's default sample-entry name `hev1`.
# That is spec-valid, but Apple's AVFoundation only accepts `hvc1`, so Finder
# thumbnails, QuickLook, QuickTime and Safari treat every restored file as
# unsupported. Lada hit exactly this and fixed it in its writer
# (ladaapp/lada@ed2f09e); we cannot patch Jasna's frozen binary, so the publish
# step rewrites the four-byte sample-entry type instead.
#
# This is metadata only: the sample data, the `hvcC` parameter sets and every
# byte offset stay put, which is also all `ffmpeg -c copy -tag:v hvc1` does to
# such a file — without rewriting tens of gigabytes. Non-Apple players accept
# both names.
_ISOBMFF_PATH = (b"moov", b"trak", b"mdia", b"minf", b"stbl", b"stsd")
_HEV1, _HVC1, _HVCC = b"hev1", b"hvc1", b"hvcC"
# A VisualSampleEntry's fixed header before its child boxes (ISO/IEC 14496-12):
# 6 reserved + 2 data_reference_index + 16 pre_defined/reserved + 2 width +
# 2 height + 4 horizresolution + 4 vertresolution + 4 reserved + 2 frame_count +
# 32 compressorname + 2 depth + 2 pre_defined.
_VISUAL_SAMPLE_ENTRY_HEADER = 78
# Termination is guaranteed by the `size < header_len` check below (every
# iteration advances at least 8 bytes); the cap is only a backstop against a
# pathological header-only file.
_MAX_BOXES_SCANNED = 10_000


def _iter_boxes(fh, start: int, end: int):
    """Yield `(type, box_start, payload_start, box_end)` for the boxes in
    `[start, end)`. Stops on anything malformed rather than guessing, and can
    never loop: a box that does not advance the cursor ends the walk."""
    pos, scanned = start, 0
    while pos + 8 <= end:
        scanned += 1
        if scanned > _MAX_BOXES_SCANNED:
            return
        fh.seek(pos)
        header = fh.read(8)
        if len(header) < 8:
            return
        size = int.from_bytes(header[:4], "big")
        btype = header[4:8]
        header_len = 8
        if size == 1:  # 64-bit largesize follows the type
            ext = fh.read(8)
            if len(ext) < 8:
                return
            size = int.from_bytes(ext, "big")
            header_len = 16
        elif size == 0:  # last box in the file (clamped to the parent here)
            size = end - pos
        if size < header_len or pos + size > end:
            return  # truncated or nonsensical — do not guess
        yield btype, pos, pos + header_len, pos + size
        pos += size


def _find_box(fh, start: int, end: int, wanted: bytes):
    """The first `wanted` box in `[start, end)`, as `(payload_start, box_end)`."""
    for btype, _box_start, payload_start, box_end in _iter_boxes(fh, start, end):
        if btype == wanted:
            return payload_start, box_end
    return None


def retag_hevc_hvc1(path: Path) -> str:
    """Rewrite an ISOBMFF file's HEVC sample-entry name from `hev1` to `hvc1`
    in place (four bytes). Returns a status string for the log:
    `patched` / `already-hvc1` / `no-hevc-entry` / `unsupported:<reason>`.
    Never raises and never touches a file it did not fully understand.

    EVERY `trak` is examined, not just the first: a file whose audio (or a
    timecode/chapter) track comes first is perfectly legal, and stopping at
    track 0 would silently leave the video entry untagged."""
    try:
        with open(path, "r+b") as fh:
            moov = _find_box(fh, 0, path.stat().st_size, b"moov")
            if moov is None:
                return "unsupported:no-moov"
            saw_trak = saw_hvc1 = saw_hev1_without_hvcc = False
            for btype, _box_start, trak_body, trak_end in _iter_boxes(fh, *moov):
                if btype != b"trak":
                    continue
                saw_trak = True
                start, end, complete = trak_body, trak_end, True
                for level in _ISOBMFF_PATH[2:]:  # mdia -> minf -> stbl -> stsd
                    found = _find_box(fh, start, end, level)
                    if found is None:
                        # A tkhd-only timecode/chapter track, or a layout we do
                        # not understand: skip this track, keep looking.
                        complete = False
                        break
                    start, end = found
                if not complete:
                    continue
                # stsd payload: 4 version/flags + 4 entry_count, then the entries.
                entries_start = start + 8
                if entries_start > end:
                    continue
                for etype, entry_start, payload_start, box_end in _iter_boxes(
                    fh, entries_start, end
                ):
                    if etype == _HVC1:
                        saw_hvc1 = True
                        continue
                    if etype != _HEV1:
                        continue
                    # Only retag when the parameter sets live in an `hvcC` box:
                    # that is what makes the renamed entry a conformant `hvc1`
                    # one. An entry without it is skipped rather than fatal — a
                    # later entry, or a later track, may still be patchable.
                    children = payload_start + _VISUAL_SAMPLE_ENTRY_HEADER
                    if (
                        children > box_end
                        or _find_box(fh, children, box_end, _HVCC) is None
                    ):
                        saw_hev1_without_hvcc = True
                        continue
                    type_offset = entry_start + 4
                    fh.seek(type_offset)
                    if fh.read(4) != _HEV1:  # an arithmetic slip in the walk
                        return "unsupported:type-mismatch"
                    fh.seek(type_offset)
                    fh.write(_HVC1)
                    # Deliberately NOT fsync'ed: on Windows that flushes the
                    # whole file's dirty cache — hundreds of milliseconds for the
                    # multi-GB output the child just wrote — while this runs
                    # under `_launch_lock`, and it buys nothing. Atomicity is the
                    # `os.replace`; a crash before write-back leaves the STAGING
                    # name, which `plan_queue` never counts as done, so the file
                    # is simply restored again.
                    return "patched"
            if not saw_trak:
                return "unsupported:no-trak"
            if saw_hev1_without_hvcc:
                return "unsupported:no-hvcC"
            return "already-hvc1" if saw_hvc1 else "no-hevc-entry"
    except OSError as e:
        # An unreadable/locked output must never cost us the video — publish it
        # untagged and say so.
        log.warning("jasna: could not retag %s: %s", path, e)
        return f"unsupported:{type(e).__name__}"


def _split_args(extra: str) -> list[str]:
    if not extra.strip():
        return []
    try:
        return shlex.split(extra)
    except ValueError:
        # An unbalanced quote — fall back to whitespace splitting (lada parity)
        # rather than dropping the operator's flags silently.
        return extra.split()


def _flag_names(extra: str) -> list[str]:
    """The `--name` part of every `--name` / `--name=value` token in `extra`."""
    return [t.split("=", 1)[0] for t in _split_args(extra) if t.startswith("--")]


def _selects(name: str, flag: str) -> bool:
    """Whether the option token `name` selects `flag` under argparse's rules:
    the exact name, or an abbreviation (any prefix — Jasna 0.10.0 keeps
    argparse's default `allow_abbrev=True`, so `--inp` selects `--input`).
    `--input-size` does NOT select `--input` (it is longer, not a prefix)."""
    return name == flag or (len(name) > 2 and flag.startswith(name))


def owned_flags_in(extra: str) -> list[str]:
    """Which plugin-owned flags `extra` sets — by exact name, `--flag=value`, or
    an argparse abbreviation (`--inp x` would silently override the generated
    `--input` and restore the wrong file under every queue item's name — Codex
    外门). A substring test would wrongly reject `--input-size` (lada's Codex
    finding), so the check is prefix-of-flag, never flag-in-token."""
    names = _flag_names(extra)
    found: list[str] = []
    for flag in _OWNED_FLAGS:
        if any(_selects(n, flag) for n in names):
            found.append(flag)
    return found


def secondary_overridden(extra: str) -> bool:
    """Whether `extra` carries the documented `--secondary-restoration` override
    (exact, `=value`, or an argparse abbreviation such as `--sec none`), which
    disables the automatic unet-4x degrade because the relaunch would carry the
    same flag."""
    return any(_selects(n, "--secondary-restoration") for n in _flag_names(extra))


def _same_folder(a: str, b: str) -> bool:
    try:
        ra = str(Path(a).expanduser().resolve()).casefold()
        rb = str(Path(b).expanduser().resolve()).casefold()
    except OSError:  # unresolvable path — compare the raw strings instead
        ra, rb = a.strip().casefold(), b.strip().casefold()
    return ra == rb


def _terminate_child(proc: Optional[subprocess.Popen], timeout: float = 5.0) -> None:
    """The ONE terminate/kill recipe (lada's, incl. its OSError guard): graceful
    terminate, then kill. Safe on an already-reaped child."""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()  # graceful first
        try:
            proc.wait(timeout=max(0.1, timeout))
        except subprocess.TimeoutExpired:
            proc.kill()  # then force
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                log.warning(
                    "jasna: child %s survived kill()", getattr(proc, "pid", "?")
                )
    except OSError:
        # The child may have exited between poll() and terminate()
        # (ProcessLookupError etc.) — already gone, nothing to clean up.
        pass


class JasnaConfig(BaseMonitorConfig):
    jasna_exe_path: str = Field(
        "",
        description="Full path to the Jasna EXECUTABLE FILE (e.g. "
        r"C:\Jasna\jasna.exe) — NOT the folder. Set it → MANAGED mode (TaskPaw "
        "runs one Jasna process per video, needs the input/output folders below). "
        "Leave empty → PASSIVE mode (just watch an already-running Jasna).",
    )
    jasna_input_folder: str = Field(
        "",
        description="Folder of videos to restore (scanned once at Start, not "
        "recursive). Required in managed mode.",
    )
    jasna_output_folder: str = Field(
        "",
        description="Folder where the restored videos are written as "
        "<name>_restored.mp4. Required in managed mode, and must be a DIFFERENT "
        "folder from the input. A file whose output already exists is skipped, so "
        "a batch resumes where it left off. Give each Jasna monitor its OWN output "
        "folder: Start sweeps stale *_restored.tmp.mp4 files it does not own.",
    )
    unet4x_1080p: bool = Field(
        True,
        description="1080p tier: run the supporter-only unet-4x secondary "
        "restoration (more detail, more VRAM). On by default.",
    )
    unet4x_4k: bool = Field(
        False,
        description="4K tier: run the supporter-only unet-4x secondary "
        "restoration. Off by default — it does not fit in 8 GB of VRAM at 4K.",
    )
    av_translate: bool = Field(
        False,
        title="AV 翻译",
        description="After each restore, transcribe the restored video with "
        "WhisperJAV and translate it to Simplified Chinese with the agent's LLM "
        "setting: writes <name>_restored.ja.srt (Japanese) and <name>_restored.srt "
        "(Chinese) next to <name>_restored.mp4. GPU work stays serial (restore → "
        "transcribe → next restore); translation runs in the background. Already "
        "restored files without subtitles are handled after the pending restores; "
        "an existing .ja.srt is reused (translation only). Staging lives in the "
        "output folder's .avsubs directory. An existing .ja.srt / .srt is reused "
        "as-is: if you replace a source video under the same name, delete its old "
        ".srt files first.",
    )
    whisperjav_exe_path: str = Field(
        "",
        description="Full path to whisperjav.exe (e.g. "
        r"C:\WhisperJAV\whisperjav.exe) — NOT the folder. Required when AV 翻译 is "
        "ticked.",
    )
    whisperjav_engine: Engine = Field(
        DEFAULT_ENGINE,
        description="WhisperJAV preset: anime-whisper (default; best on the "
        "prototype), large-v3, large-v2, qwen3, or custom (no preset flags — "
        "set --mode/--model/--qwen-generator yourself in the extra args).",
    )
    whisperjav_extra_args: str = Field(
        "",
        description="Extra WhisperJAV flags appended to every transcription, e.g. "
        "--sensitivity aggressive. TaskPaw owns --output-dir, --output-format, "
        "--language, --temp-dir and --no-signature (and, unless the engine is "
        "custom, --mode/--model/--qwen-generator); those, and any argparse "
        "abbreviation of them 4 or more characters long (e.g. --out), are "
        "rejected. --translate* options are always rejected (they "
        "would put an API key on the command line).",
    )
    clip_size_1080p: int = Field(
        90,
        ge=8,
        description="Frames per clip (--max-clip-size) for the 1080p tier. Higher "
        "= better temporal consistency and more VRAM.",
    )
    clip_size_4k: int = Field(
        60,
        ge=8,
        description="Frames per clip (--max-clip-size) for the 4K tier. A file "
        "counts as 4K when its pixel count exceeds 1.5x 1920x1080 (about 3.1 MP, "
        "so 2560x1440 and up); 1920x1200 and 2560x1080 stay 1080p.",
    )
    temporal_overlap: int = Field(
        8,
        ge=0,
        description="Frames of overlap between clips (--temporal-overlap). Jasna "
        "requires 2 x overlap to be smaller than the smallest clip size.",
    )
    codec: Literal["hevc", "h264", "av1"] = Field(
        "hevc",
        description="Output video codec (--codec). hevc matches Jasna's own default.",
    )
    cq: int = Field(
        24,
        ge=0,
        le=63,
        description="Constant-quality level (--cq), 0-63. Lower = better quality "
        "and a bigger file; 24 is Jasna's default.",
    )
    detection_model: str = Field(
        _DEFAULT_DETECTION_MODEL,
        description="Detection model (--detection-model). Left at the default, 4K "
        "files are automatically upgraded to rfdetr-v6-large when those weights "
        "are installed next to jasna.exe.",
    )
    process_name: str = Field(
        "jasna",
        description="Passive mode only: the process to detect. Matches with or "
        "without a trailing '.exe' (Windows: jasna.exe).",
    )
    jasna_extra_args: str = Field(
        "",
        description="Extra Jasna flags appended verbatim to every launch, e.g. "
        "--device cuda:1. The flags TaskPaw owns (--input, --output, "
        "--output-pattern, --max-clip-size, --temporal-overlap, --codec, --cq, "
        "--detection-model) are rejected here — use the fields above. "
        "`--secondary-restoration` here overrides the tickboxes for every file "
        "and disables the automatic unet-4x degrade (the relaunch would carry the "
        "same flag). Passing `--encoder-settings cq=...` alongside --cq is Jasna's "
        "own error.",
    )
    jasna_gpu_monitor: bool = Field(
        True,
        description="Report GPU% / VRAM via nvidia-smi (turn off on a machine "
        "without an NVIDIA GPU).",
    )
    jasna_capture_progress: bool = Field(
        False,
        description="Advanced. Off (default): EACH file opens its OWN console "
        "window with Jasna's progress bar. On: capture Jasna's output into TaskPaw "
        "(no separate window) to show %/fps/ETA in the status pane and to include "
        "the error tail in failure alerts. The 'compiling TensorRT engines' hint "
        "does not depend on this setting — it is derived from whether "
        "model_weights/*.engine exist.",
    )

    @model_validator(mode="after")
    def _validate_jasna(self) -> "JasnaConfig":
        if self.jasna_exe_path.strip():
            missing = [
                field
                for field, value in (
                    ("jasna_input_folder", self.jasna_input_folder),
                    ("jasna_output_folder", self.jasna_output_folder),
                )
                if not value.strip()
            ]
            if missing:
                raise ValueError(
                    "managed Jasna (jasna_exe_path set) needs an input AND output "
                    f"folder; missing: {', '.join(missing)}"
                )
            if _same_folder(self.jasna_input_folder, self.jasna_output_folder):
                # Same folder → the restored files would be rescanned as sources on
                # the next run and every `<stem>_restored.mp4` would collide.
                raise ValueError(
                    "jasna_input_folder and jasna_output_folder must be different "
                    "folders"
                )
        owned = owned_flags_in(self.jasna_extra_args)
        if owned:
            raise ValueError(
                "jasna_extra_args must not set the flags TaskPaw owns "
                f"({', '.join(owned)}); use the dedicated fields instead"
            )
        validate_fields(
            self.whisperjav_exe_path,
            self.whisperjav_engine,
            self.whisperjav_extra_args,
            required=self.av_translate,
            owner="AV 翻译 (av_translate)",
        )
        smallest = min(self.clip_size_1080p, self.clip_size_4k)
        if 2 * self.temporal_overlap >= smallest:
            raise ValueError(
                f"2 * temporal_overlap ({2 * self.temporal_overlap}) must be "
                f"smaller than the smallest clip size ({smallest}) — Jasna's own "
                "rule"
            )
        return self


def build_argv(
    cfg: JasnaConfig,
    exe: str,
    video: Path,
    staging_out: Path,
    tier: Tier,
    unet_enabled: bool,
    large_detector: bool,
) -> list[str]:
    """Pure: the exact argv for one file. Operator extra args come LAST so
    argparse's last-wins makes the documented `--secondary-restoration` override
    work. A list (shell=False) — constitution §2."""
    clip = cfg.clip_size_4k if tier == "4k" else cfg.clip_size_1080p
    detection = cfg.detection_model
    if tier == "4k" and large_detector and detection == _DEFAULT_DETECTION_MODEL:
        detection = _LARGE_DETECTION_MODEL
    return [
        exe,
        "--input",
        str(video),
        "--output",
        str(staging_out),
        "--max-clip-size",
        str(clip),
        "--temporal-overlap",
        str(cfg.temporal_overlap),
        "--secondary-restoration",
        "unet-4x" if unet_enabled else "none",
        "--codec",
        cfg.codec,
        "--cq",
        str(cfg.cq),
        "--detection-model",
        detection,
        *_split_args(cfg.jasna_extra_args),
    ]


class JasnaInstance(MonitorInstance):
    """One managed (or passive) Jasna queue.

    Locks: `_launch_lock` (RLock) guards launch / exit handling / stop. The exit
    branch releases it BEFORE advancing to the next file, so the ffprobe probe
    (up to 5 s) never runs under it; it is re-entrant only so `start()`'s
    idempotent `stop()` and same-thread re-checks can't self-deadlock. `_lock`
    (lada's) guards `_progress` / `_recent_output`, which the reader thread writes
    per line. Lock order is `_launch_lock` → `_lock` only; nothing holding `_lock`
    ever takes `_launch_lock`; `stop()` joins the reader holding neither; and the
    only waits under `_launch_lock` are the non-blocking `Popen`, the
    `os.replace`, the publish-time `hev1` retag (a bounded header walk and a
    four-byte write — deliberately never an fsync), and `_terminate_child`,
    bounded by its timeout (+2 s for the kill reap)."""

    def __init__(self, instance_id: str, config: JasnaConfig) -> None:
        super().__init__(instance_id, config)
        self._launch_lock = threading.RLock()
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._process: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._progress: dict = {}
        self._recent_output: deque[str] = deque(maxlen=_RECENT_OUTPUT_LINES)
        # queue
        self._pending: list[Path] = []
        self._done = 0
        self._failed = 0
        self._total = 0
        # current file
        self._current: Optional[Path] = None
        self._current_tier: Optional[Tier] = None
        self._current_dims: Optional[tuple[int, int]] = None
        self._current_unet = False
        # retry / degrade
        self._unet_retry_pending = False
        self._plain_retry_used = False
        self._run_unet_disabled: dict[str, bool] = {}
        self._last_failure_tail = ""
        self._consecutive_failures = 0
        # run flags
        self._launch_error: Optional[str] = None
        self._batch_done_emitted = False
        self._batch_aborted = False
        self._compiling = False
        self._idle_note = ""
        self._started = False
        self._ffprobe: Optional[str] = None
        self._prev_running: Optional[bool] = None  # passive transition
        # GPU lease (#177 AC9 hooks, backed by `core.gpu_lease` since #179).
        # `_gpu_holder` makes every release pair exactly one acquire even when
        # two exit paths race (tiny leaf lock, taken under nothing but
        # `_launch_lock`). The hold is FILE-SCOPED (#179 C5/D7): a restore's
        # hold that the same file continues with (its ASR, or a same-file
        # retry) is parked in `_carried` and transferred, never given and
        # re-taken. `_gpu_waiting`: a launch was refused; `check()` retries.
        self._gpu_lock = threading.Lock()
        self._gpu_holder: Optional[object] = None
        self._restore_hold: Optional[object] = None
        self._carried: Optional[object] = None
        self._gpu_waiting = False
        # 「AV 翻译」(#177) — per-run subtitle state, reset in start()
        self._spawn: Callable[[list[str]], ChildProcess] = _default_spawn
        self._run: RunId = (instance_id, 0)
        self._phase: Phase = "restore"
        self._plan = SubsPlan({}, [], 0)
        self._kinds: dict[str, SubsKind] = {}
        self._subs_only: list[Path] = []
        self._subs_job: Optional[SubsJob] = None
        self._translator: Optional[Translator] = None
        self._jobs: dict[str, SubsJob] = {}
        self._settled: dict[str, tuple[str, str]] = {}
        self._subs_completed = 0
        self._subs_failed = 0
        self._subs_skipped = 0
        self._subs_consecutive_failures = 0
        self._subs_disabled: Optional[str] = None
        self._subs_key_alerted = False
        self._next_action: Optional[str] = None
        self._deferred: list[Callable[[], None]] = []
        self._advance_requested = False
        self._had_work = False

    @property
    def _cfg(self) -> JasnaConfig:
        return self.config  # type: ignore[return-value]

    def _exe_dir(self) -> Optional[str]:
        path = self._cfg.jasna_exe_path.strip()
        return str(Path(path).parent) if path else None

    # ── lifecycle ──────────────────────────────────────────────────────────
    def start(self, emit: EventEmitter) -> None:
        cfg = self._cfg
        # Idempotent restart (supervisor stop→start / watchdog respawn): kill any
        # child a prior run left, then RESET every per-run field so a stale flag
        # can't suppress the next completion or mask a healthy relaunch (lada #59).
        # #177: the subtitle side (ASR child, translator thread) counts too — a
        # run whose only live part is the translator must still be stopped.
        if (
            self._process is not None
            or self._subs_job is not None
            or self._translator is not None
        ):
            self.stop()
        self._stopping.clear()
        self._process = None
        self._reader = None
        self._pending = []
        self._done = self._failed = self._total = 0
        self._current = None
        self._current_tier = None
        self._current_dims = None
        self._current_unet = False
        self._unet_retry_pending = False
        self._plain_retry_used = False
        self._run_unet_disabled = {}
        self._last_failure_tail = ""
        self._consecutive_failures = 0
        self._launch_error = None
        self._batch_done_emitted = False
        self._batch_aborted = False
        self._compiling = False
        self._idle_note = ""
        self._ffprobe = None
        self._prev_running = None
        self._started = True
        with self._lock:
            self._progress = {}
            self._recent_output.clear()
        self._reset_subs()
        if cfg.jasna_exe_path.strip():
            self._sweep_subs_leftovers()
            self._start_managed(emit)

    def _reset_subs(self) -> None:
        """Every per-run subtitle field back to its initial value, under a NEW
        process-wide generation (results/temp names of an earlier run — or an
        earlier instance object — can never be mistaken for this run's).

        #179 N7: a hold the OLD run still records is released, and the old run's
        waiter entry withdrawn, BEFORE `_run` changes — a late release under the
        new run id would be refused and the lease would stay stuck."""
        with self._gpu_lock:
            had_hold = self._gpu_holder is not None
            self._gpu_holder = None
        if had_hold:
            self._gpu_release()
        gpu_lease.withdraw(self._run)
        self._restore_hold = None
        self._carried = None
        self._gpu_waiting = False
        self._run = (self.instance_id, next_generation())
        self._phase = "restore"
        self._plan = SubsPlan({}, [], 0)
        self._kinds = {}
        self._subs_only = []
        self._subs_job = None
        self._translator = None
        self._jobs = {}
        self._settled = {}
        self._subs_completed = self._subs_failed = self._subs_skipped = 0
        self._subs_consecutive_failures = 0
        self._subs_disabled = None
        self._subs_key_alerted = False
        self._next_action = None
        self._deferred = []
        self._advance_requested = False
        self._had_work = False

    def _sweep_subs_leftovers(self) -> None:
        """Best effort: `*_restored*.srt.<gen>.tmp` publish leftovers untouched
        for `_SUBS_TMP_MIN_AGE` (#179 M12 — a fresh one may belong to another
        task still publishing) and WhisperJAV's `.avsubs/tmp` from a crashed /
        hard-stopped run."""
        out = self._cfg.jasna_output_folder.strip()
        if not out:
            return
        try:
            leftovers = list(Path(out).glob(_SUBS_TMP_GLOB))
        except OSError as e:
            log.warning("jasna: subtitle temp sweep skipped (%s)", e)
            leftovers = []
        now = time.time()
        for tmp in leftovers:
            try:
                if tmp.is_file() and now - tmp.stat().st_mtime >= _SUBS_TMP_MIN_AGE:
                    tmp.unlink()
            except OSError as e:
                log.warning("jasna: could not sweep %s: %s", tmp, e)
        shutil.rmtree(Path(out) / _SUBS_STAGING / "tmp", ignore_errors=True)

    def _emit_launch_error(self, emit: EventEmitter, msg: str) -> None:
        self._launch_error = msg
        gpu_lease.withdraw(self._run)  # nothing launches any more this run
        emit(
            "alert",
            f"{self._cfg.name} error",
            msg,
            dedupe_key=f"{self.instance_id}:launch",
        )

    def _start_managed(self, emit: EventEmitter) -> None:
        cfg = self._cfg
        exe = Path(cfg.jasna_exe_path)
        # The #1 real-world misconfig is pointing at the install FOLDER; Popen
        # would fail that with a cryptic "[WinError 5]". start() NEVER raises.
        try:
            is_dir, exists = exe.is_dir(), exe.exists()
        except OSError as e:
            self._emit_launch_error(emit, f"cannot read {cfg.jasna_exe_path} ({e})")
            return
        if is_dir:
            self._emit_launch_error(
                emit,
                f"jasna_exe_path is a folder ({cfg.jasna_exe_path}); point it at "
                f"the Jasna executable, e.g. {exe / 'jasna.exe'}",
            )
            return
        if not exists:
            self._emit_launch_error(emit, f"jasna not found at {cfg.jasna_exe_path}")
            return

        self._ffprobe = find_ffprobe(self._exe_dir())
        if self._ffprobe is None:
            emit(
                "alert",
                f"{cfg.name}: ffprobe not found",
                "ffprobe was not found next to jasna.exe or on PATH; all files use "
                "the 1080p tier. Jasna itself requires ffprobe, so the launches are "
                "likely to fail until it is installed.",
                dedupe_key=f"{self.instance_id}:ffprobe",
            )

        try:
            pending, done, collisions = plan_queue(
                cfg.jasna_input_folder, cfg.jasna_output_folder
            )
        except OSError as e:
            # Scanning happens only at Start: an unreadable input folder would
            # otherwise leave the task "idle · nothing to process" forever.
            self._emit_launch_error(
                emit,
                f"cannot scan jasna_input_folder {cfg.jasna_input_folder} ({e}); "
                "fix the folder and Start again",
            )
            return
        self._pending = list(pending)
        self._done = done
        self._failed = len(collisions)
        self._total = done + len(pending) + len(collisions)
        if collisions:
            listed = ", ".join(f"{a.name} vs {b.name}" for a, b in collisions[:5])
            emit(
                "alert",
                f"{cfg.name}: output name collisions",
                f"{len(collisions)} file(s) would overwrite another file's output "
                f"and were skipped: {listed}",
                dedupe_key=f"{self.instance_id}:collisions",
            )
        # Orphaned staging files from an earlier hard stop (best effort).
        sweep_orphan_staging(cfg.jasna_input_folder, cfg.jasna_output_folder)

        if cfg.av_translate:
            self._setup_subs(emit, pending, [loser for loser, _ in collisions])
        # D20: only a Start that has work can ever emit `done`.
        self._had_work = bool(self._pending) or bool(self._jobs)

        if not self._pending:
            if cfg.av_translate and self._subs_only:
                # D2/D32: subs-only work starts at once, without jasna.exe — through
                # the dispatch LOOP, so a large translate-only backlog is walked
                # iteratively (D29).
                self._advance_requested = True
                self._dispatch(emit)
                return
            # Nothing to do is NOT an event (AC 6) — just a visible idle detail.
            self._idle_note = f"nothing to process ({done} already restored)"
            return
        self._launch_next(emit)

    def _setup_subs(
        self, emit: EventEmitter, pending: list[Path], excluded: list[Path]
    ) -> None:
        """Plan the run's subtitle jobs, preflight whisperjav.exe and start the
        translator. Never raises (start() never raises)."""
        cfg = self._cfg
        out = cfg.jasna_output_folder
        try:
            plan = plan_subs(cfg.jasna_input_folder, out, pending, excluded)
        except OSError as e:
            # plan_queue just scanned the same folder, so this is a race; the
            # restores still run, subtitles are off for this run — visibly.
            log.warning("jasna %s: subtitle planning failed: %s", self.instance_id, e)
            emit(
                "alert",
                f"{cfg.name}: AV 翻译 disabled",
                f"could not plan subtitles ({e}); restores continue",
                dedupe_key=f"{self.instance_id}:subs-disabled",
            )
            self._subs_disabled = "planning failed"
            return
        self._plan = plan
        exe = cfg.whisperjav_exe_path.strip()
        staging = Path(out) / _SUBS_STAGING
        work: list[tuple[Path, SubsKind]] = [
            (v, k) for v, k in plan.for_pending.items() if k != "none"
        ]
        work += [(v, subs_kind(out, v)) for v in plan.subs_only]
        for video, kind in work:
            self._kinds[video.name] = kind
            self._jobs[video.name] = SubsJob(
                run=self._run,
                job_id=video.name,
                media=output_path_for(out, video),  # C10: the restored file
                relpath=video.name,
                ja_target=ja_target_for(out, video),
                zh_target=zh_target_for(out, video),
                staging_root=staging,
                exe=exe,
                engine=cfg.whisperjav_engine,
                extra=cfg.whisperjav_extra_args,
            )
        self._subs_only = list(plan.subs_only)
        if not self._jobs:
            return
        try:
            exe_ok = Path(exe).is_file()
        except OSError:
            exe_ok = False
        if not exe_ok:
            # D34: every job skipped AND _subs_only emptied, so `done` can fire.
            with self._launch_lock:
                self._subs_disabled = "no_exe"
                self._subs_only = []
                emit(
                    "alert",
                    f"{cfg.name}: whisperjav.exe not found",
                    f"AV 翻译 is off for this run: whisperjav_exe_path ({exe}) is "
                    "not a file. Restores continue; every subtitle job is skipped.",
                    dedupe_key=f"{self.instance_id}:subs-noexe",
                )
                for job_id in list(self._jobs):
                    self._settle(job_id, "skipped", "no_exe", emit)
            return
        try:
            translator = Translator(self._run, name=self.instance_id)
            # Assigned BEFORE the re-check below (CX2): a concurrent stop() either
            # snapshots it (and cancels it) or has already set the flag we see.
            self._translator = translator
            translator.start()
            if self._stopping.is_set():
                # A Stop landed during start(): never leave an idle daemon thread.
                translator.cancel()
                translator.join(1.0)
        except Exception as e:  # a thread that cannot start — never raise here
            log.warning("jasna %s: translator did not start: %s", self.instance_id, e)
            self._translator = None
            with self._launch_lock:
                self._disable_subs(f"translator did not start ({e})", emit)
            self._run_deferred()
            return

    def stop(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + max(0.1, timeout)
        self._stopping.set()
        # #177: cancel the translator FIRST (bounded: ≤ 1 s + a tree kill) so its
        # llm-worker is already going away while we wait for the lock. It takes
        # neither of our locks.
        translator = self._translator
        if translator is not None:
            try:
                translator.cancel()
            except Exception as e:  # never let cleanup abort the child kill
                log.warning(
                    "jasna %s: translator cancel failed: %s", self.instance_id, e
                )
        # Mirrors supervisor.stop()'s timed acquire: the no-orphan guarantee (#40)
        # wins over tidiness, so a lock we can't take within the caller's budget
        # must not stop us from terminating the child.
        acquired = self._launch_lock.acquire(
            timeout=max(0.1, deadline - time.monotonic())
        )
        try:
            proc = self._process
            if acquired:
                if proc is not None and proc.poll() is None:
                    _terminate_child(proc, timeout)
                    # ONLY after killing a LIVE child: an already-exited child is
                    # never left with a stale staging file — see below.
                    self._delete_current_staging()
                    self._gpu_give(self._restore_hold)
                elif proc is not None and proc.poll() == 0:
                    # The child finished between two polls and the worker may never
                    # run check() again: publish its result NOW so a Stop between
                    # files never throws away a completed video (Codex 外门 C-4).
                    # Counters are per-run and the run is ending — the next
                    # start() rescans and counts it as done.
                    self._publish_current()
                    self._process = None
                    self._gpu_give(self._restore_hold)
                elif proc is not None:
                    # Exited non-zero, unpolled: the exit branch handles it, but
                    # the GPU is free now — release the hook (idempotent).
                    self._gpu_give(self._restore_hold)
                self._stop_asr(deadline)
            else:
                log.warning(
                    "jasna %s: stop() could not take the launch lock within %.1fs; "
                    "terminating the child without it (the staging file is left to "
                    "the exit branch / the next start()'s sweep)",
                    self.instance_id,
                    timeout,
                )
                _terminate_child(proc, timeout)
                self._gpu_give(self._restore_hold)
                job = self._subs_job
                if job is not None and job.child is not None:
                    self._kill_asr(job, max(0.1, deadline - time.monotonic()))
                    self._gpu_give(job)
            # #179 M4: whatever still holds the GPU — e.g. a restore's hold
            # carried to the same file's ASR or retry — is given now, and a
            # registered wait is withdrawn: nothing of this run launches again.
            with self._gpu_lock:
                holder = self._gpu_holder
            self._gpu_give(holder)
            gpu_lease.withdraw(self._run)
        finally:
            if acquired:
                self._launch_lock.release()
        reader = self._reader  # joined with NO lock held
        if reader is not None:
            reader.join(timeout=2)
        if translator is not None:
            translator.join(max(0.1, deadline - time.monotonic()))

    def _stop_asr(self, deadline: float) -> None:
        """Under `_launch_lock` (stop): a live ASR child is tree-killed (bounded)
        and its GPU hook released; one that already exited 0 unpolled gets its
        `.ja.srt` published (never the zh — the translator is cancelled)."""
        job = self._subs_job
        # Read `child` ONCE: the deferred cleanup of `_disable_subs` runs without
        # `_launch_lock` and `SubsJob.terminate()` sets it to None concurrently.
        child = job.child if job is not None else None
        if job is None or child is None:
            return
        if child.poll() is None:
            self._kill_asr(job, max(0.1, min(5.0, deadline - time.monotonic())))
        else:
            outcome = job.poll_asr()
            if outcome is not None and outcome.kind == "succeeded":
                err = job.publish_ja(outcome.cues)
                if err is not None:
                    log.warning("jasna %s: %s", self.instance_id, err)
            self._kill_asr(job, 0.5)  # joins readers; no-op once reaped
        self._gpu_give(job)  # C11: released even when a process survived
        if job.child is None:
            # A direct child still running keeps `_subs_job` (and so the
            # live-child guards); the next start()'s stop() tries again.
            self._subs_job = None

    def _kill_asr(
        self, job: SubsJob, timeout: float, emit: Optional[EventEmitter] = None
    ) -> bool:
        """Tree-kill `job`'s ASR child (#179 C11). A False result — a tracked
        process survived — is logged with the pids and, where an `emit` is
        available, alerted once; the caller still releases the hold."""
        child = job.child
        if job.terminate(timeout=timeout):
            return True
        pids = _survivor_pids(child)
        log.error(
            "jasna %s: WhisperJAV processes survived the kill of %s: %s",
            self.instance_id,
            job.job_id,
            pids,
        )
        if emit is not None:
            emit(
                "alert",
                f"{self._cfg.name}: a WhisperJAV process may still be running",
                f"Stopping the transcription of {job.job_id} left a process "
                f"running (pids {pids}); check Task Manager. The GPU is handed "
                "on regardless.",
                dedupe_key=f"{self.instance_id}:subs-survivor",
            )
        return False

    def _delete_current_staging(self) -> None:
        if self._current is None:
            return
        staging = staging_path_for(self._cfg.jasna_output_folder, self._current)
        try:
            staging.unlink(missing_ok=True)
        except OSError as e:
            # A partial file we couldn't remove: harmless (start() sweeps it) but
            # worth a log line rather than a silent pass.
            log.warning("jasna: could not remove staging file %s: %s", staging, e)

    # ── launching ──────────────────────────────────────────────────────────
    def _launch_next(self, emit: EventEmitter) -> None:
        if self._stopping.is_set() or not self._pending:
            self._give_carried()
            return
        # GPU lease FIRST (#179 D5), outside the lock and never blocking: a
        # same-file retry continues with the carried hold (C5/D7); otherwise the
        # lease is tried, and a refusal leaves `_pending` untouched and waits
        # for the next check() — the probe is not even run.
        hold = object()
        carried, self._carried = self._carried, None
        if not (carried is not None and self._gpu_transfer(carried, hold)):
            if self._stopping.is_set():
                return  # stop() gave the carried hold; take nothing new
            if not self._gpu_take(hold):
                self._gpu_waiting = True
                return
        # N5: from here on every path that does not leave a live child gives
        # the hold — the probe and the reader join included.
        launched = False
        try:
            # Peek + probe BEFORE taking the lock: probe_resolution may block up
            # to 5 s and touches no shared state. A relaunch of the SAME file
            # reuses the earlier probe (Q-3). Only this worker thread mutates
            # _pending/_current.
            nxt = self._pending[0]
            if nxt == self._current and self._current_tier is not None:
                tier: Tier = self._current_tier
                dims = self._current_dims
            else:
                dims = probe_resolution(nxt, self._ffprobe)
                tier = tier_for(*dims) if dims else "1080p"
            # The previous file's reader is at EOF (its process exited before we
            # got here); join it so it can't append to the deque we are about to
            # clear.
            prev_reader = self._reader
            if prev_reader is not None and prev_reader.is_alive():
                prev_reader.join(timeout=1)
            launched = self._launch_locked(emit, tier, dims, hold)
        finally:
            if not launched:
                self._gpu_give(hold)

    def _launch_locked(
        self,
        emit: EventEmitter,
        tier: Tier,
        dims: Optional[tuple[int, int]],
        hold: object,
    ) -> bool:
        """The locked half of `_launch_next`; True when a child is now live."""
        cfg = self._cfg
        with self._launch_lock:
            if self._stopping.is_set() or not self._pending:
                return False
            self._phase = "restore"
            video = self._pending.pop(0)
            if video != self._current:  # a NEW file → fresh retry budget
                self._plain_retry_used = False
                self._unet_retry_pending = False
            self._current = video
            self._current_tier = tier
            self._current_dims = dims

            staging = staging_path_for(cfg.jasna_output_folder, video)
            try:
                staging.unlink(missing_ok=True)  # stale partial from an earlier run
            except OSError as e:
                log.warning("jasna: could not clear staging file %s: %s", staging, e)

            exe_dir = self._exe_dir()
            tickbox = cfg.unet4x_4k if tier == "4k" else cfg.unet4x_1080p
            unet = (
                bool(tickbox)
                and not self._run_unet_disabled.get(tier, False)
                and not self._unet_retry_pending
            )
            # An operator `--secondary-restoration` in the extra args wins
            # (argparse last-wins), so the launch is NOT a unet-4x launch in the
            # sense of AC 4: a failure must take the plain retry path, never the
            # "retry without unet → degrade" path (Codex 外门 C-1).
            self._current_unet = unet and not secondary_overridden(cfg.jasna_extra_args)
            argv = build_argv(
                cfg,
                cfg.jasna_exe_path,
                video,
                staging,
                tier,
                unet,
                large_detector_available(exe_dir),
            )

            capture = cfg.jasna_capture_progress
            if sys.platform == "win32":
                flags = _NO_WINDOW if capture else _NEW_CONSOLE
            else:
                flags = 0
            kw: dict = (
                dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
                if capture
                else {}
            )
            with self._lock:
                self._progress = {}
                self._recent_output.clear()
            try:
                # shell=False (list argv) — constitution §2.
                proc = subprocess.Popen(argv, creationflags=flags, **kw)
            except FileNotFoundError:
                self._emit_launch_error(
                    emit, f"jasna not found at {cfg.jasna_exe_path}"
                )
                return False
            except PermissionError as e:
                # WinError 5 — usually a non-executable target or an AV block.
                self._emit_launch_error(
                    emit,
                    f"access denied launching {cfg.jasna_exe_path} ({e}); make sure "
                    "it's the Jasna executable and isn't blocked by antivirus",
                )
                return False
            except (OSError, ValueError) as e:
                # start()/check() must NEVER raise — a raising start() would make
                # the supervisor's watchdog restart-spin on a broken install.
                self._emit_launch_error(emit, f"failed to launch jasna: {e}")
                return False

            self._compiling = not engines_present(exe_dir)
            if self._stopping.is_set():
                # stop() ran between the guard above and Popen — don't orphan it.
                _terminate_child(proc, timeout=2)
                return False
            self._restore_hold = hold  # released by the exit branch / stop()
            self._process = proc
            if capture and proc.stdout is not None:
                self._reader = threading.Thread(
                    target=self._reader_loop,
                    args=(proc,),
                    name=f"jasna-reader-{self.instance_id}",
                    daemon=True,
                )
                self._reader.start()
            return True

    # ── reader thread (capture mode) ───────────────────────────────────────
    def _reader_loop(self, proc: subprocess.Popen) -> None:
        stdout = proc.stdout
        if stdout is None:
            return
        buf = b""
        try:
            while not self._stopping.is_set():
                ch = stdout.read(1)  # byte at a time: tqdm uses \r in-place updates
                if ch == b"":
                    break  # EOF — process exited
                if ch in (b"\r", b"\n"):
                    self._consume_output(buf)
                    buf = b""
                else:
                    buf += ch
            # A crash line printed right before exit may arrive without a trailing
            # \r/\n before the pipe closes — flush it so the reason isn't lost.
            self._consume_output(buf)
        except (OSError, ValueError):
            # Pipe closed / read on a terminated process as the child exits —
            # expected; the reader simply ends.
            pass

    def _consume_output(self, buf: bytes) -> None:
        """Classify one decoded output line: a recognized progress update advances
        `_progress`; any other non-empty line is retained as recent output (so a
        non-zero exit can report the crash reason)."""
        if not buf:
            return
        line = buf.decode("utf-8", "replace")
        with self._lock:
            new_progress = parse_progress_line(line, self._progress)
            if new_progress is not self._progress:
                self._progress = new_progress
            elif line.strip():
                self._recent_output.append(line.strip())

    # ── check (one observation) ────────────────────────────────────────────
    def check(self, emit: EventEmitter) -> MonitorStatus:
        if self._cfg.jasna_exe_path.strip():
            return self._check_managed(emit)
        return self._check_passive(emit)

    def _check_managed(self, emit: EventEmitter) -> MonitorStatus:
        if not self._started:
            if self._launch_error is not None:
                return MonitorStatus(state="error", detail=self._launch_error)
            return MonitorStatus(state="error", detail="not started")
        # (1) translation results first: settlement — and the deferred part of a
        # degrade it triggers — happens before anything below can return, so a
        # restore launch error never strands finished translations (S1).
        self._settle_results(emit)
        # (2) launch error
        if self._launch_error is not None:
            return MonitorStatus(state="error", detail=self._launch_error)
        if self._batch_aborted:
            # Short-circuit: counters are frozen and nothing launches again.
            self._phase = self._live_phase()
            return self._build_status("degraded", detail=self._aborted_detail())
        proc = self._process
        if proc is not None:
            retcode = proc.poll()
            if retcode is not None:
                self._handle_exit(retcode, emit)  # may launch the next file
                if self._launch_error is not None:
                    return MonitorStatus(state="error", detail=self._launch_error)
                if self._batch_aborted:
                    self._phase = self._live_phase()
                    return self._build_status("degraded", detail=self._aborted_detail())
        self._poll_subs(emit)
        self._retry_gpu_wait(emit)
        self._maybe_done(emit)  # D1: evaluated at the end of EVERY check
        if self._launch_error is not None:
            return MonitorStatus(state="error", detail=self._launch_error)
        self._phase = self._live_phase()
        if self._process is not None or self._asr_live():
            return self._build_status("running")
        if self._gpu_waiting:
            # #179: queued work waits for the GPU another task holds.
            return self._build_status("running" if self._translating() else "idle")
        if self._translating():
            return self._build_status("running")
        return self._build_status("idle", detail=self._idle_note or None)

    def _gpu_child_live(self) -> bool:
        return self._process is not None or self._asr_live()

    def _retry_gpu_wait(self, emit: EventEmitter) -> None:
        """#179: a launch refused by the GPU lease is retried once per check;
        a retry that finds nothing needing the GPU withdraws the wait (D12)."""
        if (
            not self._gpu_waiting
            or self._gpu_child_live()
            or self._stopping.is_set()
            or self._batch_aborted
            or self._launch_error is not None
        ):
            return
        self._gpu_waiting = False
        self._advance_requested = True
        self._dispatch(emit)
        if not self._gpu_waiting and not self._gpu_child_live():
            gpu_lease.withdraw(self._run)

    def _asr_live(self) -> bool:
        job = self._subs_job
        return job is not None and job.child is not None

    def _translating(self) -> int:
        """Requests queued in or held by the translator (0 once cancelled)."""
        tr = self._translator
        if tr is None:
            return 0
        return tr.queued() + (1 if tr.in_flight() else 0)

    def _live_phase(self) -> Phase:
        if self._process is not None:
            return "restore"
        if self._asr_live():
            return "subs"
        if self._translating():
            return "translate"
        return "restore"

    def _aborted_detail(self) -> str:
        return (
            f"batch aborted after {_ABORT_AFTER_FAILURES} consecutive failures · "
            f"{self._done}/{self._total} done, {self._failed} failed"
        )

    # ── exit handling (one place, under _launch_lock) ──────────────────────
    def _handle_exit(self, retcode: int, emit: EventEmitter) -> None:
        stopping = False
        with self._launch_lock:
            if self._process is None:
                return  # already handled
            self._process = None  # handled ONCE
            self._next_action = None
            if self._stopping.is_set():
                # Shutting down: a terminate-induced non-zero exit is not a file
                # failure — don't alert, don't relaunch. A clean exit is still
                # published so the finished video survives the Stop (C-4).
                if retcode == 0:
                    self._publish_current()
                stopping = True
            elif retcode == 0:
                self._handle_success(emit)
            else:
                self._handle_failure(retcode, emit)
            # D3: exactly ONE post-lock action, chosen under the lock.
            action = self._next_action or "advance"
            self._next_action = None
            video = self._current
            # #179 C5/D7: the hold is FILE-scoped — kept (carried) when the
            # same file continues with a GPU child: its ASR (a `full` job) or a
            # retry `_requeue_current()` put back at the head of `_pending`.
            carry = not stopping and (
                (
                    action == "subs"
                    and video is not None
                    and self._kinds.get(video.name) == "full"
                )
                or (
                    action == "advance"
                    and video is not None
                    and bool(self._pending)
                    and self._pending[0] == video
                )
            )
            if carry:
                self._carried = self._restore_hold
        if not carry:
            self._gpu_give(self._restore_hold)  # the file's GPU work is over
        if stopping:
            return
        # Act OUTSIDE the lock: _launch_next probes the next file (up to 5 s)
        # before taking the lock, so a concurrent stop() is never held behind the
        # probe. _launch_next re-checks _stopping under the lock before popping.
        if action == "subs" and video is not None:
            self._start_subs(video, emit)  # never dispatches itself (D29)
        else:
            self._advance_requested = True
        # D31/D32: after EITHER action — a subs start that settled synchronously
        # (e.g. translate-only without a key) leaves its request for this loop.
        # The deferred part of a degrade/abort runs here, before check() can
        # return `degraded` (D8).
        self._run_deferred()
        self._dispatch(emit)
        # A continuation nothing consumed (e.g. the run was aborted or stopped
        # meanwhile) must not keep the GPU.
        self._give_carried()

    def _publish_current(self) -> Optional[str]:
        """Atomic publish of the current file's staging output (constitution §2).
        Returns an error string when the rename failed, else None."""
        video = self._current
        if video is None:
            return None
        staging = staging_path_for(self._cfg.jasna_output_folder, video)
        final = output_path_for(self._cfg.jasna_output_folder, video)
        # Retag BEFORE the rename, so the published file is never briefly the
        # unpreviewable `hev1` variant and a failure here costs nothing. A
        # missing staging file is reported by the os.replace below — don't add a
        # second, more confusing line to the log.
        if staging.exists():
            status = retag_hevc_hvc1(staging)
            if status == "patched":
                log.info("jasna: %s retagged hev1 -> hvc1", staging.name)
            elif status.startswith("unsupported") or (
                # `no-hevc-entry` is the expected result for an h264/av1 job —
                # only HEVC needs this tag. It IS worth a warning when we asked
                # Jasna for HEVC and did not get one (Codex 外门).
                status == "no-hevc-entry" and self._cfg.codec == "hevc"
            ):
                log.warning(
                    "jasna: %s kept its ffmpeg codec tag (%s); macOS will not "
                    "preview it",
                    staging.name,
                    status,
                )
        try:
            os.replace(staging, final)
        except OSError as e:
            log.warning("jasna: could not publish %s: %s", final, e)
            return f"could not publish {final.name}: {e}"
        return None

    def _handle_success(self, emit: EventEmitter) -> None:
        tier = self._current_tier or "1080p"
        err = self._publish_current()
        if err is not None:
            # Jasna says it succeeded but we cannot publish the result — the
            # file is NOT done, so count it as a failure rather than lie.
            self._fail_current(emit, err)
            return
        self._done += 1
        self._consecutive_failures = 0
        if self._unet_retry_pending:
            # The plain relaunch after a unet-4x failure worked → unet-4x is
            # unavailable for this tier in this run (AC 4).
            self._emit_degrade(emit, tier)
            self._run_unet_disabled[tier] = True
        self._unet_retry_pending = False
        # #177 (D3): subtitle this file next, or move on.
        name = self._current.name if self._current is not None else None
        if (
            self._cfg.av_translate
            and self._subs_disabled is None
            and name is not None
            and name in self._jobs
            and name not in self._settled
        ):
            self._next_action = "subs"
        else:
            self._next_action = "advance"

    def _emit_degrade(self, emit: EventEmitter, tier: str) -> None:
        label = "4K" if tier == "4k" else "1080p"
        cause = (
            "the supporter key is not activated in Jasna's GUI"
            if is_license_failure(self._last_failure_tail)
            else "the supporter key is not activated or there is not enough VRAM"
        )
        emit(
            "alert",
            f"{self._cfg.name}: unet-4x disabled for the {label} tier",
            f"The file worked without unet-4x: {cause}. unet-4x stays OFF for the "
            f"{label} tier for the rest of this run.",
            dedupe_key=f"{self.instance_id}:unet:{tier}",
        )

    def _handle_failure(self, retcode: int, emit: EventEmitter) -> None:
        cfg = self._cfg
        # Snapshot the tail ONLY here, so the failing launch's output survives the
        # successful relaunch's clear (P-2).
        with self._lock:
            tail = "\n".join(list(self._recent_output)[-_CRASH_DETAIL_LINES:]).strip()
        self._last_failure_tail = tail
        if self._current is not None:
            staging = staging_path_for(cfg.jasna_output_folder, self._current)
            try:
                staging.unlink(missing_ok=True)
            except OSError as e:
                log.warning("jasna: could not remove %s: %s", staging, e)
        if self._current_unet:
            # Retry the SAME file without unet-4x; neither counter moves yet.
            self._unet_retry_pending = True
            self._requeue_current()
            return
        # A plain launch failed. _unet_retry_pending is left AS IS: it means "this
        # file runs without unet from now on" and is reset only when _current
        # changes or after the rc-0 degrade handling (P-3).
        if not self._plain_retry_used:
            self._plain_retry_used = True
            self._requeue_current()
            return
        detail = f"exit code {retcode}"
        if cfg.jasna_capture_progress and tail:
            if len(tail) > _CRASH_DETAIL_CHARS:
                tail = tail[-_CRASH_DETAIL_CHARS:].lstrip()
            detail = f"{detail}\n{tail}"  # bounded tail, never the argv
        self._fail_current(emit, detail)

    def _requeue_current(self) -> None:
        if self._current is not None:
            self._pending.insert(0, self._current)  # front → deterministic order

    def _fail_current(self, emit: EventEmitter, detail: str) -> None:
        cfg = self._cfg
        name = self._current.name if self._current is not None else "?"
        self._failed += 1
        self._consecutive_failures += 1
        emit("alert", f"{cfg.name}: {name} failed", detail)
        if cfg.av_translate and name in self._jobs:
            # #177: a file that never got restored gets no subtitles.
            self._settle(name, "skipped", "restore_failed", emit)
        if (
            self._consecutive_failures >= _ABORT_AFTER_FAILURES
            and not self._batch_aborted
        ):
            if cfg.av_translate:
                # C2: disable subtitles BEFORE the abort — its deferred part
                # (translator cancel, ASR terminate) runs right after this
                # _handle_exit releases the lock, before `degraded` is returned.
                self._disable_subs("batch aborted", emit)
            self._batch_aborted = True
            gpu_lease.withdraw(self._run)  # #179: nothing launches again
            emit(
                "alert",
                f"{cfg.name} batch aborted",
                f"batch aborted after {_ABORT_AFTER_FAILURES} consecutive failures "
                f"| Queue: {self._done}/{self._total} done, {self._failed} failed",
            )

    def _advance(self, emit: EventEmitter) -> None:
        """Never under `_launch_lock`; reached only through `_dispatch()`."""
        if (
            self._batch_aborted
            or self._stopping.is_set()
            or self._launch_error is not None
        ):
            return
        if self._process is not None or self._asr_live():
            return  # D25 safety net: one GPU child at a time
        if self._pending:
            self._launch_next(emit)
            return
        if self._cfg.av_translate and self._subs_disabled is None and self._subs_only:
            self._start_subs(self._subs_only.pop(0), emit)
            return
        self._maybe_done(emit)

    def _dispatch(self, emit: EventEmitter) -> None:
        """The ONLY caller of `_advance()` on the exit/poll/settle/start paths
        (D25/D29). A LOOP, not recursion: a subs-only file that settles inside
        `_start_subs` just sets the request again, so any backlog is walked on
        this one frame. Ends when an advance launches a GPU child, finds nothing
        to do, or the run is stopped/aborted."""
        while self._advance_requested:
            self._advance_requested = False
            self._advance(emit)

    def _run_deferred(self) -> None:
        """Run the blocking side effects queued by settlements (D8) — called by
        every lock holder right after it released `_launch_lock`."""
        while self._deferred:
            fn = self._deferred.pop(0)
            try:
                fn()
            except Exception as e:  # never let cleanup break check()
                log.warning(
                    "jasna %s: deferred cleanup failed: %s", self.instance_id, e
                )

    def _maybe_done(self, emit: EventEmitter) -> None:
        """Emit the batch `done` exactly once, when every part of the run has
        finished (D1). Stop/abort/launch errors never emit; a Start without any
        work never emits (D20)."""
        if not self._had_work or self._batch_done_emitted:
            return
        if (
            self._stopping.is_set()
            or self._batch_aborted
            or self._launch_error is not None
        ):
            return
        if self._pending or self._process is not None:
            return
        av = bool(self._cfg.av_translate)
        if av:
            if self._subs_job is not None or self._subs_only:
                return
            if len(self._settled) < len(self._jobs):
                return
            tr = self._translator
            if tr is not None and (
                tr.queued() or tr.in_flight() or not tr.results.empty()
            ):
                return
        self._batch_done_emitted = True
        self._phase = "restore"
        subs = ""
        if av:
            subs = (
                f" | Subs: {self._subs_completed}/{len(self._jobs)} done, "
                f"{self._subs_failed} failed, {self._subs_skipped} skipped"
            )
        emit(
            "done",
            f"{self._cfg.name} complete",
            f"Jasna processing complete | Queue: {self._done}/{self._total} "
            f"done, {self._failed} failed{subs} | "
            f"{datetime.now():%Y-%m-%d %H:%M:%S}",
        )
        gpu_lease.withdraw(self._run)  # #179: the run needs no GPU any more
        # F2 (constitution §4): the run is over — end the idle translator thread
        # and its llm-worker now instead of at Stop. Bounded, and never under
        # `_launch_lock` (`_maybe_done` only runs outside it).
        translator = self._translator
        if translator is not None:
            self._translator = None
            try:
                translator.cancel()
                translator.join(2.0)
            except Exception as e:  # cleanup must never break check()
                log.warning(
                    "jasna %s: translator shutdown failed: %s", self.instance_id, e
                )

    # ── 「AV 翻译」: subtitle phases (#177) ─────────────────────────────────
    def _gpu_acquire(self) -> bool:
        """Try the process-wide GPU lease (#179); never blocks. A True is
        always paired with one `_gpu_release`. A refusal while stopping
        withdraws the waiter entry at once (D6)."""
        ok = gpu_lease.try_acquire(
            self._run, self._cfg.poll_interval, label=self._cfg.name
        )
        if not ok and self._stopping.is_set():
            gpu_lease.withdraw(self._run)
        return ok

    def _gpu_release(self) -> None:
        gpu_lease.release(self._run)

    def _gpu_take(self, holder: object) -> bool:
        """Acquire the lease for `holder`; False (nothing recorded) when it is
        refused — the caller waits and retries on a later check."""
        if not self._gpu_acquire():
            return False
        with self._gpu_lock:
            self._gpu_holder = holder
        return True

    def _gpu_transfer(self, old: Optional[object], new: object) -> bool:
        """Hand the hold from `old` to `new` without touching the lease (#179
        C5: same-file continuation). False when `old` is not the holder (e.g.
        stop() already gave it)."""
        with self._gpu_lock:
            if old is None or self._gpu_holder is not old:
                return False
            self._gpu_holder = new
        return True

    def _give_carried(self) -> None:
        """Give a carried hold nothing is going to continue with."""
        carried, self._carried = self._carried, None
        self._gpu_give(carried)

    def _gpu_give(self, holder: Optional[object]) -> None:
        """Release the hook once for `holder` — a no-op for any other holder, so
        two racing exit paths can never release twice (D12 strict pairs)."""
        with self._gpu_lock:
            if holder is None or self._gpu_holder is not holder:
                return
            self._gpu_holder = None
        self._gpu_release()

    def _alert_job(self, job_id: str, detail: str, emit: EventEmitter) -> None:
        emit(
            "alert",
            f"{self._cfg.name}: subtitles for {job_id} failed",
            _bounded(detail),  # bounded tail, never the argv
            dedupe_key=f"{self.instance_id}:subs:{job_id}",
        )

    def _settle(
        self, job_id: str, terminal: str, detail: str, emit: EventEmitter
    ) -> None:
        """The ONE place a job becomes terminal (callers hold `_launch_lock`).
        No-op when it already is; 3 consecutive failures → `_disable_subs`."""
        if job_id in self._settled or job_id not in self._jobs:
            return
        self._settled[job_id] = (terminal, detail)
        if terminal == "completed":
            self._subs_completed += 1
            self._subs_consecutive_failures = 0
        elif terminal == "failed":
            self._subs_failed += 1
            self._subs_consecutive_failures += 1
        else:
            self._subs_skipped += 1
            if detail == "no_llm_key":
                self._subs_consecutive_failures = 0
        if (
            self._subs_consecutive_failures >= _SUBS_DISABLE_AFTER
            and self._subs_disabled is None
        ):
            self._disable_subs(
                f"{_SUBS_DISABLE_AFTER} consecutive subtitle failures", emit
            )

    def _disable_subs(self, reason: str, emit: EventEmitter) -> None:
        """Flags + settlement only (may run under `_launch_lock`); the blocking
        part — translator cancel, ASR terminate — is DEFERRED until the lock is
        released (D8/D22), and it only REQUESTS an advance (D25)."""
        if self._subs_disabled is not None:
            return
        self._subs_disabled = reason
        emit(
            "alert",
            f"{self._cfg.name}: AV 翻译 disabled",
            f"Subtitles are off for the rest of this run ({reason}); restores "
            "continue. Unfinished subtitle jobs are skipped.",
            dedupe_key=f"{self.instance_id}:subs-disabled",
        )
        for job_id in list(self._jobs):
            self._settle(job_id, "skipped", "cancelled", emit)
        self._subs_only = []
        translator = self._translator
        job = self._subs_job

        def cleanup() -> None:
            if translator is not None:
                translator.cancel()
            # D26: an exited-but-unpolled child is NOT reaped — still terminate
            # it (harmless on an exited tree), join it and release its hook.
            if job is not None and job.child is not None:
                self._kill_asr(job, 5.0, emit)
                self._gpu_give(job)  # C11: released even when one survived
                # A direct child still running keeps `_subs_job`, so the
                # live-child guards keep blocking launches until `_poll_subs`
                # reaps it.
                if self._subs_job is job and job.child is None:
                    self._subs_job = None
                    self._phase = "restore"
                    self._advance_requested = True

        self._deferred.append(cleanup)

    def _submit_translation(
        self, job: SubsJob, cues: Optional[list[Cue]], emit: EventEmitter
    ) -> None:
        """Under `_launch_lock`. The transcript is loaded first: an empty one
        needs no request (CX3); otherwise the key is checked per job
        (live-apply)."""
        if cues is None:
            try:
                cues = job.load_ja()
            except (SrtError, OSError) as e:
                log.warning("jasna %s: %s: %s", self.instance_id, job.job_id, e)
                self._settle(job.job_id, "failed", "unreadable .ja.srt", emit)
                self._alert_job(job.job_id, "unreadable .ja.srt", emit)
                return
        if not cues:
            # An empty transcript (no speech): the zh is empty too — regardless
            # of the key, since nothing has to be translated.
            err = job.publish_zh([])
            if err is None:
                self._settle(job.job_id, "completed", "no speech", emit)
            else:
                self._settle(job.job_id, "failed", err, emit)
                self._alert_job(job.job_id, err, emit)
            return
        settings = get_llm_settings()
        if not settings.api_key and needs_llm_key(settings.api_base):
            self._settle(job.job_id, "skipped", "no_llm_key", emit)
            if not self._subs_key_alerted:
                self._subs_key_alerted = True
                emit(
                    "alert",
                    f"{self._cfg.name}: no LLM API key",
                    "AV 翻译 needs the agent's LLM API key to translate; files are "
                    "skipped (their .ja.srt is kept) until a key is set.",
                    dedupe_key=f"{self.instance_id}:subs-nokey",
                )
            return
        translator = self._translator
        if translator is None:
            self._settle(job.job_id, "failed", "translator not running", emit)
            self._alert_job(job.job_id, "translator not running", emit)
            return
        translator.submit(TranslateRequest(self._run, job.job_id, tuple(cues)))

    def _start_subs(self, video: Path, emit: EventEmitter) -> None:
        """Start one file's subtitle job. Always called from a dispatching
        context, so it NEVER dispatches itself (D29); terminal branches only set
        flags under the lock, and the GPU release / deferred work run after it
        (D27/D30)."""
        name = video.name
        # #179 C5: the restore's hold when this file was just restored (full
        # kind); None on the subs-only path.
        carried, self._carried = self._carried, None
        job = self._jobs.get(name)
        if job is None or name in self._settled:
            self._gpu_give(carried)
            self._advance_requested = True
            return
        if self._kinds.get(name) == "translate_only":
            self._gpu_give(carried)
            with self._launch_lock:  # no GPU (D12)
                if not self._stopping.is_set() and self._subs_disabled is None:
                    self._submit_translation(job, None, emit)
                    self._advance_requested = True
            self._run_deferred()
            return
        if not (carried is not None and self._gpu_transfer(carried, job)):
            if self._stopping.is_set():
                return  # stop() gave the carried hold; take nothing new
            if not self._gpu_take(job):
                # Refused: the file stays first in line, check() retries.
                self._subs_only.insert(0, video)
                self._gpu_waiting = True
                return
        release_gpu = False
        with self._launch_lock:
            if self._stopping.is_set() or self._subs_disabled is not None:
                release_gpu = True  # D30: no bare return — the release pairs
            else:
                err = job.start_asr(self._spawn)
                if (
                    err is not None
                    and err != "unstable"
                    and job.attempt < _ASR_MAX_ATTEMPTS
                ):
                    err = job.start_asr(self._spawn)  # a failed attempt: retry once
                if self._stopping.is_set():
                    self._kill_asr(job, 2.0, emit)  # D9: stop() raced the spawn
                    if job.child is not None:
                        self._subs_job = job  # keep the live-child guard (C11)
                    release_gpu = True
                elif err is None:
                    self._phase = "subs"
                    self._subs_job = job
                elif err == "unstable":
                    self._settle_unstable(job, emit)
                    release_gpu = True
                    self._advance_requested = True
                else:
                    self._settle(name, "failed", err, emit)
                    self._alert_job(name, err, emit)
                    release_gpu = True
                    self._advance_requested = True
        if release_gpu:
            self._gpu_give(job)
        self._run_deferred()

    def _settle_unstable(self, job: SubsJob, emit: EventEmitter) -> None:
        self._settle(job.job_id, "skipped", "unstable", emit)
        emit(
            "alert",
            f"{self._cfg.name}: {job.job_id} changed during transcription",
            f"{job.media.name} changed or disappeared while it was being "
            "transcribed; its subtitles are skipped for this run.",
            dedupe_key=f"{self.instance_id}:subs-unstable:{job.job_id}",
        )

    def _poll_subs(self, emit: EventEmitter) -> None:
        job = self._subs_job
        if job is None:
            return
        if self._subs_disabled is not None or job.job_id in self._settled:
            # The deferred terminate owns this job (D22). It leaves the job
            # current only when the direct child survived the kill (C11, hold
            # already released): reap it here once it has exited.
            with self._launch_lock:
                if self._stopping.is_set() or self._subs_job is not job:
                    return
                self._reap_survivor(job)
            self._run_deferred()
            self._dispatch(emit)
            return
        release_gpu = False
        with self._launch_lock:
            if (
                self._stopping.is_set()
                or self._subs_disabled is not None
                or job.job_id in self._settled
                or self._subs_job is not job
            ):
                return  # stop() / the deferred terminate owns this job (D22)
            outcome = job.poll_asr()
            if outcome is None:
                return
            terminal = True
            name = job.job_id
            if outcome.kind == "succeeded":
                err = job.publish_ja(outcome.cues)  # D9: under the lock
                if err is not None:
                    self._settle(name, "failed", err, emit)
                    self._alert_job(name, err, emit)
                else:
                    self._submit_translation(job, list(outcome.cues), emit)
            elif outcome.kind == "no_speech":
                err = job.publish_empty()
                if err is None:
                    self._settle(name, "completed", "no speech", emit)
                else:
                    self._settle(name, "failed", err, emit)
                    self._alert_job(name, err, emit)
            elif outcome.kind == "unstable":
                self._settle_unstable(job, emit)
            elif job.attempt < _ASR_MAX_ATTEMPTS:
                err = job.start_asr(self._spawn)  # retry: a new attempt dir
                if self._stopping.is_set():
                    # D35: exactly _start_subs's post-spawn re-check.
                    self._kill_asr(job, 2.0, emit)
                    release_gpu = True
                    if job.child is None:  # else keep the guard (C11)
                        self._subs_job = None
                    terminal = False
                elif err is None:
                    terminal = False  # the job stays current
                elif err == "unstable":  # D33: like a final failure
                    self._settle_unstable(job, emit)
                else:
                    self._settle(name, "failed", err, emit)
                    self._alert_job(name, err, emit)
            else:
                self._settle(name, "failed", outcome.detail, emit)
                self._alert_job(name, outcome.detail, emit)
            if terminal:
                self._subs_job = None
                release_gpu = True
                self._phase = "translate" if self._translating() else "restore"
                self._advance_requested = True  # D28
        if release_gpu:
            self._gpu_give(job)
        self._run_deferred()
        self._dispatch(emit)

    def _reap_survivor(self, job: SubsJob) -> None:
        """Under `_launch_lock`: a settled job whose direct child outlived its
        kill (C11) — once that child has exited, join it, drop the job and
        request an advance. A no-op while it still runs."""
        child = job.child
        if child is None or child.poll() is None:
            return
        job.terminate(timeout=0.5)  # joins the readers, resets `child`
        if job.child is not None:
            return
        self._subs_job = None
        self._phase = "translate" if self._translating() else "restore"
        self._advance_requested = True

    def _settle_results(self, emit: EventEmitter) -> None:
        """Drain the translator's results (worker thread only). Each result is
        settled under `_launch_lock`, `_settled` checked BEFORE any publish (D7);
        deferred work and dispatch run after each release."""
        translator = self._translator
        if translator is None:
            return
        while True:
            try:
                result = translator.results.get_nowait()
            except queue.Empty:
                return
            if result is CANCELLED or not isinstance(result, TranslateResult):
                continue
            if result.run != self._run:
                continue  # an earlier generation's result
            with self._launch_lock:
                if self._stopping.is_set():
                    return
                job = self._jobs.get(result.job_id)
                if job is not None and result.job_id not in self._settled:
                    if result.outcome == "translated":
                        err = job.publish_zh(result.zh_cues)  # D9: under the lock
                        if err is None:
                            self._settle(job.job_id, "completed", "", emit)
                        else:
                            self._settle(job.job_id, "failed", err, emit)
                            self._alert_job(job.job_id, err, emit)
                    else:
                        self._settle(job.job_id, "failed", result.detail, emit)
                        self._alert_job(job.job_id, result.detail, emit)
            self._run_deferred()
            self._dispatch(emit)

    # ── passive ────────────────────────────────────────────────────────────
    def _check_passive(self, emit: EventEmitter) -> MonitorStatus:
        cfg = self._cfg
        running = process_alive(cfg.process_name or "jasna")
        if self._prev_running is True and not running:  # exited → completion
            emit(
                "done",
                f"{cfg.name} complete",
                f"Jasna processing complete | {datetime.now():%Y-%m-%d %H:%M:%S}",
            )
        self._prev_running = running
        return self._build_status("running" if running else "idle")

    # ── status assembly ────────────────────────────────────────────────────
    def _build_status(
        self, state: State, detail: Optional[str] = None
    ) -> MonitorStatus:
        cfg = self._cfg
        metrics: dict = {}
        # Per-task progress / current file are only meaningful while RUNNING —
        # reporting them when idle would tell the UI/Hub a file is being processed
        # when it isn't (lada/Codex #59 parity). #177 (D17): they belong to the
        # phase's live child — the restore child's progress in `restore`, the
        # ASR child's media name in `subs`, nothing while only translating.
        if state == "running" and self._phase == "restore":
            with self._lock:
                metrics.update(self._progress)
            if self._current is not None:
                # Single-file mode prints no filename header, so the queue is the
                # authority for current_file.
                metrics["current_file"] = self._current.name
            if self._compiling and (
                "percent" in metrics or engines_present(self._exe_dir())
            ):
                self._compiling = False
        elif state == "running" and self._phase == "subs":
            job = self._subs_job
            if job is not None and job.child is not None:
                metrics["current_file"] = job.media.name
        if cfg.jasna_exe_path.strip() and self._total:
            metrics["queue_completed"] = self._done
            metrics["queue_total"] = self._total
            metrics["queue_failed"] = self._failed
            metrics["queue_remaining"] = max(0, self._total - self._done - self._failed)
        if cfg.jasna_exe_path.strip():
            metrics["phase"] = self._phase
            if cfg.av_translate:
                total = len(self._jobs)
                metrics["subs_total"] = total
                metrics["subs_completed"] = self._subs_completed
                metrics["subs_failed"] = self._subs_failed
                metrics["subs_skipped"] = self._subs_skipped
                metrics["subs_remaining"] = max(
                    0,
                    total
                    - self._subs_completed
                    - self._subs_failed
                    - self._subs_skipped,
                )
                metrics["subs_translating"] = self._translating()
        metrics.update(_cpu_mem())
        if cfg.jasna_gpu_monitor:
            gpu = read_gpu()
            if gpu:
                metrics["gpu_pct"] = gpu["util_pct"]
                metrics["gpu_mem_used_mb"] = gpu["mem_used_mb"]
                metrics["gpu_mem_total_mb"] = gpu["mem_total_mb"]
        return MonitorStatus(
            state=state, detail=detail or self._detail(state, metrics), metrics=metrics
        )

    def _tier_suffix(self) -> str:
        tier = self._current_tier or "1080p"
        label = "4K" if tier == "4k" else "1080p"
        dims = ""
        if self._current_dims:
            dims = f" {self._current_dims[0]}x{self._current_dims[1]}"
        unet = "unet-4x" if self._current_unet else "unet-4x off"
        return f" [{label}{dims}, {unet}]"

    def _detail(self, state: str, m: dict) -> str:
        # A clean one-line summary with "·" separators (lada parity); the rich
        # view is the UI metrics dashboard.
        parts: list[str] = []
        if self._gpu_waiting and not self._gpu_child_live():
            return self._waiting_detail(m)
        if state == "running" and self._phase in ("subs", "translate"):
            return self._subs_detail(m)
        if state == "running" and self._current is not None:
            head = f"{state}: {self._current.name}{self._tier_suffix()}"
            if self._compiling and "percent" not in m:
                head = _COMPILING_HINT + head
            parts.append(head)
        else:
            parts.append(state)
        if "percent" in m:
            parts.append(f"{m['percent']}%")
        if m.get("eta"):
            parts.append(f"ETA {m['eta']}")
        if "fps" in m:
            parts.append(f"{m['fps']:.1f} fps")
        if "queue_total" in m:
            parts.append(f"{m['queue_completed']}/{m['queue_total']} done")
        if m.get("subs_translating"):
            parts.insert(1, f"translating {m['subs_translating']}")
        return " · ".join(parts)

    def _subs_detail(self, m: dict) -> str:
        subs = f"subs {m.get('subs_completed', 0)}/{m.get('subs_total', 0)}"
        translating = f"translating {m.get('subs_translating', 0)}"
        job = self._subs_job
        if self._phase == "subs" and job is not None and job.child is not None:
            secs = max(0, int(time.monotonic() - job.started_at))
            elapsed = f"{secs // 60:02d}:{secs % 60:02d} elapsed"
            head = f"subtitling: {job.media.name} [{job.engine}]"
            return " · ".join((head, elapsed, translating, subs))
        return f"{translating} · {subs}"

    def _waiting_detail(self, m: dict) -> str:
        """#179: `waiting for GPU (held by <label>)` + the usual queue text."""
        label = gpu_lease.blocking_label()
        parts = [f"waiting for GPU (held by {label})" if label else "waiting for GPU"]
        if m.get("subs_translating"):
            parts.append(f"translating {m['subs_translating']}")
        if "queue_total" in m:
            parts.append(f"{m['queue_completed']}/{m['queue_total']} done")
        if "subs_total" in m:
            parts.append(f"subs {m['subs_completed']}/{m['subs_total']}")
        return " · ".join(parts)


class JasnaPlugin(MonitorPlugin):
    type_id = "jasna"
    display_name = "Jasna (video restore)"
    category = "task"
    config_version = 1

    @classmethod
    def config_model(cls) -> type[BaseMonitorConfig]:
        return JasnaConfig

    @classmethod
    def ui_schema(cls) -> dict:
        # Lead with the fields that matter; path fields are flagged for the
        # file/folder picker widget (#71). Help text comes from the field
        # descriptions (rjsf renders them) — not a ui:help key.
        return {
            "ui:order": [
                "name",
                "jasna_exe_path",
                "jasna_input_folder",
                "jasna_output_folder",
                "unet4x_1080p",
                "unet4x_4k",
                "av_translate",
                "whisperjav_exe_path",
                "whisperjav_engine",
                "whisperjav_extra_args",
                "clip_size_1080p",
                "clip_size_4k",
                "temporal_overlap",
                "codec",
                "cq",
                "detection_model",
                "process_name",
                "jasna_extra_args",
                "jasna_gpu_monitor",
                "jasna_capture_progress",
                "poll_interval",
                "timeout",
                "*",
            ],
            "jasna_exe_path": {"ui:options": {"taskpawPath": "file"}},
            "jasna_input_folder": {"ui:options": {"taskpawPath": "directory"}},
            "jasna_output_folder": {"ui:options": {"taskpawPath": "directory"}},
            "whisperjav_exe_path": {"ui:options": {"taskpawPath": "file"}},
        }

    def manual_start(self, config: BaseMonitorConfig) -> bool:
        # Managed Jasna LAUNCHES jasna.exe on start, so add it STOPPED and let the
        # operator click Start — never begin video processing (or a 15-60 min
        # engine compile) the instant the form is saved. Owner rule: managed Jasna
        # never auto-starts at boot.
        return bool(getattr(config, "jasna_exe_path", "").strip())

    def create(self, instance_id: str, config: BaseMonitorConfig) -> MonitorInstance:
        return JasnaInstance(instance_id, config)  # type: ignore[arg-type]
