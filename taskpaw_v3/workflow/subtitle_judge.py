"""Decide what an existing SRT subtitle is good for (T04-P1 §3.4).

Pure: works on the bytes it is given and does no I/O. `judge` turns a file's
bytes and its manifest language tag into one of `reused_zh` (use it as the
Chinese subtitle), `source_ja` (use it as the Japanese source for
translation), `run_asr` (ignore it and transcribe) or `needs_attention`
(tagged Chinese but not usable as Chinese).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from taskpaw_v3.monitors.subs import srt

Decision = Literal["reused_zh", "source_ja", "run_asr", "needs_attention"]
Language = Literal["zh", "ja", "undetermined", "insufficient"]

MIN_CJK = 20
ZH_MAX_KANA_SHARE = 0.05
JA_MIN_KANA_SHARE = 0.2
BILINGUAL_MIN_HAN = 2

_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")
_ZH_TAGS = {"chs", "cht", "zho", "chi"}
_JA_TAGS = {"ja", "jp", "jpn"}


@dataclass(frozen=True)
class Judgement:
    decision: Decision
    reason: str = ""  # Chinese, operator-facing; set only for needs_attention
    language: Language | None = None  # None when the bytes never parsed


def decode(raw: bytes) -> str | None:
    """UTF-8 (BOM optional), then UTF-16 behind a BOM, then GB18030."""
    encodings = ["utf-8-sig"]
    if raw.startswith(_UTF16_BOMS):
        encodings.append("utf-16")
    encodings.append("gb18030")
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _is_han(ch: str) -> bool:
    # CJK Unified Ideographs and Extension A.
    return "一" <= ch <= "鿿" or "㐀" <= ch <= "䶿"


def _is_kana(ch: str) -> bool:
    # Hiragana, Katakana (incl. ー; excl. the ・ middle dot, which Chinese
    # subtitles also use in foreign names), Katakana Phonetic Extensions,
    # halfwidth katakana (incl. ｰ).
    return (
        ("ぁ" <= ch <= "ゟ")
        or ("゠" <= ch <= "ヿ" and ch != "・")
        or ("ㇰ" <= ch <= "ㇿ")
        or ("ｦ" <= ch <= "ﾟ")
    )


def content_language(cues: Sequence[srt.Cue]) -> Language:
    """Han/kana ratio over the whole file."""
    han = kana = 0
    for cue in cues:
        for ch in cue.text:
            han += _is_han(ch)
            kana += _is_kana(ch)
    cjk = han + kana
    if cjk < MIN_CJK:
        return "insufficient"
    share = kana / cjk
    if share <= ZH_MAX_KANA_SHARE:
        return "zh"
    if share >= JA_MIN_KANA_SHARE:
        return "ja"
    return "undetermined"


def _has_chinese_line(cue: srt.Cue) -> bool:
    return any(
        not any(_is_kana(ch) for ch in line)
        and sum(_is_han(ch) for ch in line) >= BILINGUAL_MIN_HAN
        for line in cue.text.splitlines()
    )


def _bilingual(cues: Sequence[srt.Cue]) -> bool:
    """At least half the cues carry a kana-free line of >= 2 Han (D21)."""
    return 2 * sum(_has_chinese_line(c) for c in cues) >= len(cues) > 0


def _tag_kind(tag: str) -> Literal["zh", "ja", "other"]:
    t = tag.strip().lower()
    if t.startswith("zh") or t in _ZH_TAGS:
        return "zh"
    if t in _JA_TAGS or t.startswith("ja-"):
        return "ja"
    return "other"


def _attention(reason: str, language: Language | None = None) -> Judgement:
    return Judgement("needs_attention", reason, language)


def judge(raw: bytes, tag: str) -> Judgement:
    """Decision for one SRT file given its manifest language tag (D13)."""
    kind = _tag_kind(tag)
    if kind == "other":
        return Judgement("run_asr")
    text = decode(raw)
    if text is None:
        return _attention("字幕无法解码") if kind == "zh" else Judgement("run_asr")
    try:
        cues = srt.parse(text)
    except srt.SrtError:
        if kind == "zh":
            return _attention("字幕不是有效的 SRT")
        return Judgement("run_asr")
    language = content_language(cues)
    if kind == "ja":
        decision: Decision = "source_ja" if language == "ja" else "run_asr"
        return Judgement(decision, language=language)
    if language == "zh":
        return Judgement("reused_zh", language=language)
    if language == "insufficient":
        return _attention("字幕内容太少，无法判定语言", language)
    if _bilingual(cues):
        return Judgement("reused_zh", language=language)
    return _attention("字幕标为中文但内容不是中文", language)
