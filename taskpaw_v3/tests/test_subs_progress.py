"""Pure progress observation (#189, subs/progress.py): the WhisperJAV parser
(`AsrProgress`), the per-film step tracker (`FilmTracker` + `LiveFacts`) and
the capture `eta` parser (`parse_eta`).

No child process, no clock: every `now` is passed explicitly. The recorded
fixture is an excerpt of a real WhisperJAV 1.9.3 anime-whisper run (41 s clip,
lines 17–116 of the captured output) with local absolute paths replaced."""

from __future__ import annotations

import collections
import json
import math

import pytest

from taskpaw_v3.monitors.subs.progress import (
    ASR,
    AVSUBS_STEPS,
    FINISHED_ROWS,
    JASNA_STEPS,
    MAX_ROWS,
    NAME_CHARS,
    PHASES,
    RESTORE,
    TRANSLATE,
    WEIGHTS,
    AsrProgress,
    FilmTracker,
    LiveFacts,
    parse_eta,
)

# ── recorded run (WhisperJAV 1.9.3, anime-whisper, qwen pipeline) ─────────
RECORDED_RUN = r"""2026-09-24 17:00:20 - whisperjav - INFO - [QwenPipeline PID 25568] Processing: ja_speech.mp4 (model=litagin/anime-whisper)
2026-09-24 17:00:20 - whisperjav - INFO - [QwenPipeline PID 25568] Phase 1: Extracting audio from ja_speech.mp4
2026-09-24 17:00:20 - whisperjav - INFO - Extracting the audio from ja_speech.mp4...
2026-09-24 17:00:20 - whisperjav - INFO - Audio ready: 40.8 seconds of audio, extracted in 0.0 seconds
2026-09-24 17:00:20 - whisperjav - INFO - [QwenPipeline PID 25568] Phase 1: Complete (40.8s audio)
2026-09-24 17:00:20 - whisperjav - INFO - [QwenPipeline PID 25568] Phase 2: Scene detection (method=semantic, safe_chunking=True)
2026-09-24 17:00:20 - whisperjav - INFO - [QwenPipeline PID 25568] Phase 2: Safe chunking (min=12s, max=48s, aligner limit=180s)
2026-09-24 17:00:20 - whisperjav - INFO - SemanticSceneDetector initialized
2026-09-24 17:00:20 - whisperjav - INFO - Starting Semantic Audio Clustering for: %TEMP%\whisperjav\ja_speech_extracted.wav
2026-09-24 17:00:21 - whisperjav - INFO - SemanticAudioClustering engine loaded (v7.2.0)
2026-09-24 17:00:21 - whisperjav - INFO - Starting processing for: %TEMP%\whisperjav\ja_speech_extracted.wav
2026-09-24 17:00:21 - whisperjav - INFO - SemanticAudioClustering engine v7.2.0
2026-09-24 17:00:21 - whisperjav - INFO - --> [1/5] Streaming features (60s chunks)...
2026-09-24 17:00:21 - whisperjav - INFO - [Semantic Diag] numpy=2.2.6  librosa=0.11.0  numba=0.67.0  soundfile=0.14.0
2026-09-24 17:00:21 - whisperjav - INFO - [Semantic Diag] ffmpeg: ffmpeg version 8.0.1-full_build-www.gyan.dev Copyright (c) 2000-2025 the FFmpeg developers
2026-09-24 17:00:21 - whisperjav - INFO - [Semantic Diag] All version checks passed.
2026-09-24 17:00:21 - whisperjav - INFO - [1/5 diag] Step 1: sf.info(ja_speech_extracted.wav)...
2026-09-24 17:00:21 - whisperjav - INFO - [1/5 diag] Step 1 OK: WAV PCM_16, 16000Hz, 1ch, 40.8s (0.00s)
2026-09-24 17:00:21 - whisperjav - INFO - [1/5 diag] Step 2: Reading audio metadata...
2026-09-24 17:00:21 - whisperjav - INFO - [1/5 diag] Step 2 OK: 40.8s, 16000Hz, block_size=960000, total_blocks=1 (0.00s)
2026-09-24 17:00:21 - whisperjav - INFO - [1/5 diag] Step 3: Starting sf.blocks() iterator...
2026-09-24 17:00:21 - whisperjav - INFO - [1/5 diag] Step 3 OK: First block received (shape=(652288, 1), 0.00s)
2026-09-24 17:00:21 - whisperjav - INFO - [1/5 diag] Step 5: librosa feature extraction (first chunk)...
2026-09-24 17:00:22 - whisperjav - INFO - [1/5 diag] Step 5 OK: First chunk features extracted (shape=(36, 1275), 1.20s)
2026-09-24 17:00:22 - whisperjav - INFO - [1/5 diag] Streaming complete: 1 chunks in 1.2s
2026-09-24 17:00:22 - whisperjav - INFO - --> [3/5] Calibrating thresholds...
2026-09-24 17:00:22 - whisperjav - INFO - --> [2/5] Clustering, Merging & Snapping to Silence...
2026-09-24 17:00:22 - whisperjav - INFO -     -> Snapping cut points to silence (onset-anchored)...
2026-09-24 17:00:22 - whisperjav - INFO - --> [4/5] Building Metadata...
2026-09-24 17:00:22 - whisperjav - INFO - Done! JSON at %TEMP%\whisperjav\scenes\ja_speech_semantic.json
2026-09-24 17:00:22 - whisperjav - INFO - Semantic clustering complete: 1 scenes extracted
2026-09-24 17:00:22 - whisperjav - INFO - Semantic scene detection complete: 1 scenes in 2.0s
2026-09-24 17:00:23 - whisperjav - INFO - ------------------------------------------------------------------
2026-09-24 17:00:23 - whisperjav - INFO -   Audio analytics - ja_speech_extracted.wav
2026-09-24 17:00:23 - whisperjav - INFO - ------------------------------------------------------------------
2026-09-24 17:00:23 - whisperjav - INFO -   1 scenes, 0:40 total. Speech detected in 57% of the running time.
2026-09-24 17:00:23 - whisperjav - INFO - ------------------------------------------------------------------
2026-09-24 17:00:23 - whisperjav - INFO - [QwenPipeline PID 25568] Phase 2: Detected 1 scenes (method=semantic)
2026-09-24 17:00:23 - whisperjav - INFO - [QwenPipeline PID 25568] Phase 2: Scene durations — total 41s, range 41–41s, mean 41s
2026-09-24 17:00:23 - whisperjav - INFO - [QwenPipeline PID 25568] Phase 3: Speech enhancement (backend=none)
2026-09-24 17:00:23 - whisperjav - INFO - [QwenPipeline PID 25568] Phase 3: Passthrough — 1 scenes at 16kHz, skipping enhancement
2026-09-24 17:00:23 - whisperjav - INFO - [QwenPipeline PID 25568] Phase 3: Complete (0.0s)
2026-09-24 17:00:23 - whisperjav - INFO - [QwenPipeline PID 25568] Phase 4: Speech segmentation (backend=whisperseg)
2026-09-24 17:00:29 - whisperjav - INFO - WhisperSeg model resolved: %HF_HOME%\hub\model.onnx
2026-09-24 17:00:30 - whisperjav - INFO - WhisperSeg ready: device=CPU, chunk=30000ms, frame=20ms
2026-09-24 17:00:30 - whisperjav - INFO - [QwenPipeline PID 25568] Phase 4: Complete (7.9s)
2026-09-24 17:00:30 - whisperjav - INFO - [QwenPipeline PID 25568] Phase 5: ASR transcription (model=litagin/anime-whisper, mode=assembly)
2026-09-24 17:00:30 - whisperjav - INFO - [DecoupledPipeline] Processing 1 scenes (aligner=none, step-down=disabled)
2026-09-24 17:00:30 - whisperjav - INFO - [DecoupledPipeline] Step 1: Framing 1 scenes
2026-09-24 17:00:30 - whisperjav - INFO - [DecoupledPipeline] Step 1: pathless mode — in-memory frame audio, no temp WAVs
2026-09-24 17:00:30 - whisperjav - INFO - WhisperSeg model resolved: %HF_HOME%\hub\model.onnx
2026-09-24 17:00:31 - whisperjav - INFO - WhisperSeg ready: device=CPU, chunk=30000ms, frame=20ms
2026-09-24 17:00:31 - whisperjav - INFO - [VadGroupedFramer] whisperseg: 7 segments → 7 groups → 7 frames (40.8s audio, 54.4% speech)
2026-09-24 17:00:31 - whisperjav - INFO - [DecoupledPipeline] Step 1: Complete — 1 scenes, 7 total frames
2026-09-24 17:00:31 - whisperjav - INFO - [DecoupledPipeline] Steps 2-4: Generating + cleaning text for 1 scenes
2026-09-24 17:00:32 - whisperjav - INFO - [AnimeWhisperGenerator] Loading model...
2026-09-24 17:00:32 - whisperjav - INFO -   Model:  litagin/anime-whisper
2026-09-24 17:00:32 - whisperjav - INFO -   Device: cuda:0
2026-09-24 17:00:32 - whisperjav - INFO -   Dtype:  torch.float16
2026-09-24 17:01:08 - whisperjav - INFO - [AnimeWhisperGenerator] Model loaded (35.4s)
2026-09-24 17:01:08 - whisperjav - INFO - [DecoupledPipeline] Generating scene 1/1 (40.8s audio)...
The attention mask is not set and cannot be inferred from input because pad token is same as eos token. As a consequence, you may observe unexpected behavior. Please pass your input's `attention_mask` to obtain reliable results.
2026-09-24 17:01:09 - whisperjav - INFO - [AnimeWhisperGenerator] Model unloaded
2026-09-24 17:01:09 - whisperjav - INFO - [DecoupledPipeline] Cleaning 1 scenes
2026-09-24 17:01:09 - whisperjav - INFO - [AnimeWhisperCleaner] Cleaned 6/7 items — 106 → 112 chars (--6)
2026-09-24 17:01:09 - whisperjav - INFO - [DecoupledPipeline] Steps 2-4: Complete — 1 scenes, 112 chars (-6 removed by cleaning), 0 empty (38.1s)
2026-09-24 17:01:09 - whisperjav - INFO - [DecoupledPipeline] Step 9: Reconstructing 1 scenes
2026-09-24 17:01:09 - whisperjav - INFO - [DecoupledPipeline] Scene 1/1: 7 words → 7 segments (sentinel: N/A)
2026-09-24 17:01:09 - whisperjav - INFO - [DecoupledPipeline] Step 9: Complete — 1 scenes, 7 total segments, 0 collapses
2026-09-24 17:01:09 - whisperjav - INFO - [QwenPipeline] Phase 5 assembly summary:
2026-09-24 17:01:09 - whisperjav - INFO -   Scenes:    1 success, 0 empty, 0 failed (of 1)
2026-09-24 17:01:09 - whisperjav - INFO -   Segments:  7 total (7.0 avg/scene)
2026-09-24 17:01:09 - whisperjav - INFO -   Sentinel:  0 collapses, 0 recoveries
2026-09-24 17:01:09 - whisperjav - INFO - [QwenPipeline PID 25568] Phase 5: Complete (38.7s)
2026-09-24 17:01:09 - whisperjav - INFO - [QwenPipeline PID 25568] Phase 6: Generating scene SRT files
Saved: %TEMP%\whisperjav\scene_srts\ja_speech_scene_0000.srt
2026-09-24 17:01:09 - whisperjav - INFO - [QwenPipeline PID 25568] Phase 7: Stitching 1 scene SRTs
2026-09-24 17:01:09 - whisperjav - INFO - [QwenPipeline PID 25568] Phase 7: Stitched 7 subtitles
2026-09-24 17:01:09 - whisperjav - INFO - [QwenPipeline PID 25568] Phase 8: anime SRT filter — 7 → 7 entries (-0 ellipsis-only, -0 empty)
2026-09-24 17:01:09 - whisperjav - INFO - [QwenPipeline PID 25568] Phase 8: nonverbal line filter - 7 -> 7 entries (-0 nonverbal, -0 empty)
2026-09-24 17:01:09 - whisperjav - INFO - [QwenPipeline PID 25568] Phase 8: scene-overlap resolver - 7 -> 7 entries (-0 nested duplicate, 0 starts shifted)
2026-09-24 17:01:09 - whisperjav - INFO - [QwenPipeline PID 25568] Phase 8: 7 subtitles in final output
2026-09-24 17:01:09 - whisperjav - INFO -
2026-09-24 17:01:09 - whisperjav - INFO - ============================================================
2026-09-24 17:01:09 - whisperjav - INFO -  PIPELINE ANALYTICS - ja_speech
2026-09-24 17:01:09 - whisperjav - INFO - ============================================================
2026-09-24 17:01:09 - whisperjav - INFO -  Audio       0:40 total | 1 scenes | mean 40.8s | median 40.8s
2026-09-24 17:01:09 - whisperjav - INFO -  Speech      0:22 VAD | 54.3% speech ratio
2026-09-24 17:01:09 - whisperjav - INFO -  Subtitles   7 subs | 0:22 duration | 54.3% coverage | 10.3/min
2026-09-24 17:01:09 - whisperjav - INFO -  Timing      0.0% aligner | 0.0% interpolated | 0.0% vad_fallback
2026-09-24 17:01:09 - whisperjav - INFO -
2026-09-24 17:01:09 - whisperjav - INFO -  [+] Collapse rate: 0.0%
2026-09-24 17:01:09 - whisperjav - INFO -  [!] Aligner native: 0.0%
2026-09-24 17:01:09 - whisperjav - INFO -  [+] Speech ratio: 54.3%
2026-09-24 17:01:09 - whisperjav - INFO -  [+] Max gap: 4.8s
2026-09-24 17:01:09 - whisperjav - INFO -  [+] Short subs (<0.3s): 0.0%
2026-09-24 17:01:09 - whisperjav - INFO - ============================================================
2026-09-24 17:01:09 - whisperjav - INFO -
2026-09-24 17:01:09 - whisperjav - INFO - Analytics saved: ja_speech.ja.whisperjav.analytics.json
2026-09-24 17:01:09 - whisperjav - INFO - [QwenPipeline PID 25568] Complete: ja_speech.ja.whisperjav.srt (7 subtitles in 0:00:48)
"""

