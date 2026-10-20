"""A fake `mdcx.cmd.postprocess_one` for the M01 scrape tests (stdlib only).

Behaviour comes from `<MDCX_CONFIG_DIR>/user-config.json` → `"fake_mode"`:
`ok` (default), `fail`, `no_result`, `extra_file`, `wrong_hash`.
It lays out Miko-like output: out/<Actor>/<NUMBER>/<NUMBER>[-cdN].<ext>,
<NUMBER>.nfo, poster/thumb/fanart, folder.jpg for several volumes, and
the Chinese subtitle renamed to .chs.srt.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    args = sys.argv[1:]
    request = json.loads(Path(args[args.index("--request") + 1]).read_text("utf-8"))
    result_path = Path(args[args.index("--result") + 1])
    config = Path(os.environ["MDCX_CONFIG_DIR"])
    profile = json.loads((config / "user-config.json").read_text("utf-8"))
    mode = profile.get("fake_mode", "ok")
    definition = profile.get("fake_definition", "1080P")
    if mode == "no_result":
        return 3
    if mode == "fail":
        result_path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "status": "failed",
                    "error_class": "transient",
                    "message": "站点暂时不可用",
                    "files": [],
                }
            ),
            encoding="utf-8",
        )
        return 2
    staging_in = Path(request["staging"]["in"])
    film = Path(request["staging"]["out"]) / "Actor" / "ABC-123"
    film.mkdir(parents=True)
    files = []

    def add(path: Path, role: str, video: str | None = None) -> None:
        files.append(
            {
                "path": path.relative_to(film.parent.parent).as_posix(),
                "role": role,
                "video": video,
                "sha256": _sha(path),
                "size": path.stat().st_size,
            }
        )

    multi = len(request["videos"]) > 1
    for video in request["videos"]:
        source = staging_in / video["file"]
        suffix = f"-cd{video['volume']}" if multi else ""
        target = film / f"ABC-123{suffix}{source.suffix}"
        shutil.move(source, target)
        add(target, "main_video", video["file"])
        sub = staging_in / f"{Path(video['file']).stem}.srt"
        if video["zh_subtitle"] and sub.exists():
            dest = film / f"ABC-123{suffix}.chs.srt"
            shutil.move(sub, dest)
            add(dest, "subtitle", video["file"])
    tags = (
        "中文字幕 无码破解"
        if any(v["zh_subtitle"] for v in request["videos"])
        else "无码破解"
    )
    (film / "ABC-123.nfo").write_text(f"<movie><tag>{tags}</tag></movie>", "utf-8")
    add(film / "ABC-123.nfo", "nfo")
    for name, role in (
        ("poster.jpg", "poster"),
        ("thumb.jpg", "thumb"),
        ("fanart.jpg", "fanart"),
    ):
        (film / name).write_bytes(f"image {name}".encode())
        add(film / name, role)
    if multi:
        shutil.copyfile(film / "poster.jpg", film / "folder.jpg")
        add(film / "folder.jpg", "folder_cover")
    if mode == "extra_file":
        (film / "trailer.mp4").write_bytes(b"unexpected")
    if mode == "wrong_hash":
        files[0]["sha256"] = "0" * 64
    result_path.write_text(
        json.dumps(
            {
                "schema": 1,
                "status": "succeeded",
                "error_class": None,
                "message": "",
                "number": "ABC-123",
                "actors": [{"name": "Actor", "source": "fake"}],
                "film_dir": "Actor/ABC-123",
                "files": files,
                "expected_marks": [
                    {
                        "video": v["file"],
                        "has_sub": v["zh_subtitle"],
                        "mosaic": "无码破解",
                        "definition": definition,
                    }
                    for v in request["videos"]
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
