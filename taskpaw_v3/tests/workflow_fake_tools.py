"""Fake Jasna / WhisperJAV / ffprobe children for the T04 executor tests.

Standard library only: it runs as `python workflow_fake_tools.py <tool> ...`.
The fake "exe" the executor is configured with is a JSON file holding the
fake's behaviour (`{"mode": ...}`); every launch appends its argv to
`<exe>.runs`. A fake video is a JSON document of its media facts
(`width`, `height`, `duration` in seconds, `audio` streams), which the fake
ffprobe reports in ffprobe's JSON shape.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

LTX_PROGRESS = (
    "LTX 1/4 scan: 100%|██████████| 300/300 [00:03<00:00, 90.00frame/s, run 12:30 left]",
    "LTX 2/4 encode:  52.00%|█████▏    | 156/300 "
    "[00:27<00:25,  5.620frame/s, run 12:12 left]",
)
JA_CUES = ("こんにちは、元気ですか", "今日はいい天気ですね", "ありがとうございました")


def _config(exe: str) -> dict:
    try:
        return json.loads(Path(exe).read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}


def _record(exe: str, argv: list[str]) -> None:
    with open(exe + ".runs", "a", encoding="utf-8") as stream:
        stream.write(json.dumps(argv, ensure_ascii=False) + "\n")


def _option(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def jasna(argv: list[str]) -> int:
    exe = argv[0]
    cfg = _config(exe)
    _record(exe, argv)
    source, output = Path(_option(argv, "--input")), Path(_option(argv, "--output"))
    for line in cfg.get("progress", LTX_PROGRESS):
        sys.stdout.write(line + "\r")  # tqdm redraws with CR only
        sys.stdout.flush()
    sys.stdout.write("\n")
    sys.stdout.flush()
    time.sleep(cfg.get("linger", 0))  # quiet but alive: the console flush
    mode = cfg.get("mode", "copy")
    if mode == "sleep":
        print("started", flush=True)
        if not cfg.get("release"):
            time.sleep(60)
            return 0
        # P2-3 §11 IR13: held until the test creates the release file, then
        # the restore completes normally (an event, never a wall-clock wait)
        release, deadline = Path(cfg["release"]), time.monotonic() + 120
        while not release.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
    if mode == "fail":
        print("RuntimeError: CUDA out of memory", flush=True)
        return 3
    if mode == "no_output":
        return 0
    shutil.copyfile(source, output)
    if "duration" in cfg:
        facts = json.loads(output.read_text(encoding="utf-8"))
        facts["duration"] = cfg["duration"]
        output.write_text(json.dumps(facts), encoding="utf-8")
    return 0


def bitrate_probe(argv: list[str]) -> int:
    """`jasna.bitrate_probe_args` (the video is the last argument): the fake
    video's `bit_rate`, none without one; `bitrate_hang` never answers (J2)."""
    try:
        facts = json.loads(Path(argv[-1]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        sys.stderr.write(f"{argv[-1]}: Invalid data found when processing input\n")
        return 1
    if facts.get("bitrate_hang"):
        time.sleep(120)
    stream = {"bit_rate": str(facts["bit_rate"])} if "bit_rate" in facts else {}
    print(json.dumps({"streams": [stream], "format": {}}, indent=4))
    return 0


def ffprobe(argv: list[str]) -> int:
    if "-select_streams" in argv:
        return bitrate_probe(argv)
    path = Path(_option(argv, "-i"))
    try:
        facts = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        sys.stderr.write(f"{path}: Invalid data found when processing input\n")
        return 1
    duration = str(facts["duration"])
    streams = [
        {
            "index": 0,
            "codec_type": "video",
            "width": facts["width"],
            "height": facts["height"],
            "duration": duration,
            "disposition": {"attached_pic": 0},
        }
    ]
    for n in range(facts.get("audio", 1)):
        streams.append(
            {"index": n + 1, "codec_type": "audio", "disposition": {"attached_pic": 0}}
        )
    print(json.dumps({"streams": streams, "format": {"duration": duration}}, indent=4))
    return 0


def whisperjav(argv: list[str]) -> int:
    exe = argv[0]
    cfg = _config(exe)
    _record(exe, argv)
    source, out_dir = Path(argv[1]), Path(_option(argv, "--output-dir"))
    print("Transcribing\r 50%\r100%", flush=True)
    mode = cfg.get("mode", "done")
    if mode == "sleep":
        print("started", flush=True)
        time.sleep(60)
        return 0
    if mode == "crash":
        print("Traceback: model load failed", flush=True)
        return 1
    if mode == "empty":
        entry = {"state": "empty", "detail": "no speech detected"}
    else:
        name = f"{source.stem}.srt"
        cues = () if mode == "no_cues" else JA_CUES
        text = "".join(
            f"{i}\n00:00:0{i},000 --> 00:00:0{i},900\n{cue}\n\n"
            for i, cue in enumerate(cues, start=1)
        )
        (out_dir / name).write_text(text, encoding="utf-8")
        entry = {"state": "done", "output": name}
    (out_dir / "whisperjav_run.json").write_text(
        json.dumps({"files": [entry]}), encoding="utf-8"
    )
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    tool, argv = sys.argv[1], sys.argv[2:]
    return {"jasna": jasna, "ffprobe": ffprobe, "whisperjav": whisperjav}[tool](argv)


if __name__ == "__main__":
    sys.exit(main())
