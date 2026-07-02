# Design: #142 Fix agent-onboarding doc drift

Date: 2026-07-02 · Issue: #142 · Branch: `docs/142-onboarding-drift`

Docs-only change. The onboarding docs the agent workflow reads first (AGENTS.md,
constitution.md, CLAUDE.md, skill/afk/SKILL.md) describe a reality that no longer
holds, costing review cycles. Fix the drift at the source.

## Drift found (verified)
- **AGENTS.md §Status (line 37)** and **constitution.md (line 19)** say the V3
  monorepo is "`taskpaw-v3/` (a monorepo, not yet created)". Reality: it exists as
  `taskpaw_v3/` (underscore) with `agent/`, `hub/`, `ui/`, `src-tauri/`, `core/`,
  `monitors/`, `migrate/`, `integrations/`, `packaging/`, and 22 backend
  `test_*.py` files.
- **AGENTS.md repo-layout table** omits `taskpaw_v3/`, `scripts/`, `docs/guides/`.
- **skill/afk/SKILL.md:64** carries `uv run --locked --extra web pytest` — the
  `--extra web` belongs to a different project (MDCX304); TaskPaw has no `web`
  extra. Today AGENTS.md and CLAUDE.md carry *warning notes* that patch over this
  at the reader; fixing the source removes the need for the notes.
- **CLAUDE.md** repeats the hyphen path `taskpaw-v3/`.

## Changes & decisions
1. **Path/status correction** in AGENTS.md §Status and constitution.md §1: hyphen
   → underscore, "not yet created" → describe it as the greenfield tree that now
   exists (V3 remains the target for new capability; the correction is factual, not
   a scope change).
2. **AGENTS.md layout table**: add rows for `taskpaw_v3/` (V3 monorepo: agent, hub,
   FastAPI backend, monitors-as-plugins, Tauri shell, migration tooling, 22 backend
   tests), `scripts/` (agent/hub setup + build helpers), `docs/guides/` (deployment
   / signing / integration guides).
   - **Deliberately does NOT touch the audit-files row (line 31).** PR #147 (#146)
     moves those files to `docs/audits/` and edits that same row; keeping #142 off
     that line avoids a merge conflict between the two open PRs.
3. **skill/afk/SKILL.md:64**: `uv run --locked --extra web pytest` →
   `uv run --locked pytest`, and drop the "涉 server 需 `--extra web`" parenthetical.
   This is the authoritative fix at source.
4. **Remove the now-redundant `--extra web` warning notes** in AGENTS.md
   (§Commands blockquote) and CLAUDE.md — they only existed to warn about the
   SKILL.md line now fixed.
5. **CLAUDE.md**: hyphen path `taskpaw-v3/` → `taskpaw_v3/` (same drift).

## Test plan
Docs-only; no code paths change. Gate: `uv run pytest` still green (the smoke suite
scans shell scripts / repo shape, unaffected). No behavioural test to add — this is
the documented exception to "every change ships a test" (constitution §5 targets
*behavioural* change; there is none here).

## Constitution gate
- §1 Scope: docs only; no V2/V3 code touched; audit-row left to #146 to avoid
  cross-PR conflict.
- §5 Testing: no behavioural change → no new test; suite must stay green.
