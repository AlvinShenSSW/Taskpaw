"""#191: recognising a film's existing subtitles from ONE folder listing
(`subs/existing.py`) — the owner's rules (a)/(b)/(c), attribution, Japanese
tags, and the two listing helpers (never-raising / Jasna's planning listing).

Names only: `judge` never touches the filesystem, and the listing tests use a
tmp dir or a monkeypatched `os.scandir` — never a real network path.
"""

from __future__ import annotations

import os
import sys
import unicodedata
from pathlib import Path

import pytest

from taskpaw_v3.monitors import subs
from taskpaw_v3.monitors.subs import existing as E
from taskpaw_v3.monitors.subs.existing import (
    Existing,
    judge,
    list_names,
    list_names_missing_ok,
    skip_reason,
)
from taskpaw_v3.monitors.subs.job import SUBTITLE_EXISTS, SUBTITLE_UNREADABLE


def _vid(*exts: str):
    """A caller's video filter: extension in `exts`, no `.tmp.`, no `._`."""

    def is_video(name: str) -> bool:
        return (
            os.path.splitext(name)[1][1:].casefold() in exts
            and ".tmp." not in name.casefold()
            and not name.startswith("._")
        )

    return is_video


MP4 = _vid("mp4")
JASNA = _vid("mp4", "mkv", "avi", "mov", "wmv", "flv", "webm")


def _zh(video: str, names, *, rule_c: bool = True, is_video=MP4, extra=()):
    return judge(video, list(names), is_video, rule_c=rule_c, extra_videos=extra)


# ── the evidence table (read-only audit of the owner's library, names only) ──
@pytest.mark.parametrize(
    "video, names, expected",
    [
        # LMNO-047 before the bug: tokens swapped + `.zh` → rule (c)
        (
            "LMNO-047-破解-C-4K.mp4",
            ["LMNO-047-破解-C-4K.mp4", "LMNO-047-破解-4K-C.zh.srt"],
            "LMNO-047-破解-4K-C.zh.srt",
        ),
        # LMNO-047 now: the duplicate exact `<stem>.srt` → rule (a) wins
        (
            "LMNO-047-破解-C-4K.mp4",
            [
                "LMNO-047-破解-C-4K.mp4",
                "LMNO-047-破解-4K-C.zh.srt",
                "LMNO-047-破解-C-4K.srt",
            ],
            "LMNO-047-破解-C-4K.srt",
        ),
        (
            "LMNO-079-破解-C-4K.mp4",
            ["LMNO-079-破解-C-4K.mp4", "LMNO-079-破解-C-4K-C.zh.srt"],
            "LMNO-079-破解-C-4K-C.zh.srt",
        ),
        ("PQRS-218.mp4", ["PQRS-218.mp4", "PQRS-218.chs.srt"], "PQRS-218.chs.srt"),
        (
            "PQRS-564-破解.mp4",
            ["PQRS-564-破解.mp4", "PQRS-564-破解.chs.srt"],
            "PQRS-564-破解.chs.srt",
        ),
        (
            "PQRS-860-破解-C.mp4",
            ["PQRS-860-破解-C.mp4", "PQRS-860-破解-C.chs.srt"],
            "PQRS-860-破解-C.chs.srt",
        ),
        (
            "PQRS-948-破解-C.mp4",
            [
                "PQRS-948-破解-C.mp4",
                "dl.example.com@pqrs00948.srt",
                "PQRS-948-破解-C.chs.srt",
            ],
            "PQRS-948-破解-C.chs.srt",  # (b) wins over (c)
        ),
        (
            "PQRS-948-破解-C.mp4",
            ["PQRS-948-破解-C.mp4", "dl.example.com@pqrs00948.srt"],
            "dl.example.com@pqrs00948.srt",  # (c): any name, one video
        ),
        (
            "LMNO-005-破解-C-4K.mp4",
            ["LMNO-005-破解-C-4K.mp4", "LMNO-005-破解-C-4K.srt"],
            "LMNO-005-破解-C-4K.srt",
        ),
    ],
)
def test_evidence_table_rows_are_recognised(video, names, expected):
    assert _zh(video, names).chinese == expected


