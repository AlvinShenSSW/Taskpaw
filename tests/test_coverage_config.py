"""Guard: coverage tooling stays wired (#144).

Keeps pytest-cov installed and the V3-scoped, threshold-free coverage config from
silently drifting. Parses pyproject.toml with a real TOML parser: stdlib
``tomllib`` on 3.11+, and ``tomli`` on 3.10 (pulled in transitively by
``coverage[toml]``, which pytest-cov depends on).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

REPO_ROOT = Path(__file__).resolve().parent.parent


def _coverage_config() -> dict:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data.get("tool", {}).get("coverage", {})


def test_pytest_cov_available() -> None:
    """pytest-cov must be in the dev group so `pytest --cov` works in CI."""
    assert (
        importlib.util.find_spec("pytest_cov") is not None
    ), "pytest-cov missing — add it to the [dependency-groups] dev list"


def test_coverage_scoped_to_v3_only() -> None:
    """Coverage measures the V3 monorepo, not the frozen V2 scripts (#144)."""
    run = _coverage_config().get("run", {})
    assert run.get("source") == ["taskpaw_v3"], (
        "coverage source must be exactly ['taskpaw_v3'] — the frozen V2 scripts "
        "stay out so coverage never pressures V2 churn"
    )


def test_coverage_has_no_hard_threshold_yet() -> None:
    """#144 is measurement-only: no fail_under until the V3 gaps are triaged."""
    report = _coverage_config().get("report", {})
    assert "fail_under" not in report, (
        "a coverage threshold was added — that belongs to a later triage issue, "
        "not #144 (update this guard deliberately when you introduce one)"
    )