P = "2026-09-25 10:00:00 - whisperjav - INFO - "
KEYS = ("phase", "phase_n", "scene", "scenes", "percent", "eta_s", "elapsed_s")


def _q(k: int, text: str = "x") -> str:
    return f"{P}[QwenPipeline PID 7] Phase {k}: {text}"


def _gen(i: int, n: int) -> str:
    return f"{P}[DecoupledPipeline] Generating scene {i}/{n} (26.1s audio)..."


def _align(i: int, n: int) -> str:
    return f"{P}[DecoupledPipeline] Aligning scene {i}/{n}"


GEN_DONE = f"{P}[DecoupledPipeline] Steps 2-4: Complete — 276 scenes, 0 empty"


def _phase5(p: AsrProgress, now: float = 1.0) -> None:
    p.feed_text("\n".join(_q(k) for k in range(1, 5)) + "\n" + _q(5, "ASR"), now)


def _poll_like_the_child(lines: list[str]) -> list[dict]:
    """One poll per new line over a 40-line / 16000-char tail, as
    `SubsJob.progress` feeds `ChildProcess.tail(lines=40, max_chars=16000)`."""
    p = AsrProgress(0.0)
    tail: collections.deque[str] = collections.deque(maxlen=40)
    out = []
    for n, line in enumerate(lines, start=1):
        tail.append(line)
        p.feed_text("\n".join(tail)[-16000:], float(n))
        out.append(p.snapshot(float(n)))
    return out


# ── AsrProgress ───────────────────────────────────────────────────────────
def test_constants():
    assert PHASES == 8
    assert set(WEIGHTS) == set(range(1, 9))
    assert math.isclose(sum(WEIGHTS.values()), 1.0)
    assert (WEIGHTS[4], WEIGHTS[5]) == (0.12, 0.75)


