# #347 — 概览 page: 「正在处理」 shows 5 rows before folding

Date: 2026-10-26 · Base: `5a5fe79` (main, 3.11.1) · Branch: `afk/overview-active-5`
Planner: Claude (driver). Implementer: Claude Opus 5.5. Gate: Codex astra xhigh.

## Spec review

The 媒体处理中心 概览 page (`taskpaw_v3/ui/src/components/media/OverviewPage.tsx`,
section `aria-labelledby="ov-active"`) renders `data.active.slice(0, 4)` film rows and
folds the rest into `另有 {n - 4} 部正在处理`. The fleet now has five machines
processing at once, so the operator wants five rows visible. The 「等待」 section
below it keeps its four rows.

## Acceptance criteria

- AC1 With five active films the section renders five `FilmRowView` rows and no
  `另有` line; with six it renders five rows and `另有 1 部正在处理`.
- AC2 The 「等待」 section is unchanged (four rows, `另有 {n - 4} 部等待`).
- AC3 A test in `taskpaw_v3/ui/src/test/` covers AC1 (six active films: five rows,
  the summary text). `npx tsc --noEmit -p .`, `npm run lint`, `npx vitest run` green.
- AC4 CHANGELOG entry under a new newest heading `## V3 3.11.1 — 概览「正在处理」显示 5 部（#347）`
  stating it is Hub-only and ships without a version bump.

## Frozen issue contract

- Invariants: no backend, protocol or settings change; the page's other sections
  untouched; V3 copy conventions.
- Allowed change: the active-section row limit 4 → 5 (three literals in one file).
- Non-goals: a configurable limit, changing the 「等待」 limit, any layout change.
- Causal boundary: `OverviewPage.tsx`, one test file, `CHANGELOG.md`.

## Approach

Replace the three `4`s in the active section with a module constant `ACTIVE_ROWS = 5`
(so the slice, the condition and the count agree), leaving the waiting section's
literals as they are.

## Test plan

RED: the new test (six active films → five rows + `另有 1 部正在处理`) fails on the
base. GREEN: tsc, lint, vitest. The driver runs the Python suite on the merged tree.
Test placement (critic round 1): add the case to `src/test/mediaworkspace.test.tsx`
inside `describe("概览")` (line 284). Its `mockApi()` (line 32) takes no films
argument and the fixture `src/test/fixtures/mediaWorkspace.ts` `FILMS` yields exactly
four active items (abf-401..404 running; abf-405 is pending → waiting), so the test
spreads `FILMS_RESPONSE` with two extra `row({ …, status: "running" })` entries and
overrides `api.workflowFilms` for that case (or adds a `films` parameter to
`mockApi`). Count rows inside the region named `正在处理` via `article.film-row`
(`OverviewPage.tsx:254`; no data-testid). No existing test asserts the four-row
limit; `src/test/multimachine.test.tsx:225` only checks machine names of two active
films and needs no change. Do not widen the multimachine fixture.

## Debate record

- Round 1 (critic): three active-section literals confirmed and separate from the
  waiting section's; no upstream cap on `data.active` (`workspace.ts:84`, backend
  pages only the done group); CHANGELOG heading pattern consistent; test placement
  detail added above (P2). The historical build spec
  `docs/specs/2026-10-21-media-center-v3-build-spec.md:274` still says four rows —
  left as is (historical). No design change.

## Handoff notes

Commit `feat(ui): 概览 active section shows five films before folding (#347) [skip ci]`.
