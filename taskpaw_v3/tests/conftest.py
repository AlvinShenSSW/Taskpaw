"""Put the repo root on sys.path so `import taskpaw_v3.*` works in tests."""

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def _llm_isolation(monkeypatch):
    """Hermetic LLM settings (#178 D8, #192): the process-wide holders (primary,
    chain, failover) start at their defaults and no real TASKPAW_LLM_* value
    from the developer's shell ever reaches a test — EVERY variable with that
    prefix (case-insensitive: the fallback keys too) is stripped. Tests that
    need one set it explicitly via monkeypatch."""
    from taskpaw_v3.core.llm import LLM_ENV_PREFIX, reset_llm_settings

    for name in list(os.environ):
        if name.upper().startswith(LLM_ENV_PREFIX):
            monkeypatch.delenv(name, raising=False)
    reset_llm_settings()
    yield
    reset_llm_settings()


@pytest.fixture(autouse=True)
def _data_dir_isolation():
    """#192 C5: no test persists into a real data dir (e.g. %APPDATA%) — the
    holder is None unless a test sets a tmp dir itself."""
    from taskpaw_v3.core import datadir

    datadir.set_data_dir(None)
    yield
    datadir.set_data_dir(None)


@pytest.fixture(autouse=True)
def _task_log_isolation():
    """#196: a fresh memory-only holder; never resolve the real app data dir."""
    from taskpaw_v3.core.tasklog import set_task_log

    set_task_log(None)
    yield
    set_task_log(None)


@pytest.fixture(autouse=True)
def _gpu_lease_isolation():
    """A fresh process-wide GPU lease per test (#179, D3): no holder, waiter or
    reservation leaks between tests. It runs on the real monotonic clock; a test
    that needs to drive time calls `gpu_lease._reset_for_tests(clock=fake)`."""
    from taskpaw_v3.core import gpu_lease

    gpu_lease._reset_for_tests()
    yield
    gpu_lease._reset_for_tests()


def tasklog_rows(kind=None):
    from taskpaw_v3.core.tasklog import get_task_log

    rows = list(get_task_log()._ring)
    return [r for r in rows if kind is None or r["kind"] == kind]