def test_recorded_run_phases_scene_and_final():
    lines = RECORDED_RUN.splitlines()
    snaps = _poll_like_the_child(lines)
    for s in snaps:
        assert tuple(s) == KEYS
        assert s["phase_n"] == 8  # the first line is already a pipeline line

    def snap_after(fragment: str) -> dict:
        return next(s for line, s in zip(lines, snaps) if fragment in line)

    assert snap_after("Processing: ja_speech.mp4")["phase"] is None
    assert snap_after("Processing: ja_speech.mp4")["percent"] is None
    assert snap_after("Phase 1: Extracting")["percent"] == 0
    assert snap_after("Phase 2: Scene detection")["percent"] == 3
    assert snap_after("Phase 3: Speech enhancement")["percent"] == 7
    assert snap_after("Phase 4: Speech segmentation")["percent"] == 8
    p5 = snap_after("Phase 5: ASR transcription")
    assert (p5["phase"], p5["percent"], p5["scene"]) == (5, 20, None)
    gen = snap_after("Generating scene 1/1")
    # C3: the line is logged BEFORE the scene is generated → (1 − 1)/1.
    assert (gen["scene"], gen["scenes"], gen["percent"]) == (1, 1, 20)
    assert snap_after("Steps 2-4: Complete")["percent"] == 88
    assert snap_after("Phase 6: Generating scene SRT")["percent"] == 95
    assert snap_after("Phase 7: Stitching")["percent"] == 96
    assert snap_after("Phase 8: anime SRT filter")["percent"] == 98
    final = snap_after("Phase 8: 7 subtitles in final output")
    assert (final["phase"], final["percent"]) == (8, 99)
    last = snaps[-1]
    assert (last["phase"], last["percent"], last["scene"], last["scenes"]) == (
        8,
        99,
        1,
        1,
    )
    assert last["eta_s"] is None  # a 1-scene clip never earns an ETA
    percents = [s["percent"] for s in snaps if s["percent"] is not None]
    assert percents == sorted(percents)  # monotonic
    assert max(percents) == 99  # 100 only when the job settles


def test_gate_closed_balanced_pipeline_is_elapsed_only():
    # D9: the balanced pipeline's display line and a stray "Phase 8" text
    # without the pipeline tag never produce a phase or percent.
    p = AsrProgress(100.0)
    p.feed_text(
        "Transcribing: [=====] 1/1 [100.0%] | x_scene_0000.wav  Scene 1/1 (41s, "
        "Internal FW Silero VAD): 2 subtitle(s) in 2s\n"
        f"{P}Phase 8: 7 subtitles in final output\n"
        f"{P}Generating scene 3/10\n"
        f"{P}Steps 2-4: Complete",
        110.0,
    )
    assert p.snapshot(112.9) == {
        "phase": None,
        "phase_n": None,
        "scene": None,
        "scenes": None,
        "percent": None,
        "eta_s": None,
        "elapsed_s": 12,
    }


def test_filename_containing_phase_8_has_no_effect():
    p = AsrProgress(0.0)
    p.feed_text(
        f"{P}[QwenPipeline PID 1] Processing: Phase 8 Final Cut-破解.mp4 (model=x)",
        1.0,
    )
    s = p.snapshot(2.0)
    assert (s["phase"], s["phase_n"], s["percent"]) == (None, 8, None)
    p.feed_text(
        f"{P}[QwenPipeline PID 1] Phase 1: Extracting audio from "
        "Phase 8: 7 subtitles in final output.mp4",
        3.0,
    )
    s = p.snapshot(3.0)
    assert (s["phase"], s["percent"]) == (1, 0)


def test_decoupled_pid_tag_and_passed_through_final():
    p = AsrProgress(0.0)
    p.feed_text(f"{P}[DecoupledPipeline PID 9] Phase 3: Speech enhancement", 1.0)
    assert p.snapshot(1.0)["phase"] == 3
    p.feed_text(f"{P}[DecoupledPipeline PID 9] Phase 8: 12 subtitles passed through", 2)
    assert p.snapshot(2.0)["percent"] == 99


def test_long_film_framer_burst_without_phase5_line_in_the_tail():
    # D2/C5: the single "Phase 5" line is pushed out of the 40-line tail by
    # the framer's per-scene lines; any [DecoupledPipeline] line implies ≥ 5.
    n = 276
    p = AsrProgress(0.0)
    p.feed_text("\n".join(_q(k) for k in range(1, 5)), 10.0)
    assert (p.snapshot(10.0)["phase"], p.snapshot(10.0)["percent"]) == (4, 8)
    burst = (
        [_q(5, "ASR transcription (model=litagin/anime-whisper, mode=assembly)")]
        + [f"{P}[DecoupledPipeline] Step 1: Framing {n} scenes"]
        + [
            f"{P}[VadGroupedFramer] whisperseg: 9 segments -> 9 groups -> 9 frames"
            for _ in range(n)
        ]
    )
    tail = "\n".join(burst[-40:])
    assert "Phase 5:" not in tail and "[DecoupledPipeline]" not in tail
    p.feed_text(tail, 20.0)
    assert p.snapshot(20.0)["phase"] == 4  # nothing seen yet: never a guess
    tail = "\n".join(
        (burst + [f"{P}[DecoupledPipeline] Step 1: Complete — {n} scenes"])[-40:]
    )
    p.feed_text(tail, 30.0)
    s = p.snapshot(30.0)
    assert (s["phase"], s["percent"], s["scene"]) == (5, 20, None)
    p.feed_text("\n".join(_gen(i, n) for i in range(99, 139)), 40.0)
    s = p.snapshot(40.0)
    assert (s["phase"], s["scene"], s["scenes"], s["percent"]) == (5, 138, n, 54)


def test_scene_fraction_is_i_minus_one_over_n():
    p = AsrProgress(0.0)
    _phase5(p)
    p.feed_text(_gen(2, 4), 2.0)
    assert p.snapshot(2.0)["percent"] == 37


def test_eta_gate_needs_two_advances_spanning_30s_then_formula():
    p = AsrProgress(0.0)
    _phase5(p)
    p.feed_text(_gen(1, 276), 100.0)
    p.feed_text(_gen(5, 276), 110.0)
    p.feed_text(_gen(10, 276), 125.0)
    assert p.snapshot(125.0)["eta_s"] is None  # 25 s < 30 s
    p.feed_text(_gen(20, 276), 160.0)
    # rate = (20 − 1)/(160 − 100); ceil((276 − 20 + 1)/rate + 60)
    assert p.snapshot(160.0)["eta_s"] == 872
    assert p.snapshot(9999.0)["eta_s"] == 872  # from the samples, not `now`


def test_eta_needs_at_least_two_advances():
    p = AsrProgress(0.0)
    _phase5(p)
    p.feed_text(_gen(1, 276), 0.0)
    p.feed_text(_gen(10, 276), 40.0)
    assert p.snapshot(40.0)["eta_s"] is None  # one advance only
    p.feed_text(_gen(11, 276), 41.0)
    assert p.snapshot(41.0)["eta_s"] == 1151


def test_a_burst_in_one_tail_is_one_advance():
    p = AsrProgress(0.0)
    _phase5(p)
    p.feed_text(_gen(1, 276), 0.0)
    p.feed_text("\n".join(_gen(i, 276) for i in range(2, 31)), 40.0)
    s = p.snapshot(40.0)
    assert s["scene"] == 30 and s["eta_s"] is None


def test_eta_stops_after_generation_and_outside_phase_5():
    p = AsrProgress(0.0)
    _phase5(p)
    for t, i in ((0, 1), (20, 5), (40, 9)):
        p.feed_text(_gen(i, 20), float(t))
    assert p.snapshot(40.0)["eta_s"] is not None
    p.feed_text(GEN_DONE, 50.0)
    assert p.snapshot(50.0)["eta_s"] is None
    assert p.snapshot(50.0)["percent"] == 88


def test_step_down_re_pass_with_a_different_n_is_ignored():
    n = 276
    p = AsrProgress(0.0)
    _phase5(p)
    p.feed_text("\n".join(_gen(i, n) for i in range(1, n + 1)), 10.0)
    p.feed_text(GEN_DONE, 20.0)
    before = p.snapshot(20.0)
    p.feed_text(
        f"{P}[DecoupledPipeline] Step-down: 5/{n} scenes collapsed, retrying\n"
        + _gen(1, 5)
        + "\n"
        + _gen(4, 5),
        30.0,
    )
    after = p.snapshot(20.0)
    assert after == before
    assert (after["scene"], after["scenes"], after["percent"]) == (n, n, 88)