@pytest.mark.parametrize("n_parts, n_subbed", [(4, 3), (5, 5), (2, 1)])
def test_cd_parts_each_count_only_their_own_subtitle(n_parts, n_subbed):
    # DEFG-594 cd1–cd4, HIJK-* cd1–cd5: correct today and must stay correct.
    names = [f"DEFG-594-cd{i}.mp4" for i in range(1, n_parts + 1)]
    names += [f"DEFG-594-cd{i}.srt" for i in range(1, n_subbed + 1)]
    for i in range(1, n_parts + 1):
        got = _zh(f"DEFG-594-cd{i}.mp4", names).chinese
        assert got == (f"DEFG-594-cd{i}.srt" if i <= n_subbed else None), i


def test_cd1_never_owns_cd10_and_back():
    names = ["X-cd1.mp4", "X-cd10.mp4", "X-cd1.srt"]
    assert _zh("X-cd10.mp4", names).chinese is None
    assert _zh("X-cd1.mp4", names).chinese == "X-cd1.srt"
    dotted = ["X.cd1.mp4", "X.cd10.mp4", "X.cd10.chs.srt"]
    assert _zh("X.cd1.mp4", dotted).chinese is None  # no dot boundary
    assert _zh("X.cd10.mp4", dotted).chinese == "X.cd10.chs.srt"


# ── attribution (F2 / F17) ────────────────────────────────────────────────
def test_a_part2_subtitle_belongs_to_the_part2_video_only():
    names = ["Movie.mp4", "Movie.part2.mp4", "Movie.part2.srt"]
    assert _zh("Movie.mp4", names).chinese is None
    assert _zh("Movie.part2.mp4", names).chinese == "Movie.part2.srt"


def test_the_longest_stem_owns_a_dotted_subtitle():
    names = ["ABC-123.mp4", "ABC-123.C.mp4", "ABC-123.C.srt"]
    assert _zh("ABC-123.mp4", names).chinese is None  # not a `.C` tag of ABC-123
    assert _zh("ABC-123.C.mp4", names).chinese == "ABC-123.C.srt"
    names = ["ABC-123.mp4", "ABC-123.C.mp4", "ABC-123.srt"]
    assert _zh("ABC-123.mp4", names).chinese == "ABC-123.srt"
    assert _zh("ABC-123.C.mp4", names).chinese is None


def test_a_sibling_of_another_extension_still_owns_its_subtitle():
    # F17: a `.mkv` in an mp4-only task is a video for attribution.
    names = ["Movie.mp4", "Movie.part2.mkv", "Movie.part2.srt"]
    assert _zh("Movie.mp4", names, rule_c=False).chinese is None
    assert _zh("Movie.mp4", names, rule_c=True).chinese is None  # not (c) either
    # ... while a subtitle nobody else owns still counts under (c)
    names.append("Movie-C.zh.srt")
    assert _zh("Movie.mp4", names, rule_c=True).chinese == "Movie-C.zh.srt"


def test_rule_c_skips_a_subtitle_owned_by_a_video_of_another_extension():
    names = ["LMNO-047-破解-C-4K.mp4", "LMNO-047-C.mkv", "LMNO-047-C.chs.srt"]
    assert _zh("LMNO-047-破解-C-4K.mp4", names).chinese is None


