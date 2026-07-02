"""Guard: coverage tooling stays wired (#144).

These are config-shape guards (no toml parser — `tomllib` is 3.11+, CI runs 3.10),
so they read pyproject.toml as text. They keep pytest-cov installed and the
V3-scoped coverage config from silently disappearing.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_pytest_cov_available() -> None:
    """pytest-cov must be in the dev group so `pytest --cov` works in CI."""
    assert (
        importlib.util.find_spec("pytest_cov") is not None
    ), "pytest-cov missing — add it to the [dependency-groups] dev list"


def test_coverage_scoped_to_v3_only() -> None:
    """Coverage measures the V3 monorepo, not the frozen V2 scripts (#144)."""
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.coverage.run]" in text, "missing [tool.coverage.run] config"
    run_block = text.split("[tool.coverage.run]", 1)[1]
    assert 'source = ["taskpaw_v3"]' in run_block, "coverage source must be taskpaw_v3"
    # Frozen V2 entry points must not be in the coverage source (no V2 churn pressure).
    for v2 in ("taskpaw.py", "taskpaw_hub.py", "macsubs.py"):
        assert f'"{v2}"' not in run_block.split("[tool.coverage.report]", 1)[0], (
            f"{v2} (frozen V2) must not be a coverage source"
        )