def test_step_down_before_generation_done_does_not_move_the_scene():
    p = AsrProgress(0.0)
    _phase5(p)
    p.feed_text(_gen(7, 10), 1.0)
    p.feed_text(_gen(9, 12), 2.0)  # a different N: not this pass
    assert (p.snapshot(2.0)["scene"], p.snapshot(2.0)["scenes"]) == (7, 10)


def test_align_sub_phase():
    p = AsrProgress(0.0)
    _phase5(p)
    p.feed_text(_align(1, 10), 1.0)  # before any GEN line: no N yet
    p.feed_text("\n".join(_gen(i, 10) for i in range(1, 11)), 2.0)
    p.feed_text(_align(6, 10), 3.0)  # during generation: not used yet
    assert p.snapshot(3.0)["percent"] == pct_generating(10, 10)
    p.feed_text(GEN_DONE, 4.0)
    assert p.snapshot(4.0)["percent"] == 91  # 0.9 + 0.1 × (6 − 1)/10
    p.feed_text(_align(3, 5), 5.0)  # another N: ignored
    p.feed_text(_align(4, 10), 5.0)  # lower: max kept
    assert p.snapshot(5.0)["percent"] == 91


def pct_generating(i: int, n: int) -> int:
    return round(100 * (0.2 + 0.75 * 0.9 * (i - 1) / n))


def test_gen_done_without_any_gen_line_still_counts_generation_done():
    p = AsrProgress(0.0)
    _phase5(p)
    p.feed_text(GEN_DONE, 1.0)
    s = p.snapshot(1.0)
    assert (s["phase"], s["percent"], s["scene"], s["scenes"]) == (5, 88, None, None)


def test_phase_6_plus_counts_phase_5_fully():
    p = AsrProgress(0.0)
    _phase5(p)
    p.feed_text(_gen(2, 100), 1.0)
    p.feed_text(_q(6, "Generating scene SRT files"), 2.0)
    assert p.snapshot(2.0)["percent"] == 95


def test_monotonic_when_an_older_tail_arrives_again():
    p = AsrProgress(0.0)
    _phase5(p)
    p.feed_text(_q(8, "7 subtitles in final output"), 5.0)
    assert p.snapshot(5.0)["percent"] == 99
    p.feed_text(_q(5, "ASR") + "\n" + _gen(1, 10), 6.0)
    s = p.snapshot(6.0)
    assert (s["phase"], s["percent"]) == (8, 99)


def test_repeated_tails_are_idempotent():
    lines = [_q(k) for k in range(1, 5)] + [_q(5, "ASR")]
    lines += [_gen(i, 50) for i in range(1, 6)]
    text = "\n".join(lines)
    p = AsrProgress(0.0)
    p.feed_text(_gen(1, 50), 0.0)  # the first sample
    p.feed_text(text, 40.0)
    once = p.snapshot(100.0)
    for t in (41.0, 42.0, 70.0):
        p.feed_text(text, t)
    assert p.snapshot(100.0) == once


def test_snapshot_is_a_fresh_dict_and_elapsed_never_negative():
    p = AsrProgress(50.0)
    a = p.snapshot(40.0)
    assert a["elapsed_s"] == 0
    a["phase"] = 7
    assert p.snapshot(40.0)["phase"] is None
    assert p.snapshot(40.0) is not p.snapshot(40.0)


@pytest.mark.parametrize(
    "junk",
    [
        None,
        b"[DecoupledPipeline] Generating scene 1/2",
        12,
        "",
        "\x00\x01\x02",
        "ne 5/276 (26.1s audio)...",  # a head cut off by max_chars
        f"{P}[DecoupledPipeline] Generating scene 3/",
        f"{P}[DecoupledPipeline] Generating scene 0/5",
        f"{P}[DecoupledPipeline] Generating scene 6/5",
        f"{P}[DecoupledPipeline] Generating scene 1/0",
        f"{P}[DecoupledPipeline] Generating scene {'9' * 5000}/{'9' * 5000}",
        f"{P}[DecoupledPipeline] Aligning scene {'9' * 5000}/3",
        f"{P}[QwenPipeline PID {'1' * 5000}] Phase 0: nothing",
        f"{P}[QwenPipeline PID 1] Phase 9: nothing",
        "[" * 10000,
    ],
)
def test_garbage_and_partial_lines_never_raise_nor_move_the_scene(junk):
    p = AsrProgress(0.0)
    _phase5(p)
    p.feed_text(junk, 5.0)
    s = p.snapshot(5.0)
    assert (s["phase"], s["scene"], s["scenes"], s["percent"]) == (5, None, None, 20)


def test_garbage_now_never_raises():
    p = AsrProgress(0.0)
    p.feed_text(_q(5, "ASR") + "\n" + _gen(1, 3), None)  # type: ignore[arg-type]
    s = p.snapshot(None)  # type: ignore[arg-type]
    assert s["phase"] == 5 and s["elapsed_s"] is None
    q = AsrProgress(None)  # type: ignore[arg-type]
    assert q.snapshot(5.0)["elapsed_s"] is None


# ── parse_eta (N8) ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "text,secs",
    [
        ("7:18", 438),
        ("07:18", 438),
        ("1:02:03", 3723),
        ("0:00", 0),
        ("30:47", 1847),
        (" 7:18 ", 438),
        ("--", None),
        ("", None),
        ("7:60", None),
        ("1:2:03", None),
        ("123:45", None),
        ("7", None),
        ("7:18:", None),
        ("-7:18", None),
        (None, None),
        (438, None),
    ],
)
def test_parse_eta(text, secs):
    assert parse_eta(text) == secs


# ── FilmTracker ───────────────────────────────────────────────────────────
NO = LiveFacts()


def _t(steps=JASNA_STEPS, *films: str, initial=None) -> FilmTracker:
    t = FilmTracker(steps)
    for f in films:
        t.add(f, dict(initial or {}))
    return t


def _states(t: FilmTracker, film: str) -> dict[str, str]:
    rec = t.record(film)
    assert rec is not None
    return {k: v["state"] for k, v in rec["steps"].items()}


def test_step_constants():
    assert (RESTORE, ASR, TRANSLATE) == ("restore", "asr", "translate")
    assert JASNA_STEPS == (RESTORE, ASR, TRANSLATE)
    assert AVSUBS_STEPS == (ASR, TRANSLATE)
    assert (MAX_ROWS, NAME_CHARS) == (12, 200)


@pytest.mark.parametrize(
    "steps,initial,expected",
    [
        # Jasna full pending: all pending.
        (
            JASNA_STEPS,
            {},
            {"restore": "pending", "asr": "pending", "translate": "pending"},
        ),
        # Jasna pending translate_only: an existing .ja.srt → asr done.
        (
            JASNA_STEPS,
            {"asr": "done"},
            {"restore": "pending", "asr": "done", "translate": "pending"},
        ),
        # Jasna subs_only: restore done (pre-existing), asr per kind.
        (
            JASNA_STEPS,
            {"restore": "done"},
            {"restore": "done", "asr": "pending", "translate": "pending"},
        ),
        (
            JASNA_STEPS,
            {"restore": "done", "asr": "done"},
            {"restore": "done", "asr": "done", "translate": "pending"},
        ),
        # Jasna kind none (subtitles present) / planning failure (M2/R2).
        (
            JASNA_STEPS,
            {"asr": "skipped", "translate": "skipped"},
            {"restore": "pending", "asr": "skipped", "translate": "skipped"},
        ),
        # avsubs full / translate_only.
        (AVSUBS_STEPS, {}, {"asr": "pending", "translate": "pending"}),
        (AVSUBS_STEPS, {"asr": "done"}, {"asr": "done", "translate": "pending"}),
    ],
)
def test_initial_states(steps, initial, expected):
    t = _t(steps, "a", initial=initial)
    assert _states(t, "a") == expected
    rec = t.record("a")
    assert all(v["duration_s"] is None for v in rec["steps"].values())
    assert t.row_status("a", NO) == "pending"