def test_extra_videos_own_their_subtitles_before_they_exist():
    # F1 (Jasna, rule c off): another film's pre-placed subtitle is never
    # credited to this film, and its own not-yet-restored media owns it.
    names = ["A-破解.mp4", "C-破解.chs.srt", ".avsubs"]
    extra = ("C-破解.mp4",)
    assert _zh("A-破解.mp4", names, rule_c=False, is_video=JASNA, extra=extra) == (
        Existing(None, None)
    )
    got = _zh("C-破解.mp4", names, rule_c=False, is_video=JASNA, extra=extra)
    assert got.chinese == "C-破解.chs.srt"
    # attribution: the future Movie.part2 owns its subtitle, not Movie
    names = ["Movie.mp4", "Movie.part2.srt"]
    assert _zh("Movie.mp4", names, rule_c=False).chinese == "Movie.part2.srt"
    assert (
        _zh("Movie.mp4", names, rule_c=False, extra=("Movie.part2.mp4",)).chinese
        is None
    )


def test_a_staging_file_is_a_video_for_attribution_only():
    names = ["A-破解.mp4", "B-破解.tmp.mp4", "A-破解.chs.srt"]
    assert _zh("A-破解.mp4", names, rule_c=False, is_video=JASNA).chinese == (
        "A-破解.chs.srt"
    )


# ── rule (c) ──────────────────────────────────────────────────────────────
def test_rule_c_needs_a_single_video_folder_and_the_caller_opt_in():
    one = ["A.mp4", "whatever.srt"]
    assert _zh("A.mp4", one).chinese == "whatever.srt"
    assert _zh("A.mp4", one, rule_c=False).chinese is None
    two = ["A.mp4", "B.mp4", "whatever.srt"]
    assert _zh("A.mp4", two).chinese is None
    # the caller's filter decides what "a video" is: a staging or ._ file
    # does not make the folder a two-video one
    assert _zh("A.mp4", ["A.mp4", "A.tmp.mp4", "._B.mp4", "x.srt"]).chinese == "x.srt"


def test_rule_c_needs_the_video_in_the_listing():
    assert _zh("A.mp4", ["B.mp4", "whatever.srt"]).chinese is None
    assert _zh("A.mp4", ["whatever.srt"]).chinese is None
    # (a)/(b) do not: the listing is only asked about the subtitle
    assert _zh("A.mp4", ["A.srt"]).chinese == "A.srt"
    assert _zh("A.mp4", ["A.zh.srt"], rule_c=False).chinese == "A.zh.srt"


def test_rule_c_residual_x_jp_is_recognised():
    # F19 (accepted residual, pinned): the only Japanese marker equals a stem
    # token, which (c) subtracts.
    assert _zh("X-JP.mp4", ["X-JP.mp4", "X.jp.srt"]).chinese == "X.jp.srt"


@pytest.mark.parametrize("video", ["JA-001.mp4", "X-JA.mp4", "Kissa_ja_koira.mp4"])
def test_rule_c_never_rejudges_the_films_own_japanese_transcript(video):
    # IR1: a subtitle attributed to the video is judged by (a)/(b) only; a
    # `ja` token inside the video's own name must not let (c) count its own
    # `.ja.srt` as Chinese (the film would be skipped and re-transcribed forever).
    stem = video[: -len(".mp4")]
    got = _zh(video, [video, f"{stem}.ja.srt"])
    assert got.chinese is None and got.ja_transcript == f"{stem}.ja.srt"
    # …while a Chinese subtitle attributed to it still counts, via (b)
    assert _zh(video, [video, f"{stem}.chs.srt"]).chinese == f"{stem}.chs.srt"


# ── Japanese tags (F3 / F12) ──────────────────────────────────────────────
@pytest.mark.parametrize(
    "sub",
    [
        "x.ja.srt",
        "x.JP.ass",
        "x.ja-JP.srt",
        "x.ja_jp.srt",
        "x.jap.srt",
        "x.jpn.zh.srt",
        "x.japanese.vtt",
        "x.日语.srt",
        "x.日文.ssa",
        "x.日本語.srt",
    ],
)
def test_a_japanese_tag_never_counts_as_chinese(sub):
    # rule (c) on and one video: neither (b) nor (c) takes it
    assert _zh("x.mp4", ["x.mp4", sub]).chinese is None


