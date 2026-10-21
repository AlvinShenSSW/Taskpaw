"""A fake `mdcx.cmd.lookup_one` for the U1 lookup tests (stdlib only).

Behaviour comes from `<MDCX_CONFIG_DIR>/config.json` → `"fake_lookup"`:
`ok` (default), `no_poster`, `fail`, `no_number`, `no_result`, `hang`, `bad_schema`,
`temp_files` (succeeds but leaves MDCX's forced-exit `.poster.<pid>.*.jpg` /
`.thumb.<pid>.*.jpg` temps in out_dir). `hang` starts a grandchild that bumps
`"fake_heartbeat"` every 0.1 s, so a test can tell the whole tree was killed.
Every run appends what it saw (file name, config dir listing, cwd) to
`"fake_log"` from the same profile, so tests can check the isolation rules.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

_HEARTBEAT = """import sys, time
n = 0
while True:
    n += 1
    open(sys.argv[1], 'w').write(str(n))
    time.sleep(0.1)
"""


def main() -> int:
    args = sys.argv[1:]
    request = json.loads(Path(args[args.index("--request") + 1]).read_text("utf-8"))
    result_path = Path(args[args.index("--result") + 1])
    config = Path(os.environ["MDCX_CONFIG_DIR"])
    profile = json.loads((config / "config.json").read_text("utf-8"))
    mode = profile.get("fake_lookup", "ok")
    log = profile.get("fake_log")
    if log:
        with open(log, "a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "file_name": request["file_name"],
                        "config": sorted(
                            p.relative_to(config).as_posix()
                            for p in config.rglob("*")
                            if p.is_file()
                        ),
                        "config_dir": str(config),
                        "cwd": os.getcwd(),
                        "stdin_closed": sys.stdin is None or not sys.stdin.isatty(),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    # Miko may drop a mark and userdata into its config dir: harmless, discarded
    (config / "MDCx.config").write_text(str(config / "config.json"), "utf-8")
    if mode == "hang":
        beat = profile.get("fake_heartbeat")
        if beat:
            import subprocess

            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    _HEARTBEAT,
                    beat,
                ],
                stdin=subprocess.DEVNULL,
            )
        time.sleep(60)
        return 1
    if mode == "no_result":
        return 3
    number = Path(request["file_name"]).stem.upper()
    if mode == "bad_schema":
        result_path.write_text(json.dumps({"schema": 9}), "utf-8")
        return 0
    if mode in ("fail", "no_number"):
        message = "文件名中没有番号" if mode == "no_number" else "未查到资料"
        result_path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "cli_version": "lookup_one/1",
                    "status": "failed",
                    "number": "" if mode == "no_number" else number,
                    "title": None,
                    "actors": [],
                    "studio": None,
                    "release": None,
                    "runtime_min": None,
                    "poster": None,
                    "error_class": "needs_attention",
                    "message": message,
                },
                ensure_ascii=False,
            ),
            "utf-8",
        )
        return 1
    out = Path(request["out_dir"])
    poster = None
    if mode == "temp_files":
        pid = os.getpid()
        (out / f".poster.{pid}.a1.jpg").write_bytes(b"partial")
        (out / f".thumb.{pid}.b2.jpg").write_bytes(b"partial")
    if mode != "no_poster":
        (out / "poster.jpg").write_bytes(b"\xff\xd8fake-jpeg-" + number.encode())
        poster = "poster.jpg"
    result_path.write_text(
        json.dumps(
            {
                "schema": 1,
                "cli_version": "lookup_one/1",
                "status": "succeeded",
                "number": number,
                "title": f"{number} 的标题",
                "actors": profile.get("fake_actors", ["演员甲", "演员乙"]),
                "studio": "Studio",
                "release": "2026-01-02",
                "runtime_min": 120,
                "poster": poster,
                "error_class": None,
                "message": "",
            },
            ensure_ascii=False,
        ),
        "utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
