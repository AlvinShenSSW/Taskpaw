#!/usr/bin/env bash
# Post the 2026-07-02 project-review findings as GitHub issues.
# Run from repo root: bash scripts/create-review-issues.sh
# Requires gh (installed under ~/.local/bin) authenticated for AlvinShenSSW/Taskpaw.
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
R="AlvinShenSSW/Taskpaw"

gh issue create -R "$R" -t "CI: add real linting + type checking (ruff, mypy, eslint)" -b "$(cat <<'EOF'
The CI job is named "lint + test" but only runs pytest — no linter or type checker exists anywhere in the repo.

**Proposed:**
- Add `ruff check` + `ruff format --check` to CI (config in `pyproject.toml`).
- Add `mypy` scoped to `taskpaw_v3/` only (V2 is frozen; don't churn it).
- Add ESLint + a `lint` script to `taskpaw_v3/ui/package.json` and run it in the frontend CI job. Today only `tsc -b` during build catches type errors; test-only paths are unchecked.

Highest-value, lowest-effort item from the 2026-07-02 project review.
EOF
)"

gh issue create -R "$R" -t "Docs: fix agent-onboarding drift (taskpaw_v3 exists, layout table, afk test command)" -b "$(cat <<'EOF'
AGENTS.md and docs/constitution.md both say the V3 monorepo is "`taskpaw-v3/`, not yet created". Reality: it's `taskpaw_v3/` (underscore) with a full agent, hub, UI, Tauri shell, migration tooling, and 23 backend test files.

**Fix:**
- Correct the path/status in AGENTS.md §Status and constitution §1.
- Update the AGENTS.md repo-layout table: add `taskpaw_v3/`, `scripts/`, `docs/guides/`.
- Fix the `uv run --locked --extra web pytest` command in `skill/afk/SKILL.md` at the source (it belongs to MDCX304), instead of carrying warning notes in AGENTS.md and CLAUDE.md.

The whole workflow is agent-driven — stale onboarding docs cost real review cycles.
EOF
)"

gh issue create -R "$R" -t "CI: add a Windows test job" -b "$(cat <<'EOF'
CI runs on ubuntu-latest only, but Windows is the PRIMARY agent target (V2 `taskpaw.py` and the V3 agent installers, #126).

**Proposed:** add a cheap `windows-latest` matrix entry that runs `uv sync --group dev` + `uv run pytest`. This catches path-separator, encoding, and process-handling bugs before they hit the real fleet. Packaging (#126) stays separate.
EOF
)"

gh issue create -R "$R" -t "Testing: add coverage measurement (pytest-cov)" -b "$(cat <<'EOF'
428 tests pass but nothing measures what they exercise.

**Proposed:** add `pytest-cov` to the dev group and a coverage report in CI (threshold optional at first). Focus review on V3 supervisor/lifecycle code — untested paths there hurt most. Exclude frozen V2 scripts from any threshold so it doesn't force V2 churn.
EOF
)"

gh issue create -R "$R" -t "V3 security: harden the auth-disabled default" -b "$(cat <<'EOF'
Empty bearer token = auth disabled (deliberate V2 parity, `taskpaw_v3/core/auth.py`). For V3, an unauthenticated network API on a non-loopback bind deserves better defaults.

**Options (pick one):**
1. Generate a token on first run and print/store it (secure by default).
2. Minimum: loud startup warning + UI banner when the network API binds non-loopback with no token set.

Constitution §2 ("Network-facing HTTP requires auth") currently has a silent hole when no token is configured.
EOF
)"

gh issue create -R "$R" -t "Hygiene: bare except in V2, requirements.txt drift, pytest warning, audit-file location" -b "$(cat <<'EOF'
Small items from the 2026-07-02 review, batched:

- `taskpaw.py:2584` — bare `except:` in the local-IP lookup (constitution §4 violation; trivial critical-fix: `except OSError:`).
- `requirements.txt` duplicates `pyproject.toml` and will drift — delete it or generate it from the lockfile.
- The suite emits `PytestUnhandledThreadExceptionWarning` (simulated thread death in event-delivery tests) — handle it in the test, then consider `filterwarnings = ["error"]` to keep the suite warning-clean.
- Move `BUG_AUDIT.md` / `CODE_AUDIT_REPORT.md` / `CODEX_AUDIT_FINDINGS.md` to `docs/audits/` now that v2.7 closed them out; root stays for README/CHANGELOG/AGENTS.
EOF
)"

echo "Done — 6 issues created."
