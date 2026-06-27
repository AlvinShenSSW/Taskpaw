---
name: afk
description: Away-From-Keyboard autonomous execution. Use when the operator hands off a PRE-SCOPED, pre-reviewed set of issues/PRs with /afk (or "AFK mode", "go AFK on …") for full autonomous execution — design-first per issue, strict waterfall, a ~30-min self-scheduled cron relay to survive pauses/rate-limits, and self-pause after 2 consecutive idle ticks. REQUIRES an operator-provided scope; never pick issues from the tracker yourself.
---

# AFK Mode

Hand-off mode: the operator designed + reviewed a scope; you execute exactly that
queue autonomously and stop yourself when done or stuck.

**This SKILL.md is the canonical, self-contained spec for AFK mode — follow it
directly.** (There is no separate `docs/afk-mode.md`.)

## Kickoff contract (do this first, every time)

1. **Require a scope.** The operator MUST provide the explicit issues/PRs (and/or
   file areas) to touch. **No scope → STOP and ask.** Never browse the tracker and
   pick work yourself. The scope fences everything you may touch.
2. Confirm the **merge policy** (`leave-open` default / `merge-to-unblock` /
   `merge-when-green`) and any **constraints** (worktrees not to touch, branch
   naming, shadow-first only, deploy = operator's job, summary language).
3. Restate the scope back in one line, then start.

## Driver variant (who implements vs who gates)

The **invariant**: every external reviewer is always a *different* model from whoever
did the implementation — you never let a model review its own work.

- **`/afk` (default, Claude-driven):** Claude does design doc + TDD implementation +
  `cto-pr-review`. Then **two sequential external reviews, both run** (each ≠ the
  implementer Claude): first **Codex as the outer gate / 外门** (`/codex-review`) —
  Claude triages and fixes every finding — then **Kimi as the final review / 终审**
  (`/kimi-review`), which has the last word before hand-back. If one of the two is
  unavailable, continue with the other — a degraded run is allowed but **must be
  flagged to the operator** (see **Degraded external review** below).
- **`/afk codex` (Codex-driven):** **Codex** does the design, development, and CTO
  review (driven via `codex exec`). Then **two sequential external reviews, both run**
  (each ≠ the implementer Codex): first **Claude as the outer gate / 外门** (Claude
  runs the independent structural review) — triage and fix every finding — then
  **Kimi as the final review / 终审** (`/kimi-review`), the last word before
  hand-back. If one of the two is unavailable, continue with the other — a degraded
  run is allowed but **must be flagged to the operator** (see below). **Never Codex
  as a reviewer here** — it implemented, so it cannot review itself.

**Symmetry:** the implementer also does CTO; the **外门 is the other driver model**
(Codex for `/afk`, Claude for `/afk codex`); **Kimi is always the 终审**. Neither
external reviewer is ever the implementer. Everything else in the waterfall below is
identical across variants.

**Degraded external review (one reviewer down → keep going, but tell the operator).**
The two external reviews are resilient, not all-or-nothing. If the 外门 **or** the
终审 is unavailable (out of quota / not installed / not logged in / self-skips), **do
not block** — run the remaining one and proceed. This is explicitly allowed. But a
single-review run is a **degraded** run, so you **must notify the operator**: state
which review ran, which was skipped, and why — both inline in the PR/commit notes
**and** in the end-of-run report. Only when **both** external reviews are unavailable
do you fall back to CTO review alone and record `external gate unavailable`. Never
silently drop a reviewer.

## Per issue (waterfall, one at a time)

design doc (`docs/specs/YYYY-MM-DD-<topic>-design.md`) → multi-round adversarial
debate until clean → TDD (RED→GREEN) → adversarial sweep → full
`uv lock --check` + `uv run --locked --extra web pytest` (本项目用 pytest, 非 unittest;
涉 server 需 `--extra web`) → constitution-gate self-check (见 `docs/constitution.md`) → commit →
push early → open PR → watch CI (fix red) → CTO self-review (`cto-pr-review`) → fix
every finding → **external review — runs automatically in AFK mode** (no
operator is present to invoke it). **The reviewers follow the Driver variant above**
(reviewer ≠ implementer): **both variants run two stages — 外门 then Kimi 终审.**
Default `/afk`: **Codex 外门** (`/codex-review`), triage + batch-fix, then **Kimi 终审**
(`/kimi-review`). `/afk codex`: **Claude 外门** (independent structural review),
triage + batch-fix, then **Kimi 终审**. The 外门 is the non-implementer driver model;
Kimi is always last. If one reviewer is unavailable, run the remaining one and
**flag the degraded run to the operator** (per **Degraded external review** above);
only if **both** are unavailable, continue on CTO review alone and record
`external gate unavailable`.
**Never the implementer as a reviewer** (no Codex reviewer under `/afk codex`). **Metering discipline (still
applies per stage):** each stage triages structural findings, batches one
self-review pass, defers doc/minor items to a single final pass — Codex finds once
and you fix in one batch, Kimi 终审 should pass in a single round, not a loop →
merge per policy. The design doc is more important than the code.

## Autonomy

Decide with best-practice defaults and record each decision; don't block on
in-scope work. Risky changes ship safe-direction (shadow-first behind a default-OFF
flag, fail-safe, additive). Only stop for: out-of-scope work, a destructive/
outward-facing action without authorization, or genuine ambiguity with no safe
default. Never merge red CI or an unresolved finding; never touch another session's
branch; never deploy (merge ≠ deploy; the operator pulls + restarts).

## Continuity + self-pause (the core)

- Create a **recurring ~30-min cron** that re-invokes you each tick (survives
  pauses / rate-limits / context resets). The cron prompt is self-contained: scope,
  order, merge policy, constraints, a "done so far" ledger, and the FIRST-each-tick
  state checks (`gh issue list`, `gh pr list`, `git branch`, `git status`) → resume
  the first unfinished step. One branch per issue off `origin/main`; push early.
  For Claude-driven runs, use Claude's scheduled-task mechanism. For Codex-driven
  runs, use an external OS scheduler/runner (launchd/cron/tmux loop) that invokes
  `codex exec` with this self-contained tick prompt; Codex has no built-in
  `CronCreate`/`CronDelete` equivalent, so the runner is the cron.
- Track **substantial new content** per tick (a new commit / pushed branch / opened
  PR / new design doc / resolved CI failure / resolved finding).
  **2 consecutive ticks with none → auto-pause:** delete/disable the scheduler
  (`CronDelete` for Claude, or the external OS runner for Codex), post a status
  report (blocking + remaining), STOP. Queue complete → auto-stop + final report.
  **Always delete the scheduler on stop — never leave an orphan cron/runner.**

## End-of-run report

Every PR # with state (merged / open-awaiting-review), every notable decision,
deferred/remaining items, anything blocking. **Any degraded external review** (a
reviewer that was down → which one ran, which was skipped, why). Operator's
preferred language.