def test_sticky_terminal_states_and_marks_on_terminal_steps_ignored():
    t = _t(JASNA_STEPS, "a")
    t.start("a", RESTORE, 5.0)
    t.finish("a", RESTORE, "done", 10.0)
    t.finish("a", RESTORE, "failed", 20.0)
    t.start("a", RESTORE, 30.0)
    t.activate("a", RESTORE, 30.0)
    r = t.record("a")["steps"][RESTORE]
    assert (r["state"], r["started_at"], r["activated_at"], r["ended_at"]) == (
        "done",
        5.0,
        None,
        10.0,
    )
    t.settle_subs("a", "failed", 40.0)
    t.settle_subs("a", "skipped", 50.0)
    t.finish("a", TRANSLATE, "done", 60.0)
    t.settle_subs("a", "completed", 70.0)
    assert _states(t, "a") == {
        "restore": "done",
        "asr": "failed",
        "translate": "skipped",
    }
    assert t.row_status("a", NO) == "failed"


def test_start_stamp_kept_across_retries_and_duration():
    t = _t(JASNA_STEPS, "a", "b")
    t.start("a", RESTORE, 10.0)
    t.start("a", RESTORE, 50.0)  # a restore retry keeps one duration
    t.finish("a", RESTORE, "done", 100.0)
    assert t.record("a")["steps"][RESTORE]["duration_s"] == 90
    t.start("b", RESTORE, 10.0)
    t.activate("b", RESTORE, 20.0)
    t.activate("b", RESTORE, 60.0)  # keeps the first
    t.finish("b", RESTORE, "done", 100.0)
    assert t.record("b")["steps"][RESTORE]["duration_s"] == 80


def test_translate_duration_only_from_the_first_derived_active_poll():
    t = _t(AVSUBS_STEPS, "a", "b", initial={"asr": "done"})
    for f in ("a", "b"):
        t.start(f, TRANSLATE, 100.0)  # submitted = queued, not running
    t.finish("a", TRANSLATE, "done", 200.0)  # finished between two polls
    assert t.record("a")["steps"][TRANSLATE]["duration_s"] is None
    t.observe(LiveFacts(active={TRANSLATE: "b"}), 150.0)
    t.observe(LiveFacts(active={TRANSLATE: "b"}), 170.0)
    t.finish("b", TRANSLATE, "done", 200.0)
    assert t.record("b")["steps"][TRANSLATE]["duration_s"] == 50
    assert t.row_status("a", NO) == "done" and t.row_status("b", NO) == "done"


def test_settle_subs_never_touches_a_pending_or_running_restore():
    # N1: no_exe at Start / `_disable_subs` mid-batch.
    t = _t(JASNA_STEPS, "a", "b")
    t.start("a", RESTORE, 1.0)
    for f in ("a", "b"):
        t.settle_subs(f, "skipped", 2.0)
    assert _states(t, "a") == {
        "restore": "pending",
        "asr": "skipped",
        "translate": "skipped",
    }
    live = LiveFacts(active={RESTORE: "a"})
    assert t.row_status("a", live) == "active"
    assert t.row_status("b", live) == "pending"
    t.finish("a", RESTORE, "done", 3.0)
    assert t.row_status("a", NO) == "skipped"  # R1: not done
    t.start("b", RESTORE, 4.0)
    assert t.row_status("b", LiveFacts(active={RESTORE: "b"})) == "active"
    t.finish("b", RESTORE, "done", 5.0)
    assert t.row_status("b", NO) == "skipped"


def test_restore_failure_marks_and_status():
    t = _t(JASNA_STEPS, "a")
    t.start("a", RESTORE, 1.0)
    t.finish("a", RESTORE, "failed", 2.0)
    t.settle_subs("a", "skipped", 2.0)
    assert _states(t, "a") == {
        "restore": "failed",
        "asr": "skipped",
        "translate": "skipped",
    }
    assert t.row_status("a", NO) == "failed"


def test_asr_failure_is_asr_failed_and_translate_skipped():
    t = _t(JASNA_STEPS, "a", initial={"restore": "done"})
    t.start("a", ASR, 1.0)
    t.settle_subs("a", "failed", 9.0)
    assert _states(t, "a") == {
        "restore": "done",
        "asr": "failed",
        "translate": "skipped",
    }
    assert t.record("a")["steps"][ASR]["duration_s"] == 8
    assert t.row_status("a", NO) == "failed"


def test_translate_failure_after_asr_done():
    t = _t(AVSUBS_STEPS, "a")
    t.start("a", ASR, 1.0)
    t.finish("a", ASR, "done", 2.0)
    t.start("a", TRANSLATE, 2.0)
    t.settle_subs("a", "failed", 3.0)
    assert _states(t, "a") == {"asr": "done", "translate": "failed"}
    assert t.row_status("a", NO) == "failed"


def test_completed_via_finish_and_via_settle_subs():
    t = _t(JASNA_STEPS, "a", "b", initial={"restore": "done"})
    t.finish("a", ASR, "done", 1.0)
    t.finish("a", TRANSLATE, "done", 2.0)
    t.settle_subs("b", "completed", 2.0)
    for f in ("a", "b"):
        assert _states(t, f) == {"restore": "done", "asr": "done", "translate": "done"}
        assert t.record(f)["outcome"] == "completed"
        assert t.row_status(f, NO) == "done"


def test_translate_only_film_is_pending_until_submitted_then_queued():
    # N2: queued only after start(translate).
    t = _t(JASNA_STEPS, "a", initial={"restore": "done", "asr": "done"})
    assert t.row_status("a", NO) == "pending"
    t.start("a", TRANSLATE, 1.0)
    assert t.row_status("a", NO) == "queued"
    assert t.row_status("a", LiveFacts(active={TRANSLATE: "a"})) == "active"
    assert t.row_status("a", LiveFacts(active={TRANSLATE: "zzz"})) == "queued"


def test_kind_none_and_planning_failure_are_done_once_restored():
    t = _t(JASNA_STEPS, "a", initial={"asr": "skipped", "translate": "skipped"})
    assert t.row_status("a", NO) == "pending"
    t.start("a", RESTORE, 1.0)
    assert t.row_status("a", LiveFacts(active={RESTORE: "a"})) == "active"
    t.finish("a", RESTORE, "done", 5.0)
    assert t.row_status("a", NO) == "done"
    assert t.record("a")["outcome"] is None


def test_avsubs_abort_after_asr_done_is_skipped_not_done():
    # Round 4 (R1): asr done + translate skipped (abort / no key / disable).
    t = _t(AVSUBS_STEPS, "a", initial={"asr": "done"})
    t.settle_subs("a", "skipped", 1.0)
    assert _states(t, "a") == {"asr": "done", "translate": "skipped"}
    assert t.row_status("a", NO) == "skipped"


def _mix(steps, initial, marks):
    t = _t(steps, "a", initial=initial)
    for name, *args in marks:
        getattr(t, name)("a", *args)
    return t


