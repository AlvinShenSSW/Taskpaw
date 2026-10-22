"""Subtitle content judgement (T04-P1 §3.4)."""

from __future__ import annotations

import pytest

from taskpaw_v3.monitors.subs import srt
from taskpaw_v3.workflow.subtitle_judge import (
    Judgement,
    content_language,
    decode,
    judge,
)


def _srt(*cues: str) -> str:
    """One SRT document; each argument is one cue's text (may be multi-line)."""
    return "".join(
        f"{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},900\n{text}\n\n"
        for i, text in enumerate(cues, start=1)
    )


SIMPLIFIED = _srt(
    "你今天怎么这么晚才回来",
    "我在公司加班到现在",
    "那你吃过晚饭了吗",
    "还没有，我们一起去吃吧",
)
TRADITIONAL = _srt(
    "你今天怎麼這麼晚才回來",
    "我在公司加班到現在",
    "那你吃過晚飯了嗎",
    "還沒有，我們一起去吃吧",
)
BILINGUAL = _srt(
    "今日はどうしてこんなに遅いの\n你今天怎么这么晚",
    "会社で残業していたんだ\n我在公司加班",
    "晩ご飯はもう食べたの\n你吃过晚饭了吗",
    "まだだよ、一緒に行こう\n还没有，一起去吧",
)
BILINGUAL_SHORT_ZH = _srt(
    "だめ、やめて\n不要",
    "気持ちいい\n好舒服",
    "ちょっと待って\n等一下",
    "どうしてここにいるの\n你为什么在这里",
)
KANJI_HEAVY_JA = _srt(
    "本日の会議資料を確認してください",
    "営業部長は明日東京本社へ出張します",
    "新製品の販売計画について説明します",
    "来月の予算会議は延期になりました",
)
# D23 negative case: short Han-only cues (本当/先生/社長/大丈夫) are valid
# Japanese, but most cues are ordinary kana dialogue, so it is not bilingual.
SHORT_KANJI_JA = _srt(
    "本当？",
    "先生！",
    "社長",
    "大丈夫",
    "ねえ、ちょっと待ってよ",
    "どうしてそんなことを言うの",
    "わたしはずっとここにいたよ",
    "もういいから、早く帰ろう",
    "ありがとうございました",
    "また明日ね、さようなら",
)
ZH_WITH_NO = _srt(
    "这家店叫做美味の家，菜很好吃",
    "我们周末一起去那里吃饭吧",
    "好啊，我请你吃他们的招牌菜",
    "听说他们的拉面也特别好吃",
)
# Chinese with a short Japanese aside: kana share falls between 0.05 and 0.2.
MOSTLY_ZH = _srt(
    "你今天怎么这么晚才回来",
    "我在公司加班到现在",
    "那你吃过晚饭了吗",
    "还没有，我们一起去吃吧",
    "おかえりなさい",
)
FEW_ZH = _srt("你好", "谢谢", "再见")
FEW_JA = _srt("はい", "いいえ")
GARBAGE = "this is not\nan srt file at all\n"


