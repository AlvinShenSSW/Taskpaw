"""Bounded snapshots of Jasna's LTX console bars, separate from film progress."""

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
            if rate := _RATE.fullmatch(field):
                if _finite(rate[1]):
                    out["rate_value"], out["rate_unit"] = rate[1], rate[2]
            elif field.startswith("run ") and field.endswith(" left"):
                value = field[4:-5].strip()
                if _TIME.fullmatch(value):
                    out["run_eta"] = value
    return out


def parse_jasna_progress_line(line: str, prev: dict) -> dict:
    """Return one fresh LTX observation or the existing ordinary progress update.

    Missing stage fields never inherit earlier values. Legacy fps admits only
    frame/s; legacy eta is this LTX run's estimate, never a whole-film estimate.
    Unrecognized output returns ``prev`` itself for the crash-tail collector.
    """
    text = _plain_text(line)
    if not text or text.startswith("Jasna: "):
        return prev
    if snapshot := _snapshot(text):
        out = {key: value for key, value in prev.items() if key not in _STAGE_KEYS}
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
    if "ltx_stage" in prev or "ltx_progress" in prev:
        # Accept the source-verified BasicVSR++ prefix or a filename header;
        # an unrelated warning with a percentage cannot end the LTX snapshot.
        probe = parse_progress_line(text, {})
        if not _ORDINARY.match(text) and set(probe) != {"current_file"}:
            return prev
        clean = {key: value for key, value in prev.items() if key not in _STAGE_KEYS}
        out = parse_progress_line(text, clean)
        return out if out is not clean else prev
    return parse_progress_line(text, prev)
