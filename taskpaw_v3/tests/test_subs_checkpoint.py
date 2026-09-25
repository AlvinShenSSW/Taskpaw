"""`CheckpointStore` (#192 AC3, subs/checkpoint.py): one JSON per film under the
agent's data dir, keyed by sha256(`srt.serialize(cues)`). Every test writes to
`tmp_path` only — never the real data dir (#68, C5)."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import pytest

from taskpaw_v3.monitors.subs import checkpoint as C
from taskpaw_v3.monitors.subs import srt
from taskpaw_v3.monitors.subs.checkpoint import (
    CHECKPOINT_MAX_AGE_S,
    CHECKPOINT_VERSION,
    CHECKPOINTS_DIRNAME,
    CheckpointStore,
    SavedCue,
)
from taskpaw_v3.monitors.subs.srt import Cue
from taskpaw_v3.monitors.subs.translate import model_label

KEY = "sk-KEYMARKER-ckpt-9f1c"


def _cues(n: int) -> list[Cue]:
    return [Cue(i + 1, i * 1000, i * 1000 + 500, f"せりふ{i}") for i in range(n)]


def _states(n: int) -> list[SavedCue]:
    out = []
    for i in range(n):
        if i % 3 == 0:
            out.append(SavedCue(zh=f"中{i}", by="grok-4.3 · api.x.ai"))
        elif i % 3 == 1:
            out.append(SavedCue(refused_by=("grok-4.3 · api.x.ai",)))
        else:
            out.append(SavedCue())
    return out


def test_constants():
    assert CHECKPOINT_VERSION == 1
    assert CHECKPOINTS_DIRNAME == "subs-checkpoints"
    assert CHECKPOINT_MAX_AGE_S == 30 * 24 * 3600


def test_key_is_sha256_of_the_serialized_cues():
    import hashlib

    cues = _cues(3)
    want = hashlib.sha256(srt.serialize(cues).encode("utf-8")).hexdigest()
    assert CheckpointStore.key(cues) == want
    assert len(want) == 64
    assert CheckpointStore.key(cues[:2]) != want


def test_key_equal_for_asr_cues_and_the_reloaded_ja_srt(tmp_path):
    # C4/A4: fresh WhisperJAV cues keep their own indices (gaps, not from 1)
    # and may carry blank lines inside a cue; the reloaded `.ja.srt` is
    # renumbered and normalised — the key is equal all the same.
    asr = [
        Cue(7, 0, 1200, "あの…\n\n  \nねえ"),
        Cue(9, 1500, 2500, "  はい  "),
        Cue(12, 3000, 3100, "○○さん\r\nこんにちは"),
        Cue(13, 3100, 3100, "え？"),
    ]
    path = tmp_path / "film.ja.srt"
    path.write_text(srt.serialize(asr), encoding="utf-8")
    reloaded = srt.load(path)
    assert [c.index for c in reloaded] == [1, 2, 3, 4]
    assert CheckpointStore.key(asr) == CheckpointStore.key(reloaded)


def test_round_trip(tmp_path):
    store = CheckpointStore(tmp_path)
    cues = _cues(7)
    key = store.key(cues)
    states = _states(7)
    assert store.save(key, "film.mp4", states) is True
    assert store.load(key, 7) == states
    data = json.loads((tmp_path / f"{key}.json").read_text(encoding="utf-8"))
    assert data["v"] == 1 and data["key"] == key and data["film"] == "film.mp4"
    assert len(data["cues"]) == 7
    assert data["cues"][0] == {"zh": "中0", "by": "grok-4.3 · api.x.ai"}
    assert data["cues"][1] == {"refused_by": ["grok-4.3 · api.x.ai"]}
    assert data["cues"][2] == {}
    # a second save replaces the file
    states[2] = SavedCue(zh="中2", by="deepseek-chat · api.deepseek.com")
    assert store.save(key, "film.mp4", states) is True
    assert store.load(key, 7) == states


def test_missing_file_loads_none(tmp_path):
    store = CheckpointStore(tmp_path)
    assert store.load(store.key(_cues(2)), 2) is None


def test_creates_the_directory(tmp_path):
    d = tmp_path / "data" / CHECKPOINTS_DIRNAME
    store = CheckpointStore(d)
    key = store.key(_cues(1))
    assert store.save(key, "f", [SavedCue(zh="x", by="m")]) is True
    assert (d / f"{key}.json").is_file()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: "{not json",
        lambda d: json.dumps([1, 2]),
        lambda d: json.dumps({**d, "v": 2}),  # unknown version
        lambda d: json.dumps({**d, "cues": d["cues"][:-1]}),  # count mismatch
        lambda d: json.dumps({**d, "key": "0" * 64}),  # another film's key
        lambda d: json.dumps({**d, "cues": [5] * len(d["cues"])}),
        lambda d: json.dumps({**d, "cues": [{"zh": 5}] * len(d["cues"])}),
        lambda d: json.dumps({**d, "cues": [{"refused_by": "m"}] * len(d["cues"])}),
        lambda d: json.dumps({**d, "cues": [{"refused_by": [3]}] * len(d["cues"])}),
        lambda d: json.dumps({**d, "cues": [{"by": ["m"]}] * len(d["cues"])}),
    ],
)
def test_corrupt_version_or_count_mismatch_is_renamed_bad(tmp_path, caplog, mutate):
    caplog.set_level(logging.WARNING)
    store = CheckpointStore(tmp_path)
    cues = _cues(4)
    key = store.key(cues)
    assert store.save(key, "film", _states(4))
    path = tmp_path / f"{key}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(mutate(data), encoding="utf-8")
    assert store.load(key, 4) is None  # start over
    assert not path.exists()
    assert (tmp_path / f"{key}.bad").is_file()
    assert "checkpoint" in caplog.text
    # the next save starts a fresh file
    assert store.save(key, "film", _states(4))
    assert store.load(key, 4) == _states(4)


def test_undecodable_file_is_renamed_bad(tmp_path):
    store = CheckpointStore(tmp_path)
    key = store.key(_cues(1))
    (tmp_path / f"{key}.json").write_bytes(b"\xff\xfe\x00garbage")
    assert store.load(key, 1) is None
    assert (tmp_path / f"{key}.bad").is_file()


def test_a_blank_saved_translation_is_not_done(tmp_path):
    store = CheckpointStore(tmp_path)
    key = store.key(_cues(2))
    body = {"v": 1, "key": key, "film": "f", "cues": [{"zh": "  ", "by": "m"}, {}]}
    (tmp_path / f"{key}.json").write_text(json.dumps(body), encoding="utf-8")
    got = store.load(key, 2)
    assert got == [SavedCue(zh=None, by="m"), SavedCue()]


def test_atomic_no_partial_file_on_replace_failure(tmp_path, monkeypatch, caplog):
    store = CheckpointStore(tmp_path)
    key = store.key(_cues(3))
    assert store.save(key, "film", _states(3))
    before = (tmp_path / f"{key}.json").read_bytes()

    def boom(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(C.os, "replace", boom)
    assert store.save(key, "film", [SavedCue(zh="新", by="m")] * 3) is False
    assert (tmp_path / f"{key}.json").read_bytes() == before  # old file intact
    assert [p.name for p in tmp_path.iterdir()] == [f"{key}.json"]  # no tmp left
    assert "OSError" in caplog.text


def test_write_failure_returns_false_and_never_raises(tmp_path, caplog):
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x", encoding="utf-8")
    store = CheckpointStore(blocker / "sub")
    key = store.key(_cues(1))
    assert store.save(key, "f", [SavedCue()]) is False
    assert store.load(key, 1) is None
    store.delete(key)  # never raises
    assert store.prune() == 0


def test_none_directory_is_memory_only(tmp_path, monkeypatch):
    store = CheckpointStore(None)
    key = store.key(_cues(2))
    assert store.directory is None
    assert store.save(key, "f", _states(2)) is True  # nothing to persist
    assert store.load(key, 2) is None
    store.delete(key)
    assert store.prune() == 0
    assert list(tmp_path.iterdir()) == []


def test_delete(tmp_path):
    store = CheckpointStore(tmp_path)
    key = store.key(_cues(2))
    assert store.save(key, "f", _states(2))
    store.delete(key)
    assert not (tmp_path / f"{key}.json").exists()
    store.delete(key)  # missing: fine


@pytest.mark.parametrize("bad", ["", "../../etc/passwd", "A" * 64, "0" * 63, None, 5])
def test_invalid_keys_never_touch_the_disk(tmp_path, bad):
    store = CheckpointStore(tmp_path)
    victim = tmp_path / "keep.json"
    victim.write_text("{}", encoding="utf-8")
    assert store.load(bad, 1) is None
    assert store.save(bad, "f", [SavedCue()]) is False
    store.delete(bad)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["keep.json"]


def test_prune_removes_only_our_old_files(tmp_path):
    store = CheckpointStore(tmp_path)
    old_key, new_key = store.key(_cues(1)), store.key(_cues(2))
    assert store.save(old_key, "old", [SavedCue()])
    assert store.save(new_key, "new", _states(2))
    bad = tmp_path / f"{'b' * 64}.bad"
    bad.write_text("x", encoding="utf-8")
    tmp = tmp_path / f".{'c' * 64}.abc123.tmp"
    tmp.write_text("x", encoding="utf-8")
    foreign = tmp_path / "notes.txt"
    foreign.write_text("mine", encoding="utf-8")
    now = time.time()
    ancient = now - CHECKPOINT_MAX_AGE_S - 60
    for p in (tmp_path / f"{old_key}.json", bad, tmp, foreign):
        os.utime(p, (ancient, ancient))
    assert store.prune(now=now) == 3
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == sorted([f"{new_key}.json", "notes.txt"])


def test_prune_missing_directory_is_fine(tmp_path):
    assert CheckpointStore(tmp_path / "nope").prune() == 0


def test_holds_labels_only_never_a_key_userinfo_or_port(tmp_path):
    # The engine stores `model_label`s; a base carrying userinfo + a port
    # yields the host only.
    label = model_label("grok-4.3", f"https://u:{KEY}@api.x.ai:443/v1")
    store = CheckpointStore(tmp_path)
    key = store.key(_cues(2))
    assert store.save(
        key, "film", [SavedCue(zh="中", by=label), SavedCue(refused_by=(label,))]
    )
    text = (tmp_path / f"{key}.json").read_text(encoding="utf-8")
    assert KEY not in text and "u:" not in text and "443" not in text
    assert "api.x.ai" in text


def test_film_name_round_trips_as_utf8(tmp_path):
    store = CheckpointStore(tmp_path)
    key = store.key(_cues(1))
    assert store.save(key, "FC2-PPV-3620789 破解.mp4", [SavedCue(zh="你好", by="m")])
    raw = (tmp_path / f"{key}.json").read_bytes()
    assert "破解".encode("utf-8") in raw and "你好".encode("utf-8") in raw


def test_directory_is_a_path(tmp_path):
    assert isinstance(CheckpointStore(str(tmp_path)).directory, Path)  # type: ignore[arg-type]