R = RESTORE
ROW_CASES = [
    # (1) restore failed → failed, whatever the subtitle steps say.
    (
        JASNA_STEPS,
        {},
        [("finish", R, "failed", 1.0), ("settle_subs", "skipped", 1.0)],
        NO,
        "failed",
    ),
    # (2) restore not terminal → its derived state even with the job settled early.
    (JASNA_STEPS, {}, [("settle_subs", "skipped", 1.0)], NO, "pending"),
    (
        JASNA_STEPS,
        {},
        [("settle_subs", "skipped", 1.0)],
        LiveFacts(active={R: "a"}),
        "active",
    ),
    (JASNA_STEPS, {}, [], LiveFacts(waiting=("a", R)), "waiting_gpu"),
    (JASNA_STEPS, {}, [], LiveFacts(active={ASR: "a"}), "pending"),  # restore first
    # (3) the settled job's outcome.
    (JASNA_STEPS, {"restore": "done"}, [("settle_subs", "completed", 1.0)], NO, "done"),
    (JASNA_STEPS, {"restore": "done"}, [("settle_subs", "failed", 1.0)], NO, "failed"),
    (
        JASNA_STEPS,
        {"restore": "done"},
        [("settle_subs", "skipped", 1.0)],
        NO,
        "skipped",
    ),
    (
        JASNA_STEPS,
        {"restore": "done", "asr": "done"},
        [("settle_subs", "skipped", 1.0)],
        NO,
        "skipped",
    ),
    # (4) no job with the restore done → done.
    (
        JASNA_STEPS,
        {"restore": "done", "asr": "skipped", "translate": "skipped"},
        [],
        NO,
        "done",
    ),
    # (5) the first non-terminal step's derived state.
    (JASNA_STEPS, {"restore": "done"}, [], NO, "pending"),
    (JASNA_STEPS, {"restore": "done"}, [], LiveFacts(active={ASR: "a"}), "active"),
    (
        JASNA_STEPS,
        {"restore": "done"},
        [],
        LiveFacts(waiting=("a", ASR)),
        "waiting_gpu",
    ),
    (
        JASNA_STEPS,
        {"restore": "done", "asr": "done"},
        [("start", TRANSLATE, 1.0)],
        NO,
        "queued",
    ),
    (
        JASNA_STEPS,
        {"restore": "done", "asr": "done"},
        [("start", TRANSLATE, 1.0)],
        LiveFacts(active={TRANSLATE: "a"}),
        "active",
    ),
    (AVSUBS_STEPS, {}, [], NO, "pending"),
    (AVSUBS_STEPS, {}, [], LiveFacts(active={ASR: "a"}), "active"),
    (AVSUBS_STEPS, {}, [], LiveFacts(waiting=("a", ASR)), "waiting_gpu"),
    (AVSUBS_STEPS, {}, [("settle_subs", "failed", 1.0)], NO, "failed"),
    (AVSUBS_STEPS, {}, [("settle_subs", "skipped", 1.0)], NO, "skipped"),
    (AVSUBS_STEPS, {"asr": "done"}, [("finish", TRANSLATE, "done", 1.0)], NO, "done"),
    # A stored terminal state wins over any live fact (N1 precedence).
    (
        AVSUBS_STEPS,
        {"asr": "done"},
        [("finish", TRANSLATE, "done", 1.0)],
        LiveFacts(active={TRANSLATE: "a"}),
        "done",
    ),
    (
        JASNA_STEPS,
        {"restore": "done"},
        [],
        LiveFacts(active={R: "a"}, waiting=("a", R)),
        "pending",
    ),
]


@pytest.mark.parametrize("steps,initial,marks,live,expected", ROW_CASES)
def test_row_status_for_each_mix(steps, initial, marks, live, expected):
    assert _mix(steps, initial, marks).row_status("a", live) == expected


def test_statuses_cover_every_film_in_plan_order():
    t = _t(AVSUBS_STEPS, "a", "b", "c")
    t.settle_subs("a", "failed", 1.0)
    t.settle_subs("b", "skipped", 1.0)
    got = t.statuses(LiveFacts(active={ASR: "c"}))
    assert list(got.items()) == [("a", "failed"), ("b", "skipped"), ("c", "active")]
    got["a"] = "done"
    assert t.statuses(NO)["a"] == "failed"  # fresh


def test_focus_priority():
    t = _t(JASNA_STEPS, "a", "b", "c", "d", "e")
    t.finish("a", RESTORE, "done", 1.0)
    t.settle_subs("a", "completed", 2.0)
    t.finish("b", RESTORE, "done", 3.0)
    t.finish("b", ASR, "done", 4.0)
    t.start("b", TRANSLATE, 4.0)
    live = LiveFacts(active={TRANSLATE: "b", RESTORE: "c"}, waiting=("d", RESTORE))
    assert t.focus(live) == "c"  # the GPU child
    live = LiveFacts(active={TRANSLATE: "b"}, waiting=("d", RESTORE))
    assert t.focus(live) == "d"  # then the GPU wait
    assert t.focus(LiveFacts(active={TRANSLATE: "b"})) == "b"  # then translating
    assert t.focus(NO) == "b"  # then the next non-terminal film (b is queued)
    t2 = _t(AVSUBS_STEPS, "x", "y", "z")
    t2.settle_subs("y", "completed", 5.0)
    t2.settle_subs("x", "failed", 9.0)
    t2.settle_subs("z", "skipped", 7.0)
    assert t2.focus(NO) == "x"  # the last finished (by time)
    assert FilmTracker(AVSUBS_STEPS).focus(NO) is None


def test_observe_waited_s_grows_resets_and_follows_the_film():
    t = _t(JASNA_STEPS, "a", "b")
    w = LiveFacts(waiting=("a", RESTORE), holder="AV")
    t.observe(w, 100.0)
    t.observe(w, 130.0)
    step = t.steps("a", w, 160.0)[0]
    assert step == {
        "key": RESTORE,
        "state": "waiting_gpu",
        "holder": "AV",
        "waited_s": 60,
    }
    t.observe(NO, 170.0)  # the wait ended: the stamp is dropped
    assert "waited_s" not in t.steps("a", w, 171.0)[0]
    t.observe(w, 200.0)
    assert t.steps("a", w, 210.0)[0]["waited_s"] == 10
    wb = LiveFacts(waiting=("b", RESTORE), holder="")
    t.observe(wb, 220.0)
    assert t.steps("b", wb, 225.0)[0] == {
        "key": RESTORE,
        "state": "waiting_gpu",
        "holder": "",
        "waited_s": 5,
    }


def test_observe_ignores_a_wait_on_a_terminal_or_active_step():
    t = _t(JASNA_STEPS, "a", initial={"restore": "done"})
    live = LiveFacts(waiting=("a", RESTORE), holder="X")
    t.observe(live, 1.0)
    assert t.steps("a", live, 5.0)[0] == {"key": RESTORE, "state": "done"}
    t.start("a", ASR, 1.0)
    live = LiveFacts(active={ASR: "a"}, waiting=("a", ASR))
    t.observe(live, 2.0)
    assert t.steps("a", live, 5.0)[1] == {"key": ASR, "state": "active"}


def test_steps_list_numbers_only_on_the_active_step():
    t = _t(JASNA_STEPS, "a")
    t.start("a", RESTORE, 0.0)
    t.finish("a", RESTORE, "done", 3400.0)
    t.start("a", ASR, 3400.0)
    nums = {"phase": 5, "phase_n": 8, "scene": 3, "scenes": 10, "percent": 40}
    live = LiveFacts(
        active={ASR: "a"},
        numbers={
            ASR: {**nums, "key": "evil", "state": "evil"},
            TRANSLATE: {"percent": 99},
        },
    )
    assert t.steps("a", live, 3500.0) == [
        {"key": RESTORE, "state": "done", "duration_s": 3400},
        {"key": ASR, "state": "active", **nums},
        {"key": TRANSLATE, "state": "pending"},
    ]
    assert t.steps("zzz", live, 1.0) == []


def _backlog() -> FilmTracker:
    """30 queued translate_only films sorting BEFORE the ASR film (N4)."""
    t = FilmTracker(AVSUBS_STEPS)
    for i in range(30):
        t.add(f"f{i:02d}", {"asr": "done"})
    for i in range(30, 40):
        t.add(f"f{i:02d}", {})
    for i in range(30):
        t.start(f"f{i:02d}", TRANSLATE, float(i))
    t.start("f30", ASR, 100.0)
    return t


def test_rows_hard_cap_keeps_the_focus_film_and_plan_order():
    t = _backlog()
    live = LiveFacts(active={ASR: "f30"}, numbers={ASR: {"percent": 43, "eta_s": 360}})
    focus = t.focus(live)
    assert focus == "f30"
    rows, more = t.rows(live, focus)
    names = [r["name"] for r in rows]
    assert len(rows) == MAX_ROWS and more == 40 - MAX_ROWS
    assert "f30" in names
    assert names == sorted(names)  # plan order
    assert names == [f"f{i:02d}" for i in range(11)] + ["f30"]  # nearest queued first
    f30 = rows[-1]
    assert f30 == {
        "name": "f30",
        "steps": {ASR: "active", TRANSLATE: "pending"},
        "status": "active",
        "percent": 43,
        "eta_s": 360,
        "duration_s": None,
    }
    assert rows[0]["status"] == "queued" and rows[0]["percent"] is None


