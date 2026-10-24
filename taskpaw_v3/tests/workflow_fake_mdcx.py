"""A fake `mdcx.cmd.postprocess_one` for the M01 scrape tests (stdlib only).

Behaviour comes from `<MDCX_CONFIG_DIR>/user-config.json` → `"fake_mode"`:
`ok` (default), `fail`, `no_result`, `extra_file`, `wrong_hash`, `wrong_mosaic`.
It follows MDCX304 #299 (postprocess_one): out/<Actor>/<NUMBER>/, and per volume
`<name>.<ext>`, `<name>.nfo`, `<name>-poster.jpg`, `<name>-fanart.jpg`,
`<name>-thumb.jpg` and the Chinese subtitle as `<name>.zh.srt`, where `<name>`
is `<NUMBER>[-无码|-破解][-C][-cdN]`; a film of several volumes also gets a
`folder.jpg` copy of the first volume's poster. A request video with `ltx` is
无码 (marker `-无码`, mosaic `无码`); a restored one without it is 破解 (mosaic
`无码破解`). A `.ja.srt` is never staged by Taskpaw, so the fake never sees one.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

# SOI + SOF0 of an 800×538 image: enough for the cover's size check (U1b)
_JACKET_HEAD = b"\xff\xd8\xff\xc0\x00\x11\x08\x02\x1a\x03\x20\x03" + bytes(9)


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
    mosaics = {}
    first_poster = None
    for video in request["videos"]:
        source = staging_in / video["file"]
        mosaics[video["file"]] = (
            "无码" if video.get("ltx") else "无码破解" if video["restored"] else ""
        )
        marker = "-无码" if video.get("ltx") else "-破解" if video["restored"] else ""
        zh = "-C" if video["zh_subtitle"] else ""
        suffix = f"-cd{video['volume']}" if multi else ""
        name = f"ABC-123{marker}{zh}{suffix}"
        target = film / f"{name}{source.suffix}"
        shutil.move(source, target)
        add(target, "main_video", video["file"])
        sub = staging_in / f"{Path(video['file']).stem}.srt"
        if video["zh_subtitle"] and sub.exists():
            dest = film / f"{name}.zh.srt"
            shutil.move(sub, dest)
            add(dest, "subtitle", video["file"])
        nfo = film / f"{name}.nfo"
        tag = " ".join(
            t for t in (("中文字幕" if zh else ""), mosaics[video["file"]]) if t
        )
        nfo.write_text(
            "<movie><title>假的标题</title><actor><name>Actor</name></actor>"
            f"<tag>{tag}</tag></movie>",
            "utf-8",
        )
        add(nfo, "nfo", video["file"])
        for role in ("poster", "thumb", "fanart"):
            image = film / f"{name}-{role}.jpg"
            # the fanart is the whole landscape jacket (a JPEG frame header, U1b)
            head = _JACKET_HEAD if role == "fanart" else b""
            image.write_bytes(head + f"image {name}-{role}".encode())
            add(image, role, video["file"])
            if role == "poster" and first_poster is None:
                first_poster = image
    if multi:
        shutil.copyfile(first_poster, film / "folder.jpg")
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
                        # an old Miko that ignores `ltx` still says 破解
                        "mosaic": "无码破解"
                        if mode == "wrong_mosaic"
                        else mosaics[v["file"]],
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
