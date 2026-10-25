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

## #323 — current machine temperatures (V3 3.10.4)

Baseline is the approved #322 commit `acfd4e7`: its ten CI checks, CTO,
Claude outer and Kimi final all approved before this issue started. The branch
targets main but depends on PR #324 being merged first; if #324 is squashed,
rebase this branch onto main before merging. Leave open, never deploy.

- Design and UI preview precede implementation. Two author design-debate rounds
  closed independent freshness, history-error coupling, partial GPU coverage,
  failure-value clearing and stalled UI polling. The design explicitly records
  the separate bounded NVIDIA query instead of changing the shared power query,
  including its extra periodic subprocess and the reason for isolating power.
- The operator confirmed no known CPU/board reader. These remain unavailable;
  no helper, elevation, speculative adapter, temperature history or control ships.
- True RED evidence: new provider module initially absent; two launcher ownership
  cases failed; 19 fleet assertions failed; 26 initial UI assertions failed.
  Follow-up author checks first reproduced oversized numeric thermal fields,
  CLI permission code 4, safe exception logging and malformed UI provenance.
- Relevant GREEN: 74 producer/launcher checks; 27 new fleet checks plus existing
  fleet coverage; 60 focused UI checks. Full UI: 1,121 passing tests / 43 files.
  Lock consistency, full Ruff/format, mypy (180 source files), UI lint/build pass.
  Existing frontend Fast Refresh and bundle-size warnings remain non-blocking.
- Native Windows verification on 2026-10-25 12:09:39 UTC: the new sampler and
  independent `nvidia-smi -q -d TEMPERATURE` each reported 44 C, with 1/1 NVIDIA
  GPU readable. CPU/board remain null/no_reader. This is one machine's actual
  GPU verification, not a CPU/board or every-machine support claim.
- Static preview and actual PowerPage browser checks passed at 375/768/1024/1440
  pixels. Product fixtures cover ten states per width with 178 assertions and
  16 screenshots, including partial GPUs, missing providers, power-off, old
  Agents, status/history failures, range switching and expiration during a
  hanging request. No browser runtime exceptions or external/product HTTP
  requests occurred; owned Edge/Vite processes were cleaned up.
- Canonical full Python on a fresh short private directory completed with
  7,418 passed, 182 skipped and one failure in 947 seconds. The failure was the
  existing launcher/Fleet test inspecting a pending background read. An Event
  barrier reproduced the same behavior on the approved baseline and this branch;
  the test now waits for the observed result, retaining real startup, credentials
  and shutdown assertions. The deterministic RED failed before this test-only
  synchronization fix; all 370 affected producer/Fleet/launcher/power/version
  tests then passed. Production refresh semantics did not change. This is not
  reported as a single green local full run; complete CI will validate final HEAD.
  No production service, state, configuration or notification was used.
- The first local full run was interrupted after Windows fixture paths caused
  confirmed WinError 206 failures. It is not counted as a complete test result.
  With the same managed interpreter, an unchanged approved baseline and this
  branch both failed the representative lookup case with a long temporary path
  and both passed with a short one. A newly created private short parent passed
  every production ancestor/security check; existing ACLs and security code were
  unchanged. The complete short-path result is recorded above.
- Final lock, Ruff/format, mypy and syntax checks pass. Author constitution/scope
  review found no blocker: V2 and the original power query/energy integrator are
  unchanged; cache-only reads, bounded provider calls, thread cleanup, optional
  wire fields and hardware-isolated tests preserve the existing trust boundary.
  Full remote CI, CTO, Claude and Kimi are pending at this source checkpoint;
  their exact-head results will be recorded on the PR and in private run evidence.
- Initial CI exposed a pre-existing Windows Rust test budget: the success path
  launching a Node parent and child returned false with a three-second limit.
  The original CI log does not prove why. An isolated native diagnostic using
  identical baseline/current helper sources reproduced the same assertion with
  a controlled 3.5-second child startup delay, after successful Job assignment.
  Only the test's success budget now matches the neighboring ten-second tests;
  its intentional timeout stays three seconds, all descendant PID/handle checks
  remain, and production helper limits are unchanged. The controlled delayed
  case passes after that change. A fresh complete CI matrix is required.
- The next CI passed Windows Rust/interop and macOS Python, but both Linux
  matrices caught a race in the new temperature snapshot test: three queued
  history requests could finish while it counted calls around a cache read.
  A history barrier reproduced that false attribution; the test now waits for
  those futures before measuring snapshot IO and asserts the exact accrued age.
  Linux 3.12 also reported two restore attempts in the existing controller-restart
  E2E case; its truncated output did not identify the first attempt's cause or
  evaluate the Jasna count. Three coverage-enabled baseline controls and three
  current controls all retained one attempt/one Jasna invocation across restart.
  The original CI cause remains unknown. That test retains both strict single-run
  assertions and now reports before-restart evidence and the independent Jasna
  count on failure; no production retry, lease or restart behavior changed.
- Both Linux matrices subsequently passed in full (7,161/7,171 passing), as did
  macOS (7,267 passing). Windows completed with 7,400 passed, 200 skipped and one
  existing admin test failure: its normal replacement enable returned `ok=false`
  after the old owner had stopped. The CI log did not include the return payload.
  Baseline/current controls both passed normally; injecting 350 ms into only that
  enable's persistence reproduced the failure on both, with the legitimate
  `saved/starting/persisted_runtime_pending` result under the test's 200 ms budget.
  Restoring the default ten-second budget only after the intentional retirement
  timeout/old-owner completion passed the delayed control. All owner-identity and
  stale-emitter isolation assertions remain. This is a test-budget repair, not a
  proven explanation of the original CI trigger or a production behavior change.
