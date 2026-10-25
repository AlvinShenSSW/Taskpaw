# AFK delivery records

## 2026-10-25 — #322 Agent current task presentation

Scope: issue #322, followed by #323 in the operator's queue. Operator instruction
overrides the issues' original no-bump text: #322 increments V3 from 3.10.2 to
3.10.3; #323 is reserved for 3.10.4. Default merge policy is leave-open. No deploy.
Author family: Codex; required external order: Claude outer, then Kimi final.
CI and external verdicts belong to the delivery PR for `afk/agent-status-322`.

Design: [current task layout](specs/2026-10-25-agent-status-322-design.md).
Preview: [Agent fixture](../design-system/taskpaw-v3/preview/agent-status-322.html).
Two internal design rounds closed connection/phase separation, stale envelope
precedence, older payload handling, transfer identity/direction and pause errors.

Validation evidence:

- TDD: ten new failing UI regressions with 27 existing passes; then 38 targeted
  passes and 1,081 full UI tests across 42 files. Lint/build passed.
- Actual browser fixture: 69 state/layout checks plus 16 Hub/browser role-header
  checks. Initial 375px shared header overflow was fixed with wrapping only.
  Product APIs were intercepted; no live backend or production database was used.
- `uv lock --check`, Ruff lint/format, and Mypy (179 source files) passed.
- Full Python run: 7,339 passed, 182 skipped, 18 failed. One failure caught the
  retired phrase inside a new waiting label; it was changed to waiting-to-start
  wording, and its source regression passed. The other 17 failed because this
  host's system Temp ancestor ACL is intentionally rejected by the production
  credential directory guard. The unchanged base reproduced the same failures.
- Native diagnostics identified the unsafe ancestor; a new private test parent
  outside system Temp passed every production directory check. The same baseline
  tests then passed with the same interpreter. All 17 affected tests passed under
  that private base. No existing ACL or production security check was changed.
- The retired-copy regression and all seven version consistency cases passed.
  Local full-run and focused closure results are reported separately; a single
  clean local full run is not claimed. The PR's clean CI matrix is the final gate.

Live read-only baseline: controller and four enrolled Agents reported 3.10.2;
this did not establish identical repair-build UI bundles. Shipping/installing the
new version is separate from source and fixture verification.

Review repair batch:

- Initial commit `8d8024f` passed all ten CI checks, including the full Windows,
  macOS and Linux Python suites, frontend, static checks and packaging. CTO
  self-review approved. Claude outer review returned APPROVE WITH COMMENTS.
- All three comments were addressed together: remove the secondary position
  chip, omit elapsed time before an attempt exists, and distinguish input
  revisions of the same film. The last case was reproduced with the scheduler
  and dispatcher before implementation. Console-only revision metadata preserves
  the strict legacy heartbeat schema. Ambiguous legacy transfers stay unavailable.
- Revision regressions: two backend and six UI failures reproduced before the
  fix. Relevant backend checks passed 226 tests (one skip); full UI passed 1,087
  tests in 42 files. UI lint/build, full Ruff/Mypy and lock consistency passed.
  The retired-copy check and seven version checks passed again; V3 stays 3.10.3.
- Actual product browser fixtures covered 14 states at 375/768/1024/1440 pixels,
  including missing and ambiguous revisions, with no overflow or JS errors.
  All product API requests were mocked; no backend or real database was started.
- The repaired commit requires a fresh full CI matrix, CTO self-review and
  Claude-to-Kimi review. Results are recorded on PR #324; the first commit's
  approvals do not apply automatically to the repair. Leave open; no deployment.
