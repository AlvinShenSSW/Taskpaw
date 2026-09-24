"""Put the repo root on sys.path so `import taskpaw_v3.*` works in tests."""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_LLM_ENV_VARS = ("TASKPAW_LLM_API_KEY", "TASKPAW_LLM_API_BASE", "TASKPAW_LLM_MODEL")


@pytest.fixture(autouse=True)
def _llm_isolation(monkeypatch):
    """Hermetic LLM settings (#178, D8): the process-wide holder starts at its
    defaults and no real TASKPAW_LLM_* value from the developer's shell ever
    reaches a test — tests that need one set it explicitly via monkeypatch."""
    from taskpaw_v3.core.llm import reset_llm_settings

    for name in _LLM_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    reset_llm_settings()
    yield
    reset_llm_settings()
