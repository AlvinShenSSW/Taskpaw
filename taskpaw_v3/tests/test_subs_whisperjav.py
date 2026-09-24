"""WhisperJAV argv/presets/flag rules/manifest outcome (#177, subs/whisperjav.py)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from taskpaw_v3.monitors.subs.srt import Cue
from taskpaw_v3.monitors.subs.whisperjav import (
    DEFAULT_ENGINE,
    ENGINES,
    FORBIDDEN_PREFIX,
    OWNED_FLAGS,
    PRESET_FLAGS,
    PRESETS,
    AsrOutcome,
    attempt_dir,
    build_argv,
    manifest_path,
    owned_flags_in,
    read_outcome,
)

SRT_TWO = (
    "1\n00:00:00,000 --> 00:00:01,000\nはい\n\n"
    "2\n00:00:02,000 --> 00:00:03,000\nいいえ\n"
)


def test_constants():
    assert ENGINES == ("anime-whisper", "large-v3", "large-v2", "qwen3", "custom")
    assert DEFAULT_ENGINE == "anime-whisper"
    assert set(PRESETS) == set(ENGINES)
    assert OWNED_FLAGS == (
        "--output-dir",
        "--output-format",
        "--language",
        "--temp-dir",
        "--no-signature",
    )
    assert PRESET_FLAGS == ("--mode", "--model", "--qwen-generator")
    assert FORBIDDEN_PREFIX == "--translate"


@pytest.mark.parametrize(
    "engine,preset",
    [
        ("anime-whisper", ["--mode", "qwen", "--qwen-generator", "anime-whisper"]),
        ("large-v3", ["--mode", "balanced", "--model", "large-v3"]),
        ("large-v2", ["--mode", "balanced"]),
        ("qwen3", ["--mode", "qwen"]),
        ("custom", []),
    ],
)
def test_build_argv_presets_exact(engine, preset):
    src = Path("D:/out/a b_restored.mp4")
    out = Path("D:/out/.avsubs/abc/attempt-1")
    tmp = Path("D:/out/.avsubs/tmp")
    argv = build_argv("C:/WJ/whisperjav.exe", src, out, tmp, engine, "")
    assert argv == [
        "C:/WJ/whisperjav.exe",
        str(src),
        *preset,
        "--language",
        "japanese",
        "--output-dir",
        str(out),
        "--output-format",
        "srt",
        "--temp-dir",
        str(tmp),
        "--no-signature",
    ]


def test_build_argv_appends_extra_last_split_with_shlex():
    argv = build_argv(
        "wj",
        Path("s.mp4"),
        Path("o"),
        Path("t"),
        "custom",
        '--mode fast --sensitivity aggressive --note "a b"',
    )
    assert argv[-6:] == [
        "--mode",
        "fast",
        "--sensitivity",
        "aggressive",
        "--note",
        "a b",
    ]
    assert argv[argv.index("--no-signature") + 1] == "--mode"


@pytest.mark.parametrize(
    "extra,engine,hits",
    [
        ("--output-dir x", "anime-whisper", ["--output-dir"]),
        ("--output-dir=x", "anime-whisper", ["--output-dir"]),
        ("--out x", "anime-whisper", ["--out"]),
        ("--lang ja", "anime-whisper", ["--lang"]),
        ("--temp /t", "anime-whisper", ["--temp"]),
        ("--no-sig", "anime-whisper", ["--no-sig"]),
        ("--mod x", "anime-whisper", ["--mod"]),
        ("--mode fast", "large-v2", ["--mode"]),
        ("--model=large-v3", "qwen3", ["--model"]),
        ("--qwen-gen x", "anime-whisper", ["--qwen-gen"]),
        ("--output-format vtt", "custom", ["--output-format"]),
        ("--translate", "anime-whisper", ["--translate"]),
        ("--translate-api-key k", "custom", ["--translate-api-key"]),
        ("--translate=yes", "large-v3", ["--translate"]),
        (
            "--translate-provider x --lang ja",
            "custom",
            ["--translate-provider", "--lang"],
        ),
    ],
)
def test_owned_flags_rejected(extra, engine, hits):
    assert owned_flags_in(extra, engine) == hits


@pytest.mark.parametrize(
    "extra,engine",
    [
        ("", "anime-whisper"),
        ("--mode fast", "custom"),
        ("--mode=qwen --qwen-generator anime-whisper --model x", "custom"),
        ("--sensitivity aggressive", "anime-whisper"),
        ("--vad-version v6", "anime-whisper"),
        ("--qwen-segmenter whisperseg", "anime-whisper"),
        ("--fail-on empty", "large-v3"),
        ("--ensemble", "large-v2"),
        ("-v --debug positional", "anime-whisper"),
        ("--o x", "anime-whisper"),  # < 4 chars: never treated as a prefix
    ],
)
def test_owned_flags_allowed(extra, engine):
    assert owned_flags_in(extra, engine) == []


def test_manifest_path():
    assert manifest_path(Path("x/y")) == Path("x/y") / "whisperjav_run.json"


def test_attempt_dir_hashing():
    root = Path("R")
    h = hashlib.sha1("sub/a.mp4".encode("utf-8")).hexdigest()[:12]
    assert attempt_dir(root, "sub/a.mp4", 2) == root / h / "attempt-2"
    assert attempt_dir(root, "sub/a.mp4", 1) != attempt_dir(root, "sub/b.mp4", 1)
    assert (
        attempt_dir(root, "日本.mp4", 1).parent.name
        == hashlib.sha1("日本.mp4".encode("utf-8")).hexdigest()[:12]
    )


# ── read_outcome with manifests shaped like run_outcome.write_manifest ──
def _manifest(out: Path, *files: dict) -> None:
    payload = {
        "whisperjav_version": "1.9.3",
        "mode": "qwen",
        "exit_status": 0,
        "counts": {},
        "files": [
            {
                "path": "D:/in/a_restored.mp4",
                "detail": "",
                "output": None,
                "subtitle_count": None,
                "error": None,
                **f,
            }
            for f in files
        ],
    }
    (out / "whisperjav_run.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _srt(out: Path, name: str = "a_restored.ja.whisperjav.srt", text=SRT_TWO) -> Path:
    p = out / name
    p.write_text(text, encoding="utf-8")
    return p


def test_read_outcome_done_succeeded(tmp_path):
    p = _srt(tmp_path)
    _manifest(tmp_path, {"state": "done", "output": str(p), "detail": "2 cue(s)"})
    o = read_outcome(tmp_path, 0, "")
    assert o == AsrOutcome(
        "succeeded",
        (Cue(1, 0, 1000, "はい"), Cue(2, 2000, 3000, "いいえ")),
        "",
    )


def test_read_outcome_suspect_is_success_with_detail(tmp_path):
    p = _srt(tmp_path)
    _manifest(tmp_path, {"state": "suspect", "output": str(p), "detail": "low mileage"})
    o = read_outcome(tmp_path, 0, "")
    assert o.kind == "succeeded" and len(o.cues) == 2
    assert o.detail == "suspect: low mileage"


def test_read_outcome_empty_is_no_speech(tmp_path):
    p = _srt(tmp_path, text="")
    _manifest(tmp_path, {"state": "empty", "output": str(p), "detail": "0 cues"})
    o = read_outcome(tmp_path, 0, "")
    assert o.kind == "no_speech" and o.cues == ()
    assert "0 cues" in o.detail


def test_read_outcome_done_with_zero_cue_srt_is_no_speech(tmp_path):
    p = _srt(tmp_path, text="")
    _manifest(tmp_path, {"state": "done", "output": str(p)})
    assert read_outcome(tmp_path, 0, "").kind == "no_speech"


def test_read_outcome_done_output_missing_on_disk_is_failed(tmp_path):
    _manifest(tmp_path, {"state": "done", "output": str(tmp_path / "gone.srt")})
    o = read_outcome(tmp_path, 0, "")
    assert o.kind == "failed" and "output missing" in o.detail


def test_read_outcome_done_without_output_field_is_failed(tmp_path):
    _manifest(tmp_path, {"state": "done", "output": None})
    o = read_outcome(tmp_path, 0, "")
    assert o.kind == "failed" and "manifest names no output" in o.detail


def test_read_outcome_ensemble_style_name_taken_from_manifest(tmp_path):
    # The computed `<stem>.ja.whisperjav.srt` does NOT exist; the manifest names
    # an ensemble-style output — the path must come from the manifest (D5).
    p = _srt(tmp_path, name="a_restored.ja.whisperjav_ensemble.merged.srt")
    _manifest(tmp_path, {"state": "done", "output": str(p)})
    o = read_outcome(tmp_path, 0, "")
    assert o.kind == "succeeded" and len(o.cues) == 2


@pytest.mark.parametrize("state", ["failed", "skipped", "weird"])
def test_read_outcome_other_states_fail(tmp_path, state):
    p = _srt(tmp_path)
    _manifest(tmp_path, {"state": state, "output": str(p), "detail": "boom"})
    o = read_outcome(tmp_path, 0, "TAIL")
    assert o.kind == "failed"
    assert o.detail.startswith(f"manifest state {state}: boom")
    assert "TAIL" in o.detail


def test_read_outcome_missing_manifest(tmp_path):
    o = read_outcome(tmp_path, 0, "")
    assert o.kind == "failed" and o.detail.startswith("no manifest")


@pytest.mark.parametrize(
    "content",
    ["not json", "[]", '{"files": []}', '{"files": "x"}', '{"files": [1]}'],
)
def test_read_outcome_bad_manifest(tmp_path, content):
    (tmp_path / "whisperjav_run.json").write_text(content, encoding="utf-8")
    o = read_outcome(tmp_path, 0, "")
    assert o.kind == "failed" and o.detail.startswith("no manifest")


def test_read_outcome_nonzero_rc_with_tail(tmp_path):
    p = _srt(tmp_path)
    _manifest(tmp_path, {"state": "done", "output": str(p)})
    o = read_outcome(tmp_path, 1, "CUDA out of memory")
    assert o == AsrOutcome("failed", (), "exit code 1: CUDA out of memory")


def test_read_outcome_no_exit_code(tmp_path):
    assert read_outcome(tmp_path, None, "") == AsrOutcome("failed", (), "no exit code")


def test_read_outcome_unparseable_srt(tmp_path):
    p = _srt(tmp_path, text="1\nnot a timestamp\nx\n")
    _manifest(tmp_path, {"state": "done", "output": str(p)})
    o = read_outcome(tmp_path, 0, "")
    assert o.kind == "failed" and o.detail.startswith("unparseable srt")


def test_read_outcome_relative_output_resolves_only_in_out_dir(tmp_path, monkeypatch):
    # A same-named file in the process CWD must never be read as this media's
    # subtitles (K-m2): a relative manifest output is `out_dir / name` only.
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    out = tmp_path / "attempt-1"
    out.mkdir()
    name = "a_restored.ja.whisperjav.srt"
    (cwd / name).write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nCWD-IMPOSTOR\n", encoding="utf-8"
    )
    monkeypatch.chdir(cwd)
    _manifest(out, {"state": "done", "output": name})
    missing = read_outcome(out, 0, "")
    assert missing.kind == "failed" and "output missing" in missing.detail
    _srt(out, name=name)
    o = read_outcome(out, 0, "")
    assert o.kind == "succeeded"
    assert [c.text for c in o.cues] == ["はい", "いいえ"]