def test_rows_priority_focus_active_waiting_queued_finished_pending():
    t = FilmTracker(JASNA_STEPS)
    for i in range(20):
        t.add(f"f{i:02d}", {})
    for i in range(6):  # six finished films, f05 the most recent
        t.start(f"f{i:02d}", RESTORE, i * 100.0)
        t.finish(f"f{i:02d}", RESTORE, "done", i * 100.0 + 60)
        t.settle_subs(f"f{i:02d}", "completed", i * 100.0 + 90)
    t.finish("f06", RESTORE, "done", 700.0)
    t.finish("f06", ASR, "done", 710.0)
    t.start("f06", TRANSLATE, 710.0)
    t.start("f07", RESTORE, 710.0)
    live = LiveFacts(active={TRANSLATE: "f06", RESTORE: "f07"})
    focus = t.focus(live)
    assert focus == "f07"
    rows, more = t.rows(live, focus)
    names = [r["name"] for r in rows]
    # focus f07, active f06, last 3 finished f05/f04/f03, then pending f08…
    assert names == ["f03", "f04", "f05", "f06", "f07"] + [
        f"f{i:02d}" for i in range(8, 15)
    ]
    assert more == 8
    done = rows[0]
    assert done["status"] == "done" and done["duration_s"] == 60  # the step durations


def test_rows_waiting_film_is_picked_before_queued_and_finished():
    t = FilmTracker(AVSUBS_STEPS)
    for i in range(14):
        t.add(f"f{i:02d}", {"asr": "done"} if i < 12 else {})
    for i in range(12):
        t.start(f"f{i:02d}", TRANSLATE, float(i))
    live = LiveFacts(active={TRANSLATE: "f00"}, waiting=("f13", ASR), holder="Jasna")
    focus = t.focus(live)
    assert focus == "f13"
    rows, more = t.rows(live, focus)
    names = [r["name"] for r in rows]
    assert names == [f"f{i:02d}" for i in range(11)] + ["f13"]
    assert more == 2
    assert rows[-1]["status"] == "waiting_gpu"


def _capped(queued: bool) -> FilmTracker:
    """4 finished films (d3 the most recent), the ASR focus film, then 11
    films queued for translation (`queued`) or still pending."""
    t = FilmTracker(AVSUBS_STEPS)
    for i in range(4):
        t.add(f"d{i}", {})
        t.settle_subs(f"d{i}", "completed", 10.0 * (i + 1))
    t.add("focus", {})
    t.start("focus", ASR, 50.0)
    for i in range(11):
        t.add(f"q{i:02d}", {"asr": "done"} if queued else {})
        if queued:
            t.start(f"q{i:02d}", TRANSLATE, 60.0 + i)
    return t


def test_rows_queued_films_beat_the_last_finished_under_the_cap():
    assert FINISHED_ROWS == 3
    live = LiveFacts(active={ASR: "focus"})
    rows, more = _capped(queued=True).rows(live, "focus")
    assert len(rows) == MAX_ROWS and more == 4
    assert {r["status"] for r in rows} == {"active", "queued"}  # no finished row
    # With pending films instead, the FINISHED_ROWS most recent finished ones
    # are picked before them.
    rows, more = _capped(queued=False).rows(live, "focus")
    finished = [r["name"] for r in rows if r["status"] == "done"]
    assert finished == ["d1", "d2", "d3"] and len(finished) == FINISHED_ROWS
    assert len(rows) == MAX_ROWS and more == 4


def test_rows_small_batch_lists_everything_and_unknown_focus_is_ignored():
    t = _t(AVSUBS_STEPS, "a", "b", "c")
    rows, more = t.rows(NO, "not-a-film")
    assert [r["name"] for r in rows] == ["a", "b", "c"] and more == 0


def test_rows_and_records_are_fresh_objects():
    t = _backlog()
    live = LiveFacts(active={ASR: "f30"})
    rows, _ = t.rows(live, "f30")
    rows[0]["steps"][TRANSLATE] = "done"
    rows[0]["status"] = "done"
    again, _ = t.rows(live, "f30")
    assert again[0]["status"] == "queued" and again[0]["steps"][TRANSLATE] == "queued"
    rec = t.record("f00")
    rec["steps"][TRANSLATE]["state"] = "done"
    assert t.record("f00")["steps"][TRANSLATE]["state"] == "pending"
    steps = t.steps("f30", live, 1.0)
    steps[0]["state"] = "done"
    assert t.steps("f30", live, 1.0)[0]["state"] == "active"
    view = t.view(live, 1.0)
    view["films"].clear()
    assert t.view(live, 1.0)["films"]


def test_name_is_bounded():
    long = "片" * 300 + ".mp4"
    t = _t(AVSUBS_STEPS, long)
    rows, _ = t.rows(NO, long)
    assert len(rows[0]["name"]) <= NAME_CHARS
    assert rows[0]["name"].endswith(".mp4")
    assert len(t.view(NO, 1.0)["film"]) <= NAME_CHARS


def test_holder_is_capped_by_the_tracker():
    t = _t(JASNA_STEPS, "a")
    live = LiveFacts(waiting=("a", RESTORE), holder="Other " + "x" * 500)
    t.observe(live, 1.0)
    holder = t.steps("a", live, 2.0)[0]["holder"]
    assert len(holder) <= NAME_CHARS and holder.startswith("Other ")
    assert t.view(live, 3.0)["steps"][0]["holder"] == holder


def test_view_bundles_focus_steps_and_rows():
    t = _t(JASNA_STEPS, "a", "b")
    t.start("a", RESTORE, 0.0)
    live = LiveFacts(
        active={RESTORE: "a"}, numbers={RESTORE: {"percent": 57, "eta_s": 438}}
    )
    v = t.view(live, 10.0)
    assert list(v) == ["film", "steps", "films", "films_more"]
    assert v["film"] == "a"
    assert v["steps"][0] == {
        "key": RESTORE,
        "state": "active",
        "percent": 57,
        "eta_s": 438,
    }
    assert [r["name"] for r in v["films"]] == ["a", "b"] and v["films_more"] == 0
    assert t.record("a")["steps"][RESTORE]["activated_at"] == 10.0  # observed
    json.dumps(v)  # wire-safe
    assert FilmTracker(JASNA_STEPS).view(live, 1.0) == {}


def test_unknown_film_step_and_garbage_never_raise():
    t = _t(JASNA_STEPS, "a")
    bad_calls = [
        ("add", (None, {})),
        ("add", (["x"], {})),
        ("add", ("b", None)),
        ("add", ("c", {"bogus": "done", "asr": "weird", 5: "done", "translate": 7})),
        ("add", ("a", {"restore": "done"})),  # a second add never resets a film
        ("start", ("zzz", ASR, 1.0)),
        ("start", ("a", "bogus", 1.0)),
        ("start", ("a", None, 1.0)),
        ("start", (["a"], ASR, 1.0)),
        ("start", ("a", RESTORE, "noon")),
        ("activate", ("a", RESTORE, float("nan"))),
        ("activate", ("zzz", RESTORE, 1.0)),
        ("finish", ("a", ASR, "bogus", 1.0)),
        ("finish", ("a", ASR, None, 1.0)),
        ("finish", ("zzz", ASR, "done", 1.0)),
        ("settle_subs", ("a", "whatever", 1.0)),
        ("settle_subs", ("zzz", "failed", 1.0)),
        ("settle_subs", ({}, "failed", 1.0)),
        ("record", ("zzz",)),
        ("record", ({},)),
        ("observe", (None, 1.0)),
        ("observe", ("garbage", None)),
        ("observe", (LiveFacts(active={ASR: None, "bogus": "a", 3: "a"}), 1.0)),  # type: ignore[dict-item]
        ("observe", (LiveFacts(waiting=("a",)), 1.0)),  # type: ignore[arg-type]
        ("observe", (LiveFacts(waiting=(["a"], RESTORE)), 1.0)),  # type: ignore[arg-type]
        ("row_status", ("zzz", NO)),
        ("row_status", ("a", None)),
        ("statuses", (None,)),
        ("focus", ("junk",)),
        ("steps", ("a", LiveFacts(numbers={RESTORE: None}), None)),  # type: ignore[dict-item]
        ("steps", ({}, NO, 1.0)),
        ("rows", (None, None)),
        ("rows", (NO, ["a"])),
        ("view", (None, None)),
        ("view", (LiveFacts(active=None, numbers=None), 1.0)),  # type: ignore[arg-type]
    ]
    for name, args in bad_calls:
        getattr(t, name)(*args)
    assert _states(t, "a") == {
        "restore": "pending",
        "asr": "pending",
        "translate": "pending",
    }
    assert t.record("zzz") is None and t.row_status("zzz", NO) is None
    assert t.record("b") is not None and _states(t, "b")["asr"] == "pending"
    assert _states(t, "c") == {
        "restore": "pending",
        "asr": "pending",
        "translate": "pending",
    }
    assert t.record("a")["steps"][RESTORE]["started"] is True  # the flag, no stamp
    assert t.record("a")["steps"][RESTORE]["started_at"] is None


