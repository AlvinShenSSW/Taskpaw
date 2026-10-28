"""#359: the controller summary keeps the bounded LTX tqdm tokens."""

from __future__ import annotations

import pytest

from taskpaw_v3.agent.server import workflow_events as events
from taskpaw_v3.core.protocol import EventQueue
from taskpaw_v3.monitors.plugins.jasna_progress import parse_jasna_progress_line
from taskpaw_v3.tests.test_workflow_events import host_for

LINE = (
    "LTX 2/4 encode:  52%|█████▏    | 156/300 "
    "[00:27<00:25,  5.62frame/s, run 12:12 left]"
)
TOKENS = {
    "stage_index": 2,
    "stage_total": 4,
    "stage_name": "encode",
    "percent": 52.0,
    "percent_text": "52",
    "current": "156",
    "total": "300",
    "rate_value": "5.62",
    "rate_unit": "frame/s",
    "elapsed": "00:27",
    "eta": "00:25",
    "run_eta": "12:12",
    "mode": "restore",
}


@pytest.fixture
def observer(tmp_path):
    return events.WorkflowEvents(EventQueue("windows"), tmp_path / "seen.json")


def test_current_ltx_keeps_tqdm_tokens_without_the_raw_line():
    ltx = parse_jasna_progress_line(LINE, {})
    assert "raw_line" in ltx["ltx_progress"]
    item = events._current({"film": "ABF-394", "stage": "restore", "ltx": ltx})
    assert item["ltx"] == TOKENS


def test_summary_current_carries_the_tokens(observer, monkeypatch):
    current = {
        "film": "ABF-394",
        "stage": "restore",
        "progress": "修复 · 阶段 2/4 encode · 52% · 剩余 12:12",
        "ltx": parse_jasna_progress_line(LINE, {}),
    }
    host, _ = host_for(monkeypatch, [], current=current)
    observer.observe(host)
    summary = observer.snapshot()
    assert summary["current"]["ltx"] == TOKENS
    assert "raw_line" not in repr(summary)