def test_the_ja_transcript_is_reported_separately():
    got = _zh("x.mp4", ["x.mp4", "x.ja.srt"])
    assert got == Existing(None, "x.ja.srt")
    assert _zh("x.mp4", ["x.mp4", "X.JA.SRT"]).ja_transcript == "X.JA.SRT"
    assert _zh("x.mp4", ["x.mp4", "x.ja.ass"]).ja_transcript is None
    both = _zh("x.mp4", ["x.mp4", "x.ja.srt", "x.chs.srt"])
    assert both == Existing("x.chs.srt", "x.ja.srt")


def test_a_japanese_looking_token_inside_the_video_name_never_disqualifies():
    # F12: (a)/(b) tags are only what follows the attributed stem.
    assert _zh("ABC-123-JP.mp4", ["ABC-123-JP.mp4", "ABC-123-JP.srt"]).chinese == (
        "ABC-123-JP.srt"
    )
    assert (
        _zh("ABC-123-JP.mp4", ["ABC-123-JP.mp4", "ABC-123-JP.chs.srt"]).chinese
        == "ABC-123-JP.chs.srt"
    )
    only_ja = _zh("ABC-123-JP.mp4", ["ABC-123-JP.mp4", "ABC-123-JP.ja.srt"])
    assert only_ja == Existing(None, "ABC-123-JP.ja.srt")
    assert (
        _zh(
            "Japanese_Wife_01.mp4", ["Japanese_Wife_01.mp4", "Japanese_Wife_01.srt"]
        ).chinese
        == "Japanese_Wife_01.srt"
    )
    names = ["JAP-001-破解.mp4", "Y-破解.mp4", "JAP-001-破解.srt"]
    got = _zh("JAP-001-破解.mp4", names, rule_c=False, is_video=JASNA)
    assert got.chinese == "JAP-001-破解.srt"


# ── names ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "sub", ["x.srt", "x.ass", "x.ssa", "x.vtt", "x.chs.ass", "x.zh.ssa", "x.cht.vtt"]
)
def test_every_subtitle_extension_counts(sub):
    assert _zh("x.mp4", ["x.mp4", "y.mp4", sub]).chinese == sub


def test_appledouble_and_temp_names_are_never_subtitles():
    assert _zh("A.mp4", ["A.mp4", "._A.srt"]).chinese is None
    assert _zh("A.mp4", ["A.mp4", "B.mp4", "A.srt.3.tmp"]).chinese is None
    assert _zh("A.mp4", ["A.mp4", "A.ja.srt.3.tmp"]).ja_transcript is None
    assert _zh("A.mp4", ["A.mp4", "A.sub", "A.idx", "A.sup"]).chinese is None


def test_nfc_nfd_and_case_insensitive_matching():
    nfc = unicodedata.normalize("NFC", "ガ字幕")
    nfd = unicodedata.normalize("NFD", "ガ字幕")
    assert nfc != nfd
    assert _zh(f"{nfc}.mp4", [f"{nfc}.mp4", "o.mp4", f"{nfd}.srt"]).chinese == (
        f"{nfd}.srt"
    )
    assert _zh(f"{nfd}.mp4", [f"{nfd}.mp4", "o.mp4", f"{nfc}.chs.srt"]).chinese
    assert _zh("ABC.mp4", ["ABC.mp4", "o.mp4", "abc.CHS.SRT"]).chinese == (
        "abc.CHS.SRT"
    )
    # (c): the one video is found whatever its case/normal form in the call
    assert _zh("abc.MP4", ["ABC.mp4", "x.srt"]).chinese == "x.srt"


