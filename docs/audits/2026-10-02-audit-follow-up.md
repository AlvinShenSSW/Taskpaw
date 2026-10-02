# 2026-10-02 audit follow-up: source, delivery and validation

This dated index connects the audit follow-ups to their public issues and PRs.
It supplements historical audits and release notes; it does not replace their
findings or certify a deployment. The source baseline is
[`91f7745`](https://github.com/AlvinShenSSW/Taskpaw/commit/91f7745564f17a0039f81fab8293a9c150232236),
including R16 / [PR #229](https://github.com/AlvinShenSSW/Taskpaw/pull/229).

## How to read the evidence

- **Merged source** means the change is in the referenced repository revision.
- **Ready, unmerged** means an open non-Draft PR with recorded review/check
  evidence. It is still outside this baseline. Its head and checks are linked
  below; later commits need their own checks.
- **Draft, unmerged** is an open proposal whose review/check gates are still
  being resolved; it is not Ready or delivered.
- **In progress / planning / waiting** records the scoped queue, not delivered
  code. An open issue without a PR is not evidence that its fix exists remotely.
- **Installed and native validation** require separate operator records. Python
  or DOM tests, fake endpoints, mock process trees and CI bundles prove their
  modeled contracts; they do not establish installed WebView behavior, real
  Jasna/WhisperJAV/GPU cleanup, LAN faults or long-running reliability.

The ten historical spec evidence tables reconcile all 94 original acceptance
items: 45 checked for their documented source/automated contract, 49 left
unchecked for superseded, partial or unverified clauses. Original multi-line
criteria and ordering are preserved. Historical versions/defaults are not
rewritten as current ones. Historical merged PRs and green checks alone do not
complete a compound criterion.

## Public queue snapshot

GitHub metadata read at **2026-10-01T18:03:09+00:00** (2026-10-02 JST). This is a snapshot, not a live tracker. There are 26 issue rows; R10/R11 are carried by I216 rather than separate invented issues. Only R16 is merged in this queue at this snapshot.

| ID / scope | Public issue | Public PR / source status | Automated evidence / remaining boundary |
|---|---|---|---|
| R16 — platform test baseline | [#217](https://github.com/AlvinShenSSW/Taskpaw/issues/217) (closed) | [PR #229](https://github.com/AlvinShenSSW/Taskpaw/pull/229) — Merged | Head [`9520e5f`](https://github.com/AlvinShenSSW/Taskpaw/commit/9520e5ff5ccd139d79b1d6b8e2f68d71b80d38e4); [recorded checks](https://github.com/AlvinShenSSW/Taskpaw/pull/229/checks) SUCCESS. Filesystem case-policy regression and macOS Python CI; this restores the automated baseline, not field acceptance. |
| R01 — local control credentials/origins | [#218](https://github.com/AlvinShenSSW/Taskpaw/issues/218) (open) | [PR #241](https://github.com/AlvinShenSSW/Taskpaw/pull/241) — Ready; open, unmerged | Head [`b1f27db`](https://github.com/AlvinShenSSW/Taskpaw/commit/b1f27db9d7954611d7ef2fafb8a7b8211c605f1d); [recorded checks](https://github.com/AlvinShenSSW/Taskpaw/pull/241/checks) SUCCESS. Prerequisites: R16. Native desktop/window actions and real installation remain V01; do not treat the new control split as baseline. |
| R02 — outbound redirect credential isolation | [#219](https://github.com/AlvinShenSSW/Taskpaw/issues/219) (open) | [PR #242](https://github.com/AlvinShenSSW/Taskpaw/pull/242) — Ready; open, unmerged | Head [`f260ec2`](https://github.com/AlvinShenSSW/Taskpaw/commit/f260ec2964545b96de25cb69dd740851560857bf); [recorded checks](https://github.com/AlvinShenSSW/Taskpaw/pull/242/checks) SUCCESS. Prerequisites: R16. Controlled HTTP/TLS and DB fixtures are recorded in the PR; no user services were tested. |
| R03 — legacy outbox date migration | [#220](https://github.com/AlvinShenSSW/Taskpaw/issues/220) (open) | [PR #243](https://github.com/AlvinShenSSW/Taskpaw/pull/243) — Ready; open, unmerged | Head [`1660096`](https://github.com/AlvinShenSSW/Taskpaw/commit/1660096745906e8ac7eee36ab204a53a5d71c9bf); [recorded checks](https://github.com/AlvinShenSSW/Taskpaw/pull/243/checks) SUCCESS. Prerequisites: R16. Temporary SQLite migration/recovery evidence is recorded in the PR; no installed Hub DB was migrated. |
| R05 — event cursor recovery | [#221](https://github.com/AlvinShenSSW/Taskpaw/issues/221) (open) | [PR #244](https://github.com/AlvinShenSSW/Taskpaw/pull/244) — Draft; open, unmerged | Head [`69df818`](https://github.com/AlvinShenSSW/Taskpaw/commit/69df818b83f135a1b7797af876c2e3d418c12295); [recorded checks](https://github.com/AlvinShenSSW/Taskpaw/pull/244/checks) in progress; no completed passing profile claimed. Independent review/release gates remain in progress; Draft is not Ready or delivered. Prerequisites: R16. Cursor recovery is separate from durable event-body replay (A03); no exactly-once promise. |
| R06 — upstream snapshot/resource isolation | [#222](https://github.com/AlvinShenSSW/Taskpaw/issues/222) (open) | Waiting for prerequisite merges; no public PR | No public implementation/check result linked at this snapshot. Prerequisites: R16, R02, R05. |
| R04 — offline/recovery alert persistence | [#223](https://github.com/AlvinShenSSW/Taskpaw/issues/223) (open) | Waiting for prerequisite merges; no public PR | No public implementation/check result linked at this snapshot. Prerequisites: R16, R03, R06. |
| R07 — config/runtime rollback and stop | [#224](https://github.com/AlvinShenSSW/Taskpaw/issues/224) (open) | Waiting for prerequisite merges; no public PR | No public implementation/check result linked at this snapshot. Prerequisites: R16, R01. Disk/instance failures must preserve truthful runtime state and emergency stop. |
| R08 — process ownership, deadlines, GPU handoff | [#225](https://github.com/AlvinShenSSW/Taskpaw/issues/225) (open) | Waiting for prerequisite merges; no public PR | No public implementation/check result linked at this snapshot. Prerequisites: R16, R07. Controlled descendants do not substitute for native media/GPU task cleanup. |
| R09 — Windows graceful close | [#226](https://github.com/AlvinShenSSW/Taskpaw/issues/226) (open) | Waiting for prerequisite merges; no public PR | No public implementation/check result linked at this snapshot. Prerequisites: R16, R01, R08. Windows close/timeout/process cleanup needs native acceptance. |
| I216 — AI activity; R10/R11 and existing minors | [#216](https://github.com/AlvinShenSSW/Taskpaw/issues/216) (open) | Planning; no public PR | No public implementation/check result linked at this snapshot. Prerequisites: R16. CPU/session/hook evidence and multi-session behavior; actual idle TUI handles/npm launchers remain native checks. |
| R12 — LAN onboarding/token pairing | [#227](https://github.com/AlvinShenSSW/Taskpaw/issues/227) (open) | Waiting for prerequisite merges; no public PR | No public implementation/check result linked at this snapshot. Prerequisites: R16, R01, R07. macOS/PowerShell setup, cancellation and real LAN token pairing remain operator checks. |
| R13 — Mac release/signing matrix | [#228](https://github.com/AlvinShenSSW/Taskpaw/issues/228) (open) | [PR #245](https://github.com/AlvinShenSSW/Taskpaw/pull/245) — Draft; open, unmerged | Head [`dd050b1`](https://github.com/AlvinShenSSW/Taskpaw/commit/dd050b1f91b41a63082a8e8281ed71340e5db038); [recorded checks](https://github.com/AlvinShenSSW/Taskpaw/pull/245/checks) in progress; no completed passing profile claimed. Independent review/release gates remain in progress; Draft is not Ready or delivered. Prerequisites: R16. [Artifact-only release run](https://github.com/AlvinShenSSW/Taskpaw/actions/runs/36903600912) is in progress at this head; no signed/notarized or clean-machine acceptance claimed. |
| R14 — truthful online/disabled/error UI | [#230](https://github.com/AlvinShenSSW/Taskpaw/issues/230) (open) | Waiting for prerequisite merges; no public PR | No public implementation/check result linked at this snapshot. Prerequisites: R16, R06. Freshness and disabled/offline evidence, including error visibility. |
| R15 — dependency security/scanning | [#231](https://github.com/AlvinShenSSW/Taskpaw/issues/231) (open) | [PR #246](https://github.com/AlvinShenSSW/Taskpaw/pull/246) — Draft; open, unmerged | Head [`4cc6883`](https://github.com/AlvinShenSSW/Taskpaw/commit/4cc6883099f644077f7b2eda9052279deba04c05); [recorded checks](https://github.com/AlvinShenSSW/Taskpaw/pull/246/checks) in progress; no completed passing profile claimed. Independent review/release gates remain in progress; Draft is not Ready or delivered. Prerequisites: R16. Exposure assessment and ongoing scans; no blanket dependency safety claim. |
| I172 — external agents/per-server tokens | [#172](https://github.com/AlvinShenSSW/Taskpaw/issues/172) (open) | Waiting for prerequisite merges; no public PR | No public implementation/check result linked at this snapshot. Prerequisites: R16, R02, R05, R06. Compatibility, token precedence and truthful status.md; external application validation remains. |
| I52 — connect-only Hub client | [#52](https://github.com/AlvinShenSSW/Taskpaw/issues/52) (open) | Waiting for prerequisite merges; no public PR | No public implementation/check result linked at this snapshot. Prerequisites: R16, R01, R08, R09, R13, R14. Connect-only Hub ownership; native close/reconnect validation remains. |
| A01 — sampling/delivery scheduling separation | [#232](https://github.com/AlvinShenSSW/Taskpaw/issues/232) (open) | Waiting for prerequisite merges; no public PR | No public implementation/check result linked at this snapshot. Prerequisites: R16, R02, R03, R06. |
| A02 — notification health/dead-letter replay/history | [#233](https://github.com/AlvinShenSSW/Taskpaw/issues/233) (open) | Waiting for prerequisite merges; no public PR | No public implementation/check result linked at this snapshot. Prerequisites: R16, R01, R03, R04, A01. |
| A03 — bounded durable event bodies/replay | [#234](https://github.com/AlvinShenSSW/Taskpaw/issues/234) (open) | Waiting for prerequisite merges; no public PR | No public implementation/check result linked at this snapshot. Prerequisites: R16, R05, R06. Event-body durability is distinct from monotonic IDs and at-most-once legacy reads. |
| A04 — limited lifecycle extraction | [#235](https://github.com/AlvinShenSSW/Taskpaw/issues/235) (open) | Waiting for prerequisite merges; no public PR | No public implementation/check result linked at this snapshot. Prerequisites: R16, R08. |
| A05 — evidence age/source display | [#236](https://github.com/AlvinShenSSW/Taskpaw/issues/236) (open) | Waiting for prerequisite merges; no public PR | No public implementation/check result linked at this snapshot. Prerequisites: R16, R06, R14, I216. |
| A06 — services, recovery, rollback, retirement | [#237](https://github.com/AlvinShenSSW/Taskpaw/issues/237) (open) | Waiting for prerequisite merges; no public PR | No public implementation/check result linked at this snapshot. Prerequisites: R16, R01, R03, R05, R08, R09, R12, R13, I52. Actual service inventory, migration, backups, restore/rollback and V2/MacSubs retirement are operator work. |
| A07 — measured UI loading/lint cleanup | [#238](https://github.com/AlvinShenSSW/Taskpaw/issues/238) (open) | Waiting for prerequisite merges; no public PR | No public implementation/check result linked at this snapshot. Prerequisites: R16, R14, R15. |
| D01 — documentation evidence reconciliation | [#239](https://github.com/AlvinShenSSW/Taskpaw/issues/239) (open) | Documentation in progress; no public PR | No public implementation/check result linked at this snapshot. Prerequisites: R16. Doc-only; tests/product/dependencies/versions unchanged. This page is not a native acceptance record. |
| V01 — native/cross-machine/long-duration validation | [#240](https://github.com/AlvinShenSSW/Taskpaw/issues/240) (open) | Manual validation tracker; open | Prerequisites: R16, R01, R02, R03, R04, R05, R06, R07, R08, R09, I216, R12, R13, R14, R15, I172, I52, A01, A02, A03, A04, A05, A06, A07. Mac/Windows/Hub release, cross-machine faults and sustained stability need recorded native runs. |

## Source versions and field retirement

[V2 metadata](../../pyproject.toml) and the V2 scripts remain at 2.7.1;
[V3's source version](../../taskpaw_v3/__init__.py) is 3.9.8 on this baseline.
The [version consistency test](../../taskpaw_v3/tests/test_version.py) checks the
V3 UI/bundle copies. These source facts do not identify the version installed or
running on any machine. Open PR heads above are remote proposals, not installed
releases.

V2 is frozen for critical fixes. `macsubs.py` is present but excluded from V3
monitoring, as specified by the [V3 design](../specs/2026-06-27-taskpaw-v3-design.md).
Neither fact proves deployed V2/MacSubs has stopped or been removed. A06 / [#237](https://github.com/AlvinShenSSW/Taskpaw/issues/237)
and V01 / [#240](https://github.com/AlvinShenSSW/Taskpaw/issues/240) own site
inventory, migration, backup/restore, rollback and actual retirement records.
No real app, configuration, database, process or service was inspected for this
documentation update.

Known audit boundaries remain visible: R07 handles persistence/runtime
consistency; R08 handles descendant ownership and GPU release; R09 handles
Windows shutdown; R12 handles LAN setup/pairing; I216 handles AI activity
uncertainty. Historical fake-process/clock/DOM results do not close those issues.
Formal signing/notarization, clean-machine launch, native control actions,
external media execution, optional tray behavior, cross-machine failure and
long-duration stability remain manual/native acceptance until separately
recorded. See [macOS signing](../guides/macos-signing.md),
[Windows signing](../guides/windows-signing.md) and
[deployment](../guides/deployment.md) for existing operational references;
those guides are not changed or re-certified here.
