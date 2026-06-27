---
name: codex-review
description: Run Codex (OpenAI Codex CLI) as a final, read-only, second-opinion review gate on the current PR/branch, then triage and fix the findings. Scoped to architecture + real bugs. Use only when Codex is a valid non-implementer gate: default /afk outer gate (外门, runs first, before the Kimi 终审) or operator-selected interactive review; never as the /afk codex gate. Triggers include "/codex-review", "run codex review", "codex gate".
preferred_model: claude-opus-4-7
role: external-reviewer
phase: pre-merge
workflow: review-and-fix
---

# Codex Review Gate

You are running the **Codex review gate** — an independent second-opinion review by
the OpenAI Codex CLI (a *different* model), used as the **last** check before a PR
is handed back for approval. Run the CTO review (`cto-pr-review`) **first** and
resolve its verdict, then run this gate on the result only when Codex did not
implement the change. Codex reviews the diff read-only; you (Claude) triage and
fix.

The helper and prompt **ship with this repo** at
[`skill/codex-review/codex-gate.mjs`](codex-gate.mjs) — the gate travels with the
code, so every clone/worktree/operator gets the same pipeline.

## Activation

Triggered when the operator runs `/codex-review`, or says "run the codex review",
"codex gate", "external review", etc. Optional in interactive sessions (the
operator's call); automatic in **default `/afk` as the outer gate (外门) — it runs
first, before the Kimi 终审** (both run). In `/afk codex`, Codex is the implementer
and this gate must not run.

**Codex calls are metered — keep invocations to an absolute minimum.** Batch all
findings into one fix pass, self-review, and only then re-run; defer
documentation/minor items to a single final pass. Never burn a Codex round-trip on
small or doc-only edits.

**Scope:** the gate runs Codex's built-in `review` — a structural code review
(architecture/design, correctness, security, safety, missed edge cases). It no
longer injects a custom focus prompt: codex-cli 0.137.0+ rejects a PROMPT
alongside a diff selector (`--base`/`--commit`/`--uncommitted`), so the gate
relies on Codex's native review (#405). Treat what comes back as high-signal —
but still verify each finding, prioritise structural items over nitpicks, and do
**not** chase exhaustive polish (see **When to stop**).

## How to run it

1. Invoke the in-repo helper **in the background** (the review is slow — it reads
   the diff, traces code paths, and may run tests). Capture stdout to a file, and
   pass any operator-provided target flag (`--base <branch>` / `--commit <sha>` /
   `--uncommitted`; default = current branch vs the repo's default branch) through
   to the helper:

   - **Windows (PowerShell):**
     `node "skill/codex-review/codex-gate.mjs" 1> "$env:TEMP\codex_gate.out" 2> "$env:TEMP\codex_gate.err"`
   - **macOS / Linux (zsh/bash):**
     `node "skill/codex-review/codex-gate.mjs" 1> "${TMPDIR:-/tmp}/codex_gate.out" 2> "${TMPDIR:-/tmp}/codex_gate.err"`

   Run it with `run_in_background: true` and a generous timeout (≥ 600000 ms). Do
   not poll in a sleep loop — wait for the completion notification. The helper picks
   the right per-OS flags automatically (it bypasses the OS sandbox **only** on
   Windows, where Codex cannot sandbox under a normal token; macOS/Linux keep their
   native sandbox).

2. When it completes, read the `.out` file. Codex's verdict is between the
   `===== CODEX REVIEW (final message) =====` markers. The `.err` file holds the
   invocation line and the path to the full transcript log. **If the verdict is
   `SKIPPED: …`** — Codex not installed, not logged in (no subscription), or
   disabled via `CODEX_REVIEW_GATE=off` — the gate is intentionally optional:
   report that it was skipped and continue. Do not treat a skip as a failure and do
   not retry. **If you skipped because Codex is out of credits / unavailable**, the
   default `/afk` still runs the [`kimi-review`](../kimi-review/SKILL.md) 终审 — note
   the missing 外门 and continue. Keep Codex calls minimal: one outer-gate pass per
   round, batch all fixes, do not loop.

   > **Order note:** in the default **`/afk` waterfall, Codex runs first as the 外门,
   > then Kimi 终审 runs last** (both run — additive, not alternatives). In
   > **`/afk codex`**, Codex implemented the change, so this gate must not run; the
   > external review is Kimi (Claude fallback). In interactive sessions the
   > operator picks either, preserving reviewer != implementer.

## How to handle the findings (batch — minimise Codex calls)

The cost you are managing is the **number of Codex invocations**, not the size of
each one. Never re-run Codex for a single fix or a doc tweak. Work in batched rounds:

1. **Sort findings by kind.** *Structural* — architecture, design, correctness
   bugs, security loopholes, missed edge cases (this is what Codex is for). *Minor*
   — naming, comments, cosmetics: set aside in a deferred list; do NOT act yet.
   (Codex's native review is not scope-filtered, so triage minor findings out
   yourself — defer or drop them.)
2. **Verify before trusting.** Each finding is a hypothesis. Read the cited
   file:line — Codex can misread context. Push back with evidence (file:line +
   actual behavior + spec) on anything you can disprove; do not capitulate to a
   wrong finding.
3. **Fix every confirmed structural finding in one batch**, and sweep for the same
   pattern elsewhere. Keep design/spec docs in sync with these fixes in the same
   change.
4. **Run one additional self-review round** over your fixes — catch follow-on issues
   yourself instead of spending a Codex round-trip on them.
5. **Only then re-run this gate once.** Repeat 1–5 until the **stop rule** below is met.
6. **Deferred-docs pass — once, at the very end.** Resolve the accumulated minor
   list in a single pass. Do NOT re-run Codex just to confirm doc edits.

## When to stop (convergence)

Codex on a detailed diff — especially a design/spec doc — can surface ever-finer
edge cases almost indefinitely. The gate exists for **architecture + real bugs, not
exhaustive polish.** Stop the loop and hand back to the operator when ANY holds:

- a round returns **no new P1 (blocker) findings** — only P2/minor or implementation-detail;
- you have done roughly **2–3 fix→re-run rounds** and the findings are getting
  progressively finer, or are narrowing to one already-addressed subsystem (you are
  now reviewing your own last fix's wording);
- it is a **design-only doc** and the remainder is implementation-detail that TDD
  will enforce in code anyway.

When you stop, report honestly — `CLEAN` (nothing structural) vs `OUTSTANDING — loop
stopped by judgment` (with what's left) — and let the **operator** decide whether to
spend more metered rounds. Do **not** spiral through many rounds chasing diminishing
nitpicks; that back-and-forth is exactly the metered-cost waste this gate avoids.

## Report back

End with a concise summary:

- **Codex verdict:** (its final message, quoted)
- **Confirmed & fixed:** (each finding + the fix)
- **Disagreed:** (each finding you rejected + the evidence)
- **Gate status:** CLEAN / OUTSTANDING (and what remains)

Do not merge, push, or declare the PR ready on the strength of a clean Codex pass
alone — hand it back to the operator for their own approval, per the project
workflow.

## Setup (per machine, once)

Codex is optional and self-skipping. To make the gate live: install Codex
(`npm i -g @openai/codex && codex login` — ChatGPT-subscription auth, no API key);
needs Node + `git` on PATH. Disable permanently with `CODEX_REVIEW_GATE=off`. The
helper is cross-platform (native sandbox on macOS/Linux, bypass on Windows).