def test_garbage_input_never_raises():
    names = [None, 3, "", ".", "..srt", ".srt", "._", b"A.srt", ".mp4"]
    assert _zh("A.mp4", names) == Existing(None, None)

    def boom(name):
        raise RuntimeError("bad filter")

    assert judge("A.mp4", ["A.mp4", "x.srt"], boom, rule_c=True) == Existing()
    assert judge(None, ["A.srt"], MP4, rule_c=True) == Existing()  # type: ignore[arg-type]
    assert judge("A.mp4", None, MP4, rule_c=True) == Existing()  # type: ignore[arg-type]
    assert judge("A.mp4", ["A.srt"], MP4, rule_c=False, extra_videos=[None, 5]) == (
        Existing("A.srt", None)
    )


# ── skip_reason (the AC5 re-check) ────────────────────────────────────────
def test_skip_reason_maps_listing_and_judgement():
    assert skip_reason(None, "A.mp4", MP4, rule_c=True) == SUBTITLE_UNREADABLE
    assert skip_reason(["A.mp4", "A.chs.srt"], "A.mp4", MP4, rule_c=True) == (
        SUBTITLE_EXISTS
    )
    assert skip_reason(["A.mp4", "A.ja.srt"], "A.mp4", MP4, rule_c=True) is None
    assert skip_reason([], "A.mp4", MP4, rule_c=False) is None
    assert SUBTITLE_EXISTS == "subtitle exists"
    assert SUBTITLE_UNREADABLE == "subtitle state unreadable"


def test_the_package_exports_the_recognition_api():
    assert subs.judge is judge and subs.Existing is Existing
    assert subs.list_names is list_names
    assert {"judge", "Existing", "list_names", "skip_reason"} <= set(subs.__all__)
    assert {".mkv", ".mp4", ".webm"} <= E.VIDEO_EXTENSIONS


# ── list_names: never raises (F14) ────────────────────────────────────────
def test_list_names_lists_one_folder(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"v")
    (tmp_path / "sub").mkdir()
    assert sorted(list_names(tmp_path) or []) == ["a.mp4", "sub"]
    assert list_names(str(tmp_path / "missing")) is None


@pytest.mark.parametrize(
    "exc", [PermissionError("denied"), OSError(53, "net"), RuntimeError("bug")]
)
def test_list_names_reads_any_failure_as_unreadable(tmp_path, monkeypatch, exc):
    def scandir(path="."):
        raise exc

    monkeypatch.setattr(os, "scandir", scandir)
    assert list_names(tmp_path) is None


def test_list_names_survives_an_invalid_path():
    assert list_names("bad" + chr(0) + "path") is None


# ── list_names_missing_ok: Jasna's planning listing (AC3, F13/F18) ─────────
def _fnf(winerror: int) -> OSError:
    """What CPython raises for a Windows error mapped to ENOENT (WinError 2/3
    missing; 53/67 unreachable server/share) — a `FileNotFoundError`."""
    e = FileNotFoundError(2, f"WinError {winerror}")
    e.winerror = winerror  # type: ignore[attr-defined]
    return e


def _scripted(monkeypatch, script: dict):
    """`os.scandir` fake: path (as given, `os.fspath`) → names or exception."""
    calls: list[str] = []

    class _Entry:
        def __init__(self, name: str) -> None:
            self.name = name

    class _It:
        def __init__(self, names) -> None:
            self._names = names

        def __enter__(self):
            return iter([_Entry(n) for n in self._names])

        def __exit__(self, *a) -> None:
            return None

    def scandir(path="."):
        key = os.fspath(path)
        calls.append(key)
        got = script[key]
        if isinstance(got, BaseException):
            raise got
        return _It(got)

    monkeypatch.setattr(os, "scandir", scandir)
    return calls


def test_missing_ok_lists_an_existing_folder(tmp_path):
    (tmp_path / "a-破解.mp4").write_bytes(b"v")
    assert list_names_missing_ok(str(tmp_path)) == ["a-破解.mp4"]


def test_a_genuinely_missing_folder_with_a_listable_parent_is_empty(tmp_path):
    assert list_names_missing_ok(str(tmp_path / "out")) == []
    # a trailing separator is ignored (pathlib's name)
    assert list_names_missing_ok(str(tmp_path / "out") + os.sep) == []


