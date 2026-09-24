"""Strict SRT parse/serialize (#177, monitors/subs/srt.py)."""

from __future__ import annotations

import pytest

from taskpaw_v3.monitors.subs.srt import Cue, SrtError, load, parse, serialize

# Shaped like the §12 anime-whisper run: 7 sentence-level CJK cues.
SAMPLE_12 = """1
00:00:00,000 --> 00:00:02,100
こんにちは、今日はいい天気ですね。

2
00:00:02,300 --> 00:00:04,500
明日も晴れるといいな。

3
00:00:09,200 --> 00:00:11,800
どこへ行きたいですか？

4
00:00:12,000 --> 00:00:14,600
海に行ってみたいです。

5
00:00:19,000 --> 00:00:22,400
それなら、一緒に行きましょう。

6
00:00:22,600 --> 00:00:25,900
本当？嬉しい！

7
00:00:30,400 --> 00:00:36,500
じゃあ、明後日の朝に駅で会おう。
"""


def test_parse_sample_seven_cjk_cues():
    cues = parse(SAMPLE_12)
    assert len(cues) == 7
    assert cues[0] == Cue(1, 0, 2100, "こんにちは、今日はいい天気ですね。")
    assert cues[6] == Cue(7, 30400, 36500, "じゃあ、明後日の朝に駅で会おう。")
    assert [c.index for c in cues] == list(range(1, 8))


def test_parse_crlf_and_bom():
    text = "﻿" + SAMPLE_12.replace("\n", "\r\n")
    assert parse(text) == parse(SAMPLE_12)


def test_parse_dot_millisecond_separator():
    cues = parse("1\n01:02:03.456 --> 01:02:04.000\nはい\n")
    assert cues == [Cue(1, 3723456, 3724000, "はい")]


def test_parse_multiline_text_joined_with_newline():
    cues = parse("5\n00:00:01,000 --> 00:00:02,000\n一行目\n二行目\n\n")
    assert cues == [Cue(5, 1000, 2000, "一行目\n二行目")]


@pytest.mark.parametrize("text", ["", "   ", "\n\n", "﻿", "\r\n \r\n"])
def test_parse_empty_is_empty_list(text):
    assert parse(text) == []


def test_parse_tolerates_extra_blank_lines_between_blocks():
    text = "1\n00:00:01,000 --> 00:00:02,000\na\n\n\n\n2\n00:00:03,000 --> 00:00:04,000\nb\n"
    assert [c.text for c in parse(text)] == ["a", "b"]


@pytest.mark.parametrize(
    "text",
    [
        "1\n00:00:01 --> 00:00:02\nbad timestamp\n",
        "1\n00:00:01,000 -> 00:00:02,000\nbad arrow\n",
        "1\n00:61:01,000 --> 00:62:02,000\nbad minutes\n",
        "1\n00:00:05,000 --> 00:00:04,000\nend before start\n",
        "x\n00:00:01,000 --> 00:00:02,000\nnon-integer index\n",
        "1\n",
        "garbage only\n",
    ],
)
def test_parse_is_strict(text):
    with pytest.raises(SrtError):
        parse(text)


def test_srt_error_is_a_value_error():
    assert issubclass(SrtError, ValueError)


def test_serialize_renumbers_and_round_trips():
    cues = [Cue(9, 1000, 2000, "a"), Cue(3, 2500, 3000, "b\nc")]
    out = serialize(cues)
    assert out == (
        "1\n00:00:01,000 --> 00:00:02,000\na\n\n"
        "2\n00:00:02,500 --> 00:00:03,000\nb\nc\n\n"
    )
    assert "\r" not in out
    again = parse(out)
    assert again == [Cue(1, 1000, 2000, "a"), Cue(2, 2500, 3000, "b\nc")]
    assert serialize(again) == out


def test_serialize_large_hours_and_sample_round_trip():
    assert serialize([Cue(1, 100 * 3600 * 1000 + 1, 100 * 3600 * 1000 + 2, "x")]) == (
        "1\n100:00:00,001 --> 100:00:00,002\nx\n\n"
    )
    assert parse(serialize(parse(SAMPLE_12))) == parse(SAMPLE_12)


def test_serialize_empty_is_empty_string():
    assert serialize([]) == ""


def test_load_utf8_sig(tmp_path):
    p = tmp_path / "a.srt"
    p.write_bytes("﻿".encode("utf-8") + SAMPLE_12.encode("utf-8"))
    assert load(p) == parse(SAMPLE_12)


def test_load_undecodable_bytes_is_srt_error(tmp_path):
    p = tmp_path / "bad.srt"
    p.write_bytes(b"1\n00:00:01,000 --> 00:00:02,000\n\xff\xfe\xfd\n")
    with pytest.raises(SrtError):
        load(p)