def test_tracker_keeps_only_known_steps_in_canonical_order():
    t = FilmTracker(("bogus", TRANSLATE, ASR, ASR, None))  # type: ignore[arg-type]
    t.add("a", {})
    assert list(t.record("a")["steps"]) == [ASR, TRANSLATE]
    assert FilmTracker(None).view(NO, 1.0) == {}  # type: ignore[arg-type]


def test_restore_done_is_a_narrow_total_accessor():
    t = _t(JASNA_STEPS, "a", "b", "p")
    t.add("c", {"restore": "done"})  # restored at Start
    t.finish("a", RESTORE, "done", 5.0)
    t.finish("b", RESTORE, "failed", 5.0)
    assert [t.restore_done(f) for f in ("a", "b", "p", "c")] == [
        True,
        False,
        False,
        True,
    ]
    assert t.restore_done("zzz") is False
    assert t.restore_done(None) is False  # type: ignore[arg-type]
    assert _t(AVSUBS_STEPS, "a").restore_done("a") is False  # no restore step


def test_film_page_plan_order_extras_and_status_parity():
    t = _t(AVSUBS_STEPS, "PQRS-002.mp4", "ABC-001.mp4", "LMNO-003.mp4")
    t.start("PQRS-002.mp4", ASR, 1.0)
    t.settle_subs("PQRS-002.mp4", "completed", 8.0)
    t.start("LMNO-003.mp4", TRANSLATE, 9.0)
    view = t.view(LiveFacts(active={ASR: "ABC-001.mp4"}), 10.0)
    t.set_extras([("DEFG-004.mp4", "collision"), ("HIJK-005.mp4", "pre_done")])
    page = t.page(None, 10)
    assert set(page) == {
        "run",
        "total",
        "size",
        "page",
        "pages",
        "focus",
        "focus_page",
        "films",
    }
    assert page["films"][:3] == view["films"]
    assert [r["name"] for r in page["films"]] == [
        "PQRS-002.mp4",
        "ABC-001.mp4",
        "LMNO-003.mp4",
        "DEFG-004.mp4",
        "HIJK-005.mp4",
    ]
    assert page["total"] == 5
    for row, status in zip(page["films"][3:], ("collision", "pre_done")):
        assert row == dict(
            name=row["name"],
            steps={},
            status=status,
            percent=None,
            eta_s=None,
            duration_s=None,
        )
    assert t.view(NO, 11.0)["films_more"] == 0  # extras never enter status


def test_film_page_focus_last_live_stamps_and_fresh_objects(monkeypatch):
    names = [f"LMNO-{i:03}.mp4" for i in range(25)]
    names[14] += "x" * 220
    t = _t(AVSUBS_STEPS, *names)
    assert t.page(None, 10)["films"][0]["status"] == "pending"
    nums = {"percent": 37, "eta_s": 123}
    t.view(LiveFacts(active={ASR: names[14]}, numbers={ASR: nums}), 20.0)
    nums["percent"] = 98
    records = [t.record(n) for n in names]
    wait = t._wait
    monkeypatch.setattr(t, "observe", lambda *a: pytest.fail("page observed"))
    page = t.page(None, 10)
    assert (page["page"], page["focus_page"], page["pages"]) == (2, 2, 3)
    assert page["focus"] == page["films"][4]["name"]
    assert len(page["focus"]) <= NAME_CHARS
    assert (page["films"][4]["percent"], page["films"][4]["eta_s"]) == (37, 123)
    page["films"][4]["steps"][ASR] = "failed"
    page["films"].clear()
    assert t.page(None, 10)["films"][4]["steps"][ASR] == "active"
    assert [t.record(n) for n in names] == records and t._wait == wait
    t.settle_subs(names[14], "completed", 30.0)
    assert t.page(2, 10)["films"][4]["status"] == "done"


@pytest.mark.parametrize(
    "page, expected",
    [
        (None, 2),
        (999, 3),
        (0, 1),
        (-1, 1),
        (True, 1),
        (False, 1),
        ("2", 1),
        (2.5, 1),
        ([], 1),
        ({}, 1),
    ],
)
def test_film_page_bad_page_and_clamp(page, expected):
    t = _t(AVSUBS_STEPS, *(f"ABC-{i:03}.mp4" for i in range(25)))
    t.view(LiveFacts(active={ASR: "ABC-014.mp4"}), 1.0)
    assert t.page(page, 10)["page"] == expected


@pytest.mark.parametrize(
    "size, expected",
    [
        (None, 10),
        (True, 10),
        (False, 10),
        ("2", 10),
        (2.5, 10),
        ([], 10),
        ({}, 10),
        (0, 1),
        (-9, 1),
        (51, 50),
        (2, 2),
    ],
)
def test_film_page_bad_size_and_clamp(size, expected):
    t = _t(AVSUBS_STEPS, *(f"ABC-{i:03}.mp4" for i in range(60)))
    page = t.page(None, size)
    assert page["size"] == len(page["films"]) == expected


def test_film_page_empty_extras_only_and_run_isolation():
    t, other = FilmTracker(AVSUBS_STEPS), FilmTracker(AVSUBS_STEPS)
    page = t.page(999, 10)
    assert page == dict(
        run=page["run"],
        total=0,
        size=10,
        page=1,
        pages=1,
        focus=None,
        focus_page=None,
        films=[],
    )
    assert isinstance(page["run"], str) and page["run"]
    assert other.page(None, 10)["run"] != page["run"]
    extras = [("LMNO-001.mp4" + "x" * 220, "pre_done"), ("ABC-002.mp4", "collision")]
    t.set_extras(extras)
    extras.clear()
    t.set_extras([("HIJK-003.mp4", "pre_done")])  # write once, even per tracker
    rows = t.page(None, 1)
    assert (rows["total"], rows["pages"], rows["focus"], rows["focus_page"]) == (
        2,
        2,
        None,
        None,
    )
    assert len(rows["films"][0]["name"]) <= NAME_CHARS
    rows["films"][0]["steps"]["asr"] = "done"
    assert t.page(None, 1)["films"][0]["steps"] == {}
    assert t.page(None, 10)["run"] == page["run"]
    assert other.page(None, 10)["total"] == 0
    other.set_extras([])
    other.set_extras([("ABC-002.mp4", "pre_done")])
    assert other.page(None, 10)["total"] == 0


def test_film_page_builds_only_window_with_20000_extras(monkeypatch):
    from taskpaw_v3.monitors.subs import progress

    t = FilmTracker(AVSUBS_STEPS)
    t.set_extras([(f"LMNO-{i:05}.mp4", "pre_done") for i in range(20000)])
    built = []
    original = progress.bounded

    def bounded(name, size):
        built.append(name)
        return original(name, size)

    monkeypatch.setattr(progress, "bounded", bounded)
    page = t.page(1700, 10)
    assert page["total"] == 20000 and page["pages"] == 2000
    assert built == [f"LMNO-{i:05}.mp4" for i in range(16990, 17000)]


def test_film_page_1000_tracked_slice_first(monkeypatch):
    names = [f"PQRS-{i:04}.mp4" for i in reversed(range(1000))]
    t = _t(AVSUBS_STEPS, *names)
    built = []
    original = t._row

    def row(film, *args):
        built.append(film.name)
        return original(film, *args)

    monkeypatch.setattr(t, "_row", row)
    page = t.page(100, 10)
    assert page["total"] == 1000 and page["pages"] == 100
    assert built == names[990:]
    assert [r["name"] for r in page["films"]] == names[990:]