def test_a_missing_folder_whose_parent_lists_it_is_unreachable(tmp_path, monkeypatch):
    # F13: WinError 53/67 also map to FileNotFoundError — when the parent
    # does list the folder, it is not "missing", it is unreachable.
    out = str(tmp_path / "out")
    _scripted(monkeypatch, {out: _fnf(53), str(tmp_path): ["out", "x"]})
    with pytest.raises(FileNotFoundError):
        list_names_missing_ok(out)


def test_an_unlistable_parent_is_unreachable(tmp_path, monkeypatch):
    out = str(tmp_path / "out")
    _scripted(monkeypatch, {out: _fnf(53), str(tmp_path): _fnf(53)})
    with pytest.raises(FileNotFoundError):
        list_names_missing_ok(out)
    _scripted(monkeypatch, {out: _fnf(3), str(tmp_path): PermissionError("x")})
    with pytest.raises(FileNotFoundError):
        list_names_missing_ok(out)


def test_the_parent_check_matches_case_and_normal_form(tmp_path, monkeypatch):
    nfd = unicodedata.normalize("NFD", "ガ")
    ga = unicodedata.normalize("NFC", "ガ")
    out = str(tmp_path / ga)
    _scripted(monkeypatch, {out: _fnf(53), str(tmp_path): [nfd]})
    with pytest.raises(FileNotFoundError):
        list_names_missing_ok(out)
    if os.path.normcase("A") == os.path.normcase("a"):  # Windows (F18)
        out = str(tmp_path / "Out")
        _scripted(monkeypatch, {out: _fnf(53), str(tmp_path): ["out"]})
        with pytest.raises(FileNotFoundError):
            list_names_missing_ok(out)


@pytest.mark.parametrize(
    "exc", [PermissionError(13, "denied"), NotADirectoryError(20, "file")]
)
def test_any_other_listing_failure_raises(tmp_path, monkeypatch, exc):
    out = str(tmp_path / "out")
    _scripted(monkeypatch, {out: exc})
    with pytest.raises(OSError):
        list_names_missing_ok(out)


@pytest.mark.skipif(sys.platform != "win32", reason="UNC / drive roots are Windows")
@pytest.mark.parametrize("root", ["\\\\host\\share", "\\\\host\\share\\", "Z:\\"])
def test_a_share_or_drive_root_is_never_missing(monkeypatch, root):
    # F18: a transient FileNotFoundError on a root means planning failed (a
    # parent listing would hit a missing key: KeyError, not the expected FNF).
    assert Path(root).parent == Path(root) or not Path(root).name
    calls = _scripted(monkeypatch, {root: _fnf(67)})
    with pytest.raises(FileNotFoundError):
        list_names_missing_ok(root)
    assert calls == [root]  # never asks a parent


def test_the_filesystem_root_is_never_missing(monkeypatch):
    root = os.path.abspath(os.sep)
    calls = _scripted(monkeypatch, {root: _fnf(3)})
    with pytest.raises(FileNotFoundError):
        list_names_missing_ok(root)
    assert calls == [root]


def test_entry_key_follows_the_platform_default_filesystem(monkeypatch):
    # CX2: Windows and macOS default volumes compare names case-insensitively
    # (POSIX normcase is a no-op, so macOS needs this explicitly); NFC always.
    monkeypatch.setattr(E, "_CASE_INSENSITIVE_FS", True)
    assert E.entry_key("A-破解.MP4") == E.entry_key("a-破解.mp4")
    monkeypatch.setattr(E, "_CASE_INSENSITIVE_FS", False)
    assert E.entry_key("A-破解.MP4") != E.entry_key("a-破解.mp4")
    assert E.entry_key("cafe\u0301.mp4") == E.entry_key("caf\u00e9.mp4")
