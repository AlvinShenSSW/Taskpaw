"""Bounded snapshots of Jasna's LTX console bars, separate from film progress.

U1c: the LTX auto split first runs a whole-video ``Mosaic scan`` tqdm bar
(jasna/mosaic_scan.py) and logs one ``LTX auto split ...`` line; both are
pre-LTX phase facts (``scan_progress``, ``split_note``), never film progress.
"""

from __future__ import annotations

import math
import re

from taskpaw_v3.monitors.plugins.lada import parse_progress_line

_RAW_LIMIT = 4096
_HEADER = re.compile(
    r"^LTX(?:\s+(speed test))?\s+([1-9]\d{0,5})/([1-9]\d{0,5})"
    r"\s+([^:：\r\n]{1,128}?)\s*[:：]\s*"
)
_NUMBER = r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_PERCENT = re.compile(rf"^({_NUMBER})\s*%")
_COUNTERS = re.compile(rf"(?:^|\s)({_NUMBER})/({_NUMBER})\s*\[")
_RATE = re.compile(rf"^({_NUMBER})\s*(frame/s|s/frame|s/step|step/s|it/s|s/it)$")
_TIME = re.compile(r"(?:\d{1,8}:)?[0-5]?\d:[0-5]\d$")
_OSC = re.compile(r"(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c|$)")
_STRING_CONTROL = re.compile(r"(?:\x1b[PX^_]|[\x90\x98\x9e\x9f]).*?(?:\x1b\\|\x9c|$)")
_CSI = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*(?:[@-~]|$)")
_ESCAPE = re.compile(r"\x1b[@-_]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SCAN = re.compile(r"^Mosaic scan\s*[:：]\s*")
_SCAN_RATE = re.compile(rf"^({_NUMBER})\s*(sample/s|s/sample)$")
_SPLIT = re.compile(r"LTX auto split(:| skipped| found no mosaics)")
_SPLIT_KIND = {":": "planned", " skipped": "skipped", " found no mosaics": "no_mosaics"}
_SEGMENTS = re.compile(r"[\r\n]+")
_CONSOLE_LIMIT = 300
_ORDINARY = re.compile(r"^(?:Processing video|正在处理视频)\s*[:：]", re.IGNORECASE)
_STAGE_KEYS = frozenset(
    (
        "ltx_progress",
        "ltx_stage",
        "percent",
        "elapsed",
        "processed_frames",
        "remaining_frames",
        "fps",
        "eta",
    )
)
_PHASE_KEYS = frozenset(("scan_progress",))  # an LTX/BasicVSR++ start ends the scan


def _plain_text(line: str) -> str:
    # Remove control-string payloads as well as their delimiters (e.g. OSC titles).
    text = _STRING_CONTROL.sub("", _OSC.sub("", line))
    text = _ESCAPE.sub("", _CSI.sub("", text))
    return _CONTROL.sub("", text.replace("\t", " ")).strip()


def _finite(token: str) -> bool:
    return len(token) <= 64 and math.isfinite(float(token))


def _snapshot(text: str) -> dict | None:
    header = _HEADER.match(text)
    if header is None:
        return None
    index, count = int(header[2]), int(header[3])
    if index > count or not (name := header[4].strip()):
        return None
    # tqdm may share a CR-delimited line with a later warning. Only its own
    # bracket-terminated record belongs in the display/copy payload.
    close = text.find("]", header.end())
    raw = text[: close + 1] if close >= 0 else text
    raw = raw[:_RAW_LIMIT]
    body = raw[header.end() :]
    out: dict = {
        "mode": "speed_test" if header[1] else "restore",
        "stage_index": index,
        "stage_total": count,
        "stage_name": name,
        "raw_line": raw,
    }
    _bar(body, _RATE, out)
    return out


def _bar(body: str, rate_re: re.Pattern[str], out: dict) -> None:
    """The tqdm fields after a bar's description: percent, n/total, times, rate."""
    if pct := _PERCENT.match(body):
        if _finite(pct[1]) and 0 <= float(pct[1]) <= 100:
            out["percent"] = float(pct[1])
            out["percent_text"] = pct[1]
    if counters := _COUNTERS.search(body):
        if _finite(counters[1]) and _finite(counters[2]):
            out["current"], out["total"] = counters[1], counters[2]
    left = body.find("[")
    right = body.find("]", left + 1)
    if left >= 0 and right >= 0:
        fields = [field.strip() for field in body[left + 1 : right].split(",")]
        times = fields[0].split("<")
        if len(times) == 2:
            for key, value in zip(("elapsed", "eta"), times):
                if _TIME.fullmatch(value.strip()):
                    out[key] = value.strip()
        for field in fields[1:]:
            if rate := rate_re.fullmatch(field):
                if _finite(rate[1]):
                    out["rate_value"], out["rate_unit"] = rate[1], rate[2]
            elif field.startswith("run ") and field.endswith(" left"):
                value = field[4:-5].strip()
                if _TIME.fullmatch(value):
                    out["run_eta"] = value


def _scan_snapshot(text: str) -> dict | None:
    """The auto split's ``Mosaic scan`` bar: only its own bracketed record."""
    header = _SCAN.match(text)
    if header is None:
        return None
    close = text.find("]", header.end())
    raw = (text[: close + 1] if close >= 0 else text)[:_RAW_LIMIT]
    out: dict = {}
    _bar(raw[header.end() :], _SCAN_RATE, out)
    out.pop("run_eta", None)  # an LTX-only field
    out["raw_line"] = raw
    return out


def console_text(line: str) -> str:
    """The latest non-empty plain segment of one console write, bounded."""
    for part in reversed(_SEGMENTS.split(line)):
        if text := _plain_text(part):
            return text[:_CONSOLE_LIMIT]
    return ""


def parse_jasna_progress_line(line: str, prev: dict) -> dict:
    """Return one fresh LTX observation or the existing ordinary progress update.

    Missing stage fields never inherit earlier values. Legacy fps admits only
    frame/s; legacy eta is this LTX run's estimate, never a whole-film estimate.
    Unrecognized output returns ``prev`` itself for the crash-tail collector.
    A CR/LF-joined write is read segment by segment, so its latest bar wins.
    """
    state = prev
    for part in _SEGMENTS.split(line):
        state = _parse_segment(part, state)
    return state


def _parse_segment(line: str, prev: dict) -> dict:
    text = _plain_text(line)
    if not text or text.startswith("Jasna: "):
        return prev
    if (scan := _scan_snapshot(text)) is not None:
        # a new film's scan: nothing of an earlier stage or split survives
        drop = _STAGE_KEYS | _PHASE_KEYS | {"split_note"}
        out = {key: value for key, value in prev.items() if key not in drop}
        out["scan_progress"] = scan
        return out
    if split := _SPLIT.search(text):
        out = {key: value for key, value in prev.items() if key not in _PHASE_KEYS}
        out["split_note"] = {
            "kind": _SPLIT_KIND[split[1]],
            "text": text[:_CONSOLE_LIMIT],
        }
        return out
    if snapshot := _snapshot(text):
        drop = _STAGE_KEYS | _PHASE_KEYS
        out = {key: value for key, value in prev.items() if key not in drop}
        out["ltx_progress"] = snapshot
        out["ltx_stage"] = (
            f"{snapshot['stage_index']}/{snapshot['stage_total']} {snapshot['stage_name']}"
        )
        if "percent" in snapshot:
            percent = snapshot["percent"]
            out["percent"] = int(percent) if percent.is_integer() else percent
        if snapshot.get("rate_unit") == "frame/s":
            out["fps"] = float(snapshot["rate_value"])
        if "run_eta" in snapshot:
            out["eta"] = snapshot["run_eta"]
        return out
    if text.startswith("LTX"):
        return prev  # plans/invalid headers are not ordinary percentage lines
    if "ltx_stage" in prev or "ltx_progress" in prev or "scan_progress" in prev:
        # Accept the source-verified BasicVSR++ prefix or a filename header;
        # an unrelated warning with a percentage cannot end the LTX snapshot
        # or the mosaic scan.
        probe = parse_progress_line(text, {})
        if not _ORDINARY.match(text) and set(probe) != {"current_file"}:
            return prev
        drop = _STAGE_KEYS | _PHASE_KEYS
        clean = {key: value for key, value in prev.items() if key not in drop}
        out = parse_progress_line(text, clean)
        return out if out is not clean else prev
    return parse_progress_line(text, prev)