@pytest.mark.parametrize(
    ("raw", "tag", "decision"),
    [
        pytest.param(SIMPLIFIED.encode(), "zh-Hans", "reused_zh", id="simplified"),
        pytest.param(TRADITIONAL.encode(), "zh-Hant", "reused_zh", id="traditional"),
        pytest.param(SIMPLIFIED.encode("gb18030"), "zh", "reused_zh", id="gb18030"),
        pytest.param(SIMPLIFIED.encode("utf-16"), "zh", "reused_zh", id="utf16-bom"),
        pytest.param(SIMPLIFIED.encode("utf-8-sig"), "zh", "reused_zh", id="utf8-bom"),
        pytest.param(BILINGUAL.encode(), "zh", "reused_zh", id="bilingual"),
        pytest.param(
            BILINGUAL_SHORT_ZH.encode(), "zh", "reused_zh", id="bilingual-short-zh"
        ),
        pytest.param(KANJI_HEAVY_JA.encode(), "ja", "source_ja", id="kanji-ja-as-ja"),
        pytest.param(
            KANJI_HEAVY_JA.encode(), "zh", "needs_attention", id="kanji-ja-as-zh"
        ),
        pytest.param(
            SHORT_KANJI_JA.encode(), "zh", "needs_attention", id="d23-short-kanji"
        ),
        pytest.param(ZH_WITH_NO.encode(), "zh", "reused_zh", id="zh-with-no"),
        pytest.param(MOSTLY_ZH.encode(), "zh", "reused_zh", id="undetermined-zh"),
        pytest.param(MOSTLY_ZH.encode(), "ja", "run_asr", id="undetermined-ja"),
        pytest.param(FEW_ZH.encode(), "zh", "needs_attention", id="zh-insufficient"),
        pytest.param(FEW_JA.encode(), "ja", "run_asr", id="ja-insufficient"),
        pytest.param(SIMPLIFIED.encode(), "en", "run_asr", id="en-tag-zh-text"),
        pytest.param(KANJI_HEAVY_JA.encode(), "en", "run_asr", id="en-tag-ja-text"),
        pytest.param(SIMPLIFIED.encode(), "unknown", "run_asr", id="unknown-tag"),
        pytest.param(SIMPLIFIED.encode(), "zh", "reused_zh", id="zh-tag"),
        pytest.param(SIMPLIFIED.encode(), "chs", "reused_zh", id="chs-tag"),
        pytest.param(KANJI_HEAVY_JA.encode(), "jpn", "source_ja", id="jpn-tag"),
        pytest.param(KANJI_HEAVY_JA.encode(), "jp", "source_ja", id="jp-tag"),
        pytest.param(
            b"\xff\x80\xff\x80" * 8, "zh", "needs_attention", id="undecodable-zh"
        ),
        pytest.param(GARBAGE.encode(), "zh", "needs_attention", id="garbage-zh"),
        pytest.param(GARBAGE.encode(), "ja", "run_asr", id="garbage-ja"),
        pytest.param(b"", "zh", "needs_attention", id="empty-zh"),
    ],
)
def test_judge_decision(raw: bytes, tag: str, decision: str) -> None:
    result = judge(raw, tag)
    assert isinstance(result, Judgement)
    assert result.decision == decision
    if decision == "needs_attention":
        assert result.reason
    else:
        assert result.reason == ""


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (b"\xff\x80\xff\x80" * 8, "字幕无法解码"),
        (GARBAGE.encode(), "字幕不是有效的 SRT"),
        (FEW_ZH.encode(), "字幕内容太少，无法判定语言"),
        (KANJI_HEAVY_JA.encode(), "字幕标为中文但内容不是中文"),
    ],
)
def test_needs_attention_reasons(raw: bytes, reason: str) -> None:
    assert judge(raw, "zh").reason == reason


@pytest.mark.parametrize(
    ("text", "language"),
    [
        pytest.param(SIMPLIFIED, "zh", id="simplified"),
        pytest.param(TRADITIONAL, "zh", id="traditional"),
        pytest.param(ZH_WITH_NO, "zh", id="zh-with-no"),
        pytest.param(KANJI_HEAVY_JA, "ja", id="kanji-heavy-ja"),
        pytest.param(SHORT_KANJI_JA, "ja", id="short-kanji-ja"),
        # Real bilingual files are kana-heavy; only the D21 rule rescues them.
        pytest.param(BILINGUAL, "ja", id="bilingual"),
        pytest.param(MOSTLY_ZH, "undetermined", id="mostly-zh"),
        pytest.param(FEW_ZH, "insufficient", id="few-zh"),
        pytest.param(FEW_JA, "insufficient", id="few-ja"),
        pytest.param("", "insufficient", id="empty"),
    ],
)
def test_content_language(text: str, language: str) -> None:
    assert content_language(srt.parse(text)) == language


def test_judgement_records_language() -> None:
    assert judge(KANJI_HEAVY_JA.encode(), "zh").language == "ja"
    assert judge(BILINGUAL.encode(), "zh").language == "ja"
    assert judge(MOSTLY_ZH.encode(), "zh").language == "undetermined"
    assert judge(b"\xff\x80\xff\x80", "zh").language is None


def test_judgement_is_frozen() -> None:
    result = judge(SIMPLIFIED.encode(), "zh")
    with pytest.raises(AttributeError):
        result.decision = "run_asr"  # type: ignore[misc]


@pytest.mark.parametrize(
    "raw",
    [
        SIMPLIFIED.encode(),
        SIMPLIFIED.encode("utf-8-sig"),
        SIMPLIFIED.encode("utf-16"),
        b"\xfe\xff" + SIMPLIFIED.encode("utf-16-be"),
        SIMPLIFIED.encode("gb18030"),
    ],
)
def test_decode_round_trips(raw: bytes) -> None:
    assert decode(raw) == SIMPLIFIED


def test_decode_rejects_bomless_utf16_and_garbage() -> None:
    # UTF-16 is only tried behind a BOM; BOM-less UTF-16 must not decode as it.
    assert decode(SIMPLIFIED.encode("utf-16-le")) != SIMPLIFIED
    assert decode(b"\xff\x80\xff\x80") is None
