# #211 — Accurate AI activity and VS Code attribution (V3 3.9.8) — design v2

## Revision log

- **v2, 2026-09-30, debate round 1 — DR-1 (P1), accepted.** Independently
  reread the issue snapshot, the #206/#207 commit `39cf421`, the guide's Windows
  section and `test_activity.py:248`. The accepted requirement is idempotent
  merge/check/uninstall with backups and atomic writes; none requires a journal,
  locks, crash recovery or four shell renderers. Replace those with exact marker
  ownership, pre-edit backups, an immediate pre-replace hash check, and a small
  undo record. Use exit codes 0/1/2. Claude uses bash (Git Bash on Windows), Codex
  uses POSIX sh on macOS/Linux; refuse native Windows Codex installation and
  document only an explicitly unverified manual example. No disagreement or
  demonstrated defect justifies retaining the removed machinery.
- **v2, 2026-09-30, debate round 1 — DR-2 (P1), accepted.** Independently
  checked the issue's metadata-only requirement, installed psutil's direct
  `open_files()` API (E5), and the existing backend roles. No existing activity
  probe or demonstrated latency defect requires another process. Driver evidence
  E6 supports in-process calls for at most 16 matched live CLI roots on
  macOS/Linux. Windows uses mtime only. Remove helper dispatch, packaging edits,
  subprocess deadlines and process-tree termination requirements. Denied or
  unavailable evidence still cannot prove idle. No disagreement.
- **Additional evidence, not a finding:** E6/A4 records idle Codex app-server
  versus running `codex exec` open-rollout observations. Idle interactive TUI
  behavior remains unverified; no new mechanism is added for that uncertainty.

## Spec review

**Stage:** planner handoff, not implementation approval. **Run:** `2026-09-30-issue-211`.
**Repository:** `AlvinShenSSW/Taskpaw`. **Worktree:**
the issue worktree (branch `issue-211-ai-activity-accuracy`).
Inspected HEAD and `origin/main` both resolve to
`30d24c2e4215905586152ce494995a6be67aa1f1`; working tree was clean on initial
planning entry. This v2 revision starts with the v1 design as an untracked file.
The concurrent #210 run owns 3.9.7; this issue must ship **3.9.8**, by operator instruction.

Requirement source: the complete driver-verified issue #211 text
(0 comments, 0 open PRs, supplied by the driver). The supplied planning inputs
are read-only for this planner. This document contains the full implementation contract; the driver
owns run state, design review, subsequent execution, and evidence retention.

The machine should indicate actual Claude Code/Codex/Kimi activity, including
VS Code-hosted activity, without reading session content. Prefer lifecycle hooks,
then session metadata, then narrowly attributed CPU, then process presence. Make
setup repeatable and diagnosable, and show provenance on both UI surfaces.

### Repository evidence at the inspected revision

All file:line references below are relative to this worktree unless absolute.
References identify existing behavior; requirements phrased as “will”, “must”, or
“proposed” describe work to implement, not code that already exists.

| Evidence | Current behavior and consequence |
|---|---|
| `taskpaw_v3/monitors/plugins/dev_activity.py:59`, `:178`, `:285` | Broad regex defaults feed full-command-line presence matching; presence and CPU perform separate scans. |
| `taskpaw_v3/monitors/process_util.py:91`, `:106`, `:113` | CPU roots match name or full command line; union prevents duplicate PIDs within one tool, but not across different tools. |
| `taskpaw_v3/monitors/plugins/dev_activity.py:79`, `:140`, `:299` | Defaults are 300s hook freshness, 1800s duty window, observation on, 8% CPU; a fresh state precedes CPU. The loader checks timestamps, but does not whitelist state values before returning them. |
| `taskpaw_v3/monitors/plugins/dev_activity.py:196`, `:272`, `:317`, `:324`, `:362` | AI-only headline aggregation, sampled duty, class-change events, and flat metrics already exist. VS Code is context (`ai=false`). |
| `taskpaw_v3/integrations/activity_writer.py:39`, `:51`, `:64`, `:98` | Writer maps Claude events, writes one atomic per-tool/default JSON file, and tolerates Codex notify's trailing argument. It currently uses the Claude map regardless of `--tool`. |
| `taskpaw_v3/monitors/process_util.py:19`, `taskpaw_v3/monitors/plugins/process.py:23` | Generic process monitoring shares the utility but deliberately supports full-command-line regex matching; do not change this unrelated plugin's contract. |
| `taskpaw_v3/ui/src/components/aiActivity.helpers.ts:6`, `:34`, `:40` | Tool metrics allow `observed`/`cpu`; discrimination requires `ai_state` plus `tools`; headline labels already cover all five values. |
| `taskpaw_v3/ui/src/components/AiActivity.tsx:14`, `:41`, `:70` | Badge displays only headline; detailed rows show CPU inference and hook age. |
| `taskpaw_v3/ui/src/views/HubDashboard.tsx:229`, `:233`, `:251`; `taskpaw_v3/ui/src/components/MonitorMetrics.tsx:68` | Online Hub rows already pass AI metrics into the shared badge; detailed metrics already delegate to `AiActivity`. No new Hub transport is needed. |
| `taskpaw_v3/ui/src/i18n.ts:136`, `:452`; `taskpaw_v3/ui/src/schemaI18n.ts:3`, `:29` | Existing English/Chinese activity strings and per-plugin schema translation pattern provide the extension points. |
| `taskpaw_v3/tests/test_dev_activity.py:44`, `:179`, `:202`, `:218`, `:258`; `taskpaw_v3/tests/test_process_activity.py:50`, `:89`, `:104` | Existing tests cover freshness, CPU precedence, subtree union, first-sample behavior and transitions. The test asserting a CPU-busy VS Code row must intentionally change. |
| `taskpaw_v3/tests/test_activity.py:179`, `:227`, `:248`; `taskpaw_v3/ui/src/test/aiactivity.test.tsx:20`; `taskpaw_v3/ui/src/test/hubdashboard.test.tsx:12` | Writer, notify, Windows documentation, basic UI and fleet fixtures are available to extend. |

Required historical reading: #154 design at
`docs/specs/2026-07-02-154-ai-activity-impl-design.md:10`; #163 at
`docs/specs/2026-07-03-163-ai-activity-real-capture-design.md:13` and `:80`.
The latter explicitly deferred an installer and documented CPU's network-wait
limitation. The V3 source of truth favors CLI lifecycle signals and privacy
(`docs/specs/2026-06-27-taskpaw-v3-design.md:301`).

**#206 discovery:** `rg --files docs/specs` plus searches for `206`, `hook`, and
`windows` found no #206 design document. `git show --stat 39cf421` identifies #207
as the merged #206 fix, with guide/test/version changes and no setup/check script.
Its actual check is the shell snippet in `docs/guides/dev-agent-activity.md:91`;
Windows quoting rationale is at `:71`, regression at
`taskpaw_v3/tests/test_activity.py:248`, history at `CHANGELOG.md:24`.
Do not invent a missing helper to reuse.

UI requirements come from the read `design-system/taskpaw-v3/MASTER.md` and
`pages/agent-console.md`, `pages/hub-dashboard.md`, `pages/ai-activity-monitor.md`:
shared dark theme, wrapping dense rows, labeled states, keyboard access and reduced
motion. Retain the shipped sampled duty contract, not the older aspirational
segment diagram (`design-system/taskpaw-v3/pages/ai-activity-monitor.md:157`).

### External verification record (2026-09-30, macOS)

**E1 — installed Codex.** Read-only `strings` inspection and `--version` /
`hooks --help` on
the installed codex-cli 0.153.4 binary (npm global install).
Version output: `codex-cli 0.153.4`. `hooks --help` returned general CLI help,
which has no `hooks` executable subcommand. Both help invocations exited 0 but
warned that PATH aliases could not be created (`Operation not permitted`); no
interactive session or hook was run. Do not repeat CLI startup against the real
home to test hooks. Binary strings contain `hooks.json`, `HookEventsToml`,
`hook_event_name`, `session_id`, `commandWindows`, `trusted_hash`,
`New hook - review required`, and `Modified since last trusted - review required`.
The contiguous event enumeration contains `PreToolUse`, `PermissionRequest`,
`PostToolUse`, `PreCompact`, `PostCompact`, `SessionStart`, `SessionEnd`,
`UserPromptSubmit`, `SubagentStart`, `SubagentStop`, `Stop`, **`Interrupt`**.
Strings establish presence, not execution semantics.

**E2 — official contract.** Opened official OpenAI documentation, following its
redirect to [Hooks](https://learn.chatgpt.com/docs/hooks), sections “Where Codex
looks for hooks”, “Review and trust hooks”, “Config shape”, “Common input fields”,
and event reference. Verified: user `~/.codex/hooks.json`; JSON nesting
`hooks → event → matcher-group array → hooks array → {type:"command", command}`;
stdin JSON fields `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `model`,
with event-specific `turn_id`/`permission_mode`. Non-managed definitions require
hash-specific trust through interactive `/hooks`; changed definitions need review
again. `timeout` uses seconds; SessionEnd/Interrupt allow at most three. Hooks may
be disabled by policy. No-output exit 0 succeeds. This is documentation verification,
not a recorded execution of 0.153.4. Shell selection on Windows remains A1.

**E3 — local config.** Parsed `~/.codex/config.toml` read-only with the existing
venv's `tomllib`, printing only booleans and hook-related feature keys: top-level
`hooks` absent, `notify` present, no hook-related feature override. No values of
notify, credentials, project entries, or other settings were printed or retained.
This does not establish whether other hook sources or trust records exist.

**E4 — session metadata.** Used bounded `os.scandir` and
`DirEntry.stat(follow_symlinks=False)`, never `open`/`read` on any session file.
Paths/IDs were redacted from output except fixed structural names. Observed:

| Root | Observed layout/sample (not a completeness claim) |
|---|---|
| `~/.claude/projects` | 1910 entries inspected to depth 6; direct `<project>/<id>.jsonl` and deeper JSONL files under project/session subdirectories. |
| `~/.codex/sessions` | 3461 entries inspected; 3391 `rollout-<timestamp>-<id>.jsonl` files beneath three directory levels. |
| `~/.kimi-code/sessions` | Exists. A 3000-entry bounded sample contains nested `wire.jsonl` at relative depth 5, alongside JSON/log files. Scan intentionally incomplete. |
| `~/.kimi/sessions` | Exists. 24-entry sample includes `<dir>/<dir>/context.jsonl` (9) and `wire.jsonl` (1), plus a JSON file. |

Metadata layout supports the proposed roots, not the claim that a write/open
handle necessarily means a turn is active. No session age was used to infer the
operator's current activity. Windows layout remains A2.

**E5 — psutil source.** Inspected the project venv's psutil 5.9.8
(`__init__.py:216`). Its public
`open_files()` returns paths/fds (`:1189`); macOS delegates to a native call and
filters regular files (`_psosx.py:494`); Windows converts native device paths to
drive paths and reports fd -1 (`_pswindows.py:1042`). `parent()` checks parent
creation time (`__init__.py:570`), `parents()` repeatedly follows parents (`:589`),
and Windows PPID is cached while POSIX PPID can change (`:625`). Therefore use
PID plus creation time and bounded ancestry. These Python sources do not prove
native handle completeness, latency, or permissions on either OS; those are A3.
No production process's open files were enumerated by this author in planning
or this revision; E6 is separately attributed driver evidence.

**E6 — driver measurements supplied for debate round 1 (2026-09-30).** On
macOS with psutil 5.9.8, `Process.open_files()` took 0.0–0.1 ms for each of
7 live Claude/Codex processes. Idle Codex app-server processes in the VS Code
extension and ChatGPT.app held 0 rollout files open; a running `codex exec`
held 1 rollout open, aged 7s. These are the driver's observations, not a new
author measurement or a latency guarantee for other hosts. They support the
small in-process probe and the heuristic for these process modes, but do not
verify idle interactive-TUI behavior, Linux timing or universal completeness.

## Acceptance criteria

Every issue acceptance bullet is mapped below. Test IDs are defined in Test plan.

| ID | Checkable outcome | Issue bullet / tests |
|---|---|---|
| AC1 | A fixture with a 43.7% ChatGPT renderer and 0.7% Codex CLI does not make Codex busy; a real executable named `codex` inside ChatGPT.app remains eligible. | Measured scenario 1; T1 |
| AC2 | A live Claude root at approximately 0% CPU plus a 7s-old transcript yields busy, `source=session`; on macOS/Linux an old (196s) Codex rollout held open by a validated live Codex root also yields busy. Windows session inference uses mtime only. | Measured Mac scenarios 2–3; T2 |
| AC3 | VS Code core at 20% without AI yields context idle, never busy; a busy/waiting Claude or Codex attributed to VS Code drives the VS Code context row. `busy_tools` includes each AI tool once and never `vscode`. | Measured scenarios 4–5; T1, T3 |
| AC4 | Fresh recognized hook state wins over sessions and CPU. Expired/invalid hook falls through; session evidence wins over CPU; unavailable session evidence allows CPU; absent CPU baseline allows presence. | Precedence; T4 |
| AC5 | Already-correct install preserves settings and undo-record bytes and creates no backup. Preserve unrelated settings/hooks; timestamped backup precedes each edit, hash recheck immediately precedes atomic replacement, and uninstall restores baseline or selectively removes marker-owned handlers while preserving later unrelated edits. Failed check and unsupported native Windows Codex install exit 1 with a useful reason. | Setup lifecycle; T5 |
| AC6 | Codex event fixtures map to the specified states, including PermissionRequest and Interrupt. Retain notify compatibility. Fixtures explicitly identify documentation-derived synthetic input versus captured input; never claim a recording that was not made. | Payload verification; E1–E2, T6 |
| AC7 | Session files with unreadable contents still work via stat. Reads of session contents are prohibited by tests. Enumeration, caches, ancestry, CPU traversal and handle probing have the limits below; stale probe results cannot keep busy alive. | Privacy/bounds; T2, T7 |
| AC8 | Existing headline values and metric keys retain meaning. Add source/host and bounded diagnostic fields. English and Chinese agent rows and Hub badge expose provenance; old-agent metrics remain renderable. | Additive metrics/UI; T3, T8 |
| AC9 | Full pytest, lock check, ruff lint/format, mypy, UI lint, vitest and build pass. All six V3 version files agree at 3.9.8; V2 is untouched. | Gates/version; T9 |

## Frozen issue contract

The smallest causal boundary is the V3 activity writer/setup, activity process and
session observation, `dev_activity` resolution, shared activity UI, associated
tests/docs, and six V3 release values. AC1–AC9 above are the frozen outcomes.

Preserve `ai_state ∈ {busy, waiting, idle, present_only, none}`, AI-only
`busy_tools`, per-tool `state/present/observed`, `ai`, `cpu`, `age_s`, and sampled
`window_s/duty`. In particular, **`observed=true` continues to mean CPU-derived**
(`dev_activity.py:311`; `aiActivity.helpers.ts:11`); session inference gets its own
`source` instead of changing that flag's meaning. Hook age remains hook age.
Fresh hook files remain usable when process inspection is unavailable, as already
tested at `taskpaw_v3/tests/test_dev_activity.py:288`.

Allowed changes: fewer false process matches; a session inference layer; no editor
CPU activity; VS Code context attribution; visible source/host; new opt-in setup
CLI; additive config defaults; explicit degraded diagnostics on observation
failures. `observe=false` remains the operator's escape to hook/presence-only.
Generic `process` regex semantics and other monitors are unaffected.

No session/prompt/code contents, arbitrary command arguments, real session paths,
IDs, usernames, or process identifiers go into metrics, logs, events, or fixtures.
Hook stdin can contain sensitive fields; parse only to select event/session and
discard all other fields without logging. The existing local state `session`
field may remain local; it is never a Hub metric. No secrets in generated command
arguments. Settings/state/undo-record writes are atomic with backups for settings;
no `shell=True`, no network operation, no automatic trust change, no deployment.
These are constitution §2/§4 requirements (`docs/constitution.md:25`, `:54`).

No schema/protocol redesign, `status.md` change, V2 edit, content analysis,
Copilot/Kilo/Cline detector, per-project dashboard, persistent duty history, or
general multi-session lifecycle database. Preserve the existing last-writer
per-tool hook model, with its explicit limitation A5. Any request to promise
perfect per-session attribution requires separate scope approval.

## Assumptions

These are proposed defaults or external claims not fully verified. None is a
permission to silently claim verification. The explicit support limits and
fallback behavior below apply; unknowns alone do not justify new machinery.

| ID | Assumption / unresolved fact | Risk and fallback |
|---|---|---|
| A1 | Native Codex 0.153.4 Windows hook shell and quoting remain unverified. Claude’s Windows Git Bash dispatch is established by #206 repository evidence, not a fresh native test. | Claude uses bash on every OS (Git Bash on Windows); Codex uses POSIX sh on macOS/Linux. Native Windows Codex install refuses with “Windows Codex hook dispatch not verified”; no shell-selection switches or speculative renderers. The guide includes a manual example clearly labeled unverified. Check verifies the generated writer command, not host dispatch/trust. |
| A2 | Default Windows homes/layouts mirror these tool directories under `%USERPROFILE%`; Kimi JSONL naming is stable enough for metadata detection. Only local Mac samples were inspected. | Missed files yield CPU/presence, never content probing. `session_roots` overrides support relocated installations. Document JSONL eligibility and the sampled Kimi layout. |
| A3 | E5 establishes the direct psutil API; E6 reports 0.0–0.1 ms per call for 7 live Claude/Codex processes on macOS/psutil 5.9.8. Linux timing and universal completeness/permissions remain unverified; no demonstrated slow-call defect is present. | Call in-process for at most 16 matched live CLI roots per check, macOS/Linux only. No Windows handle calls; its session layer uses mtime only. Positive handles are usable; empty/denied/unavailable results alone never imply idle. Denied calls emit sanitized degradation and allow other evidence. No helper, hard syscall deadline or privilege escalation. |
| A4 | Recent writes and held-open transcripts are heuristics. E6: idle Codex app-server processes in VS Code and ChatGPT.app held 0 rollouts open, while running `codex exec` held 1 (7s old). Idle interactive-TUI behavior remains unverified; housekeeping writes can still occur. | Keep inference labeled `session`, with fresh hooks authoritative and the issue-required configurable windows. Existing `observe=false` and the session-layer control retain their meanings; add no new mechanism for the unverified TUI concern. No claim of perfect inference. |
| A5 | One per-tool hook file remains acceptable; simultaneous sessions can overwrite each other. A tool-level hook cannot identify which of mixed VS Code/non-VS Code instances produced it. | Retain last-writer compatibility. Do not attribute an ambiguous hook to VS Code. Expose host `mixed`/`unknown`, and infer VS Code activity only from attributable evidence. This is a documented coverage limit, not fabricated certainty. |
| A6 | 30s busy, 300s idle eligibility, 30s rediscovery, newest 64 candidates and the fixed bounds below balance responsiveness and work. These are engineering choices, not measured performance claims. | Long quiet remote work can read idle without hooks/handles; huge histories take multiple scans or remain outside the bounded candidate set. Expose truncation, continue incremental discovery, and prefer open handles. |
| A7 | Metadata stat/scandir on local home storage normally returns promptly; count/time checks cannot interrupt a blocked filesystem syscall. No mounted/network-home timing was verified. | Use a cooperative scan budget, document local-home support and degradation. In-process native handle calls also have no hard syscall deadline. Do not claim an absolute end-to-end poll deadline on arbitrary filesystems. |
| A8 | Official current Codex hook docs correspond sufficiently to 0.153.4. Binary strings corroborate names/fields, but no actual payload or trust interaction was captured. Claude VS Code settings propagation was not exercised. | Use docs-derived fixtures, label host activation unverified until operator smoke, and retain fallback. Do not alter trust or enable disabled host policy in setup. |
| A9 | Proposed mappings treat SessionStart as busy and SubagentStop as ongoing parent work for newly installed hooks. These are state-policy choices; session startup alone can precede a prompt. | SessionStart expires normally. Codex SubagentStop cannot clear the parent; legacy Claude explicit mapping is retained but the new installer omits that event. T6 pins this distinction. |

No clarification blocks this design handoff: the issue and round-1 instructions
supply the required direction; remaining gaps have explicit support limits or
the already specified fallback behavior.
Native validation limitations remain visible to the driver's design review.

## Approach

### 1. One activity-specific process snapshot

Replace activity's two broad sweeps with one typed snapshot in `process_util.py`;
leave `scan_matches`/`scan_one` untouched for the generic process plugin.
Collect PID, PPID, creation time, name, executable, argv[0], and CPU times. Discard
the rest of cmdline immediately; never use argv[1:] to infer identity. If psutil
returns `None` for an inaccessible field, that is unknown, not a numeric zero.

Built-in CLI identity requires an exact basename `claude`, `codex`, or `kimi`
(Windows also strips `.exe` and compares case-insensitively). Prefer executable
path, then argv[0], then process name. A known executable contradicting a tool
name must not be overridden by a matching argument/name. On POSIX use exact
case-sensitive CLI names, avoiding the GUI's `Claude` name when its path is hidden.
Exclude the Claude desktop bundle executables/helpers; reject ChatGPT renderers,
framework helpers and computer-use services by identity. Do **not** reject every
path containing ChatGPT.app: an exact `codex` executable there remains Codex.
Interpreter-only launchers (`node cli.js`, `python -m ...`) are not magically
recognized; hooks still work and an explicit executable-basename override is
available. `process_patterns` stays a validated regex map but, for this activity
monitor only, searches executable/name basenames instead of complete commands.
Document that intentional narrowing.

VS Code identity uses an explicit list (`code`, `Code.exe`, `Visual Studio Code`,
`Code Helper` and its standard parenthesized helper roles), corroborated by the
VS Code executable/bundle path when available. Do not treat a directory called
`code`, a prompt mentioning Code, or arbitrary `Code Something` as the editor.
Follow PPIDs with creation-time validation to at most 32 ancestors; a missing,
denied, reused, or cyclic parent gives host `unknown`, not an invented terminal.
A complete chain containing VS Code gives `vscode`; complete without it gives
`other` (does not claim a terminal).

Assign each process to its nearest matching AI ancestor/root, once globally;
a nested Codex under Claude owns its own subtree. If explicit custom regexes
overlap, built-in exact identity wins, then configured tool order breaks ties.
Deduplicate `tools` preserving order. VS Code never owns CPU. Retain at most 500
descendants per AI root and 8192 process records per sweep; cap ancestry at 32.
Bounded/truncated data must not be treated as evidence of idle.

CPU deltas use `(pid, create_time)` identities and monotonic elapsed time; only
processes present in both consecutive complete samples contribute. New roots
have no baseline; exited/reused PIDs cannot inject lifetime CPU or negative CPU.
This deliberately improves the aggregate-counter baseline at
`process_util.py:133` while preserving 8% of one core as the classification
threshold and first-sample presence-only behavior (`test_process_activity.py:104`).

### 2. Session metadata layer and limits

Add `monitors/session_activity.py`, owning a bounded metadata cache and direct
macOS/Linux handle inspection. Never use `glob('**/*')`, `rglob`, whole-tree sorting,
session JSON parsing, tailing, or content opens.

Proposed config additions to `DevActivityConfig`:

| Field | Default / validation | Purpose |
|---|---|---|
| `session_activity` | `true` | Enable this layer, additionally gated by existing `observe`. |
| `session_busy_seconds` | `30.0`, finite 1–600 | Recent write window; inclusive upper bound. |
| `session_idle_seconds` | `300.0`, finite, ≥ busy window, ≤ 3600 | A recently observed but quiet session can yield idle; ancient history cannot suppress CPU indefinitely. |
| `session_scan_interval_seconds` | `30.0`, finite 1–300 | Monotonic discovery refresh interval. |
| `session_max_files` | `64`, integer 1–256 | Newest discovered metadata candidates per tool. |
| `session_roots` | empty mapping `dict[str, list[str]]` | Per-tool root overrides; replace only that tool's defaults. Empty list disables its session lookup. Paths expand home, not shell variables. |

Default roots: Claude `~/.claude/projects` with regular `*.jsonl`; Codex
`~/.codex/sessions` with regular `rollout-*.jsonl`; Kimi both
`~/.kimi-code/sessions` and `~/.kimi/sessions` with regular `*.jsonl`. No other
file extensions count. Use `Path.home()` on the agent; setup/test `--home` resolves
the equivalent explicit home. Do not silently reinterpret `CODEX_HOME` in the
monitor; overrides are explicit and documented. Relative overrides resolve once
against startup cwd, then remain fixed. Reject duplicate/overlapping roots within
a tool, symlinks/junctions, non-directories, and more than 8 roots per tool; a
missing default directory is normal, not an error.

For each tool with a live root, stat cached candidates each check (at most
`session_max_files`). On discovery ticks consume at most 512 directory entries
and enter at most 64 directories per tool; maximum descent 8 levels, directory
queue 256, live scandir iterators at most one per root. Check a 100ms monotonic
budget between operations for the entire discovery step. Keep cursors across
ticks; close them on exhaustion, error, reconfiguration and stop. When a cycle
finishes, wait the discovery interval before another. Prioritize known active
directories and recent date partitions, then remaining queued directories;
cache and retain the newest candidates encountered by `(mtime_ns, path)`.
Queue overflow/depth exclusion sets a truncation flag; never claim to have found
the globally newest file in a history larger than these bounds. Evict candidates
after 600s without successful revalidation; deleted files disappear immediately.
Skip session discovery and handle calls for a tool while a fresh valid hook
already supplies its state. Continue the cheap process/CPU baseline snapshot for
host attribution and later fallback. Resume metadata work when the hook expires.

Inspect handles separately from this newest-file cache, so an old open rollout
outside the top 64 can still be evidence. On macOS/Linux, at most 16 matched
live CLI roots total across tools are probed per check, round-robin over roots,
and at most 256 returned paths per root
are consumed. Only the same tool's allowed roots/name filters are eligible.
Positive handles must pass a current stat and PID/create-time revalidation;
an arbitrary child opening a transcript is insufficient. No symlink traversal
outside the allowed roots. Other paths returned by psutil are immediately discarded.

On macOS/Linux call `psutil.Process.open_files()` directly in-process for those
roots. E6 supplies measured ordinary latency; there is no demonstrated defect
requiring subprocess isolation. The 256-path cap limits processing after psutil
returns its list, not native enumeration or syscall duration. Calls have no hard
timeout. Catch specific psutil/OS errors and mark unavailable results explicitly;
denied inspection is degraded, process exit/reuse is an expected race. Results
apply only to this check: an old cached positive handle cannot pin busy. Probing
fewer than all roots is marked incomplete, not inferred idle.

On Windows make **no open-files calls**. Session inference uses mtime only;
this is a documented platform limit, not a failed probe or evidence that no
handles exist. Hooks remain primary and CPU/presence remain later layers. Do
not add a helper entry point, frozen backend role or process-tree kill machinery.

Session state policy, when no fresh hook wins:

1. A valid live tool is required; leftover transcripts alone never make AI present.
2. On macOS/Linux, any eligible file held open by that live tool gives **busy**,
   regardless of age.
3. Otherwise the newest discovered eligible file with age ≤ busy window gives
   **busy**. Age from -5s to 0 is clamped to zero; further-future or non-finite
   metadata is rejected and diagnosed.
4. With complete applicable checks and file age in `(busy, idle]`, give **idle**
   as an mtime heuristic. On macOS/Linux, denied/unavailable/truncated handle
   inspection prevents that inference; an empty handle list alone never proves
   idle. Windows uses complete candidate checks only and may miss quiet active
   work that an open handle would identify. Positive recent-write evidence
   remains usable on either platform.
5. No file, age > idle window, disabled layer, or unusable/incomplete negative
   evidence falls through to CPU, then presence. Session metadata never yields
   waiting. A fresh waiting hook remains authoritative.

“Complete” here requires this discovery cycle to have finished without a cap or
error and, on macOS/Linux, all current roots to have usable handle results. A
yielded cursor or root left for the next round makes negative session evidence
unavailable. Windows completeness concerns metadata only. Positive
recent-write/open-handle evidence remains usable even in an incomplete cycle.

The windows reflect the issue's 7s example and ordinary 10s monitor cadence
(`taskpaw_v3/monitors/base.py:49`). On macOS/Linux a 196s rollout remains busy
through its open handle; Windows cannot use that signal. The idle eligibility
ceiling avoids making old archives a permanent override of real CPU work. A4/A6 explicitly cover heuristic errors.

### 3. Resolution, provenance, and VS Code

Resolve each AI tool in strict order **hook → session → CPU → presence**, then
aggregate exactly once. Whitelist hook states busy/waiting/idle; preserve finite
timestamp and legacy shared-file rules (`dev_activity.py:140`). Missing, stale,
or unknown states allow fallback. Wrong tagged per-tool records are invalid.
Hooks do not need a visible process; session/CPU do.

Add these fields without deleting or repurposing old metrics:

| Location/key | Values and meaning |
|---|---|
| `tools[].source` | `hook`, `session`, `cpu`, `presence`; presence means no usable state signal, even when absent. |
| `tools[].host` | `vscode`, `other`, `mixed`, `unknown`. Computed from validated process ancestry of the state evidence where attributable; `mixed` for both known host classes; `unknown` for missing/ambiguous ancestry. No machine name/path. |
| `tools[].session_age_s` | Optional finite number, only for chosen session evidence; kept separate from hook `age_s`. |
| `tools[].vscode_state` | Optional `busy`, `waiting`, `idle`, or null: the attributable VS Code portion of this tool's chosen evidence. Needed when a tool has multiple hosts. |
| `probe_errors` | Optional bounded list of `{tool, layer, code}`; fixed enums, no raw exceptions/paths. |
| `probe_limited` | Optional boolean: one of the finite observation bounds excluded data. |

`observed` is true only for CPU, `cpu` only carries that CPU estimate; session/hook
rows set them false/null. Preserve `age_s` as last hook age even on fallback;
the UI labels it explicitly when present. For tied state candidates, select
busy before waiting before idle, then newer evidence, then configured order.

Host attribution must not equate “some Claude is in VS Code” with “the busy
Claude is in VS Code.” For an open handle or CPU, use its root's ancestry. For a
tool-level recent-file observation or hook without process identity, attribute
only when all validated live roots have the same known host. Mixed roots give
`mixed`, and `vscode_state=null` for ambiguous tool-wide evidence. Do not use a
lower-precedence busy estimate to override a fresh idle hook. A5 documents this
conservative limitation and the existing per-tool last-writer semantics.

Build the VS Code row only after AI rows: busy if any attributable
`vscode_state=busy`, else waiting if any waiting, else idle when VS Code is
present, else null. Always `ai=false`, `host=vscode`, `observed=false`, `cpu=null`.
Its source mirrors the winning hosted AI evidence (precedence breaks equal-state
ties); idle context uses `presence`. Ignore `agent-activity-vscode.json` and editor
CPU for activity. The UI labels an active VS Code row “Vibe coding” / “AI 编程中”
and includes busy/waiting text. Deduplicate AI names before headline/events;
exclude context rows from waiting/active event lists as well as `busy_tools`.

Failures must obey constitution §4: catch specific filesystem/psutil
exceptions, log only tool/layer/error-code, and set generic monitor state
`degraded` when a relevant configured probe fails. Retain useful `ai_state`
from independent evidence and show an activity-probe warning in the UI. Normal
missing paths, process exit races, and deliberately disabled layers are not
errors. A cap sets `probe_limited`; it is an availability limit, not proof of
idle. Do not emit a spurious “AI idle” completion when all previously active
evidence became unavailable because of errors; resume normal edge tracking
after a successful observation.

### 4. Lifecycle writer and setup command

Keep the writer callable as a standalone absolute script. Extend
`state_from_stdin(raw, tool="claude")` with tool-specific dispatch while preserving
the old one-argument callers (`test_activity.py:214`). Existing explicit
`--state`, default output path and `parse_known_args` notify compatibility remain.

Proposed event policy:

| Tool | Busy | Waiting | Idle | No state write |
|---|---|---|---|---|
| Claude | SessionStart, UserPromptSubmit, PreToolUse, PostToolUse | Notification (legacy), PermissionRequest | Stop, SessionEnd, SubagentStop (legacy only) | Unknown events |
| Codex | SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, PreCompact, PostCompact, SubagentStart, SubagentStop | PermissionRequest | Stop, Interrupt, SessionEnd | Unknown events, Notification |

New Claude install wires SessionStart, UserPromptSubmit, PreToolUse, PostToolUse,
PermissionRequest, Notification, Stop, SessionEnd. Notification matcher is
`permission_prompt|idle_prompt`, so unrelated notifications do not indicate
waiting. Omit Claude SubagentStop from installation: finishing one child must
not declare its parent idle. Keep its explicit legacy parser behavior to avoid
changing existing wiring. Codex installs the exact 12 E1 event names; omit
matchers for all-events coverage. All handlers are synchronous `type=command`,
`timeout=3`; no async reordering and no output directing the host's decisions.

Use synthetic, clearly labeled docs-derived payload fixtures with harmless
`session_id`, `hook_event_name`, `cwd`, and nullable `transcript_path`, plus the
event's optional fields. All sensitive body fields are ignored, even when
present. Unknown/malformed events exit 0 without touching state; write failures
exit nonzero with a sanitized error so setup can detect them. Ensure unique
same-directory temporary names and cleanup on replace failure. Never inspect
the transcript path supplied in a hook payload.

New module: `taskpaw_v3/integrations/activity_setup.py`. Public CLI from repo root:

```text
uv run python -m taskpaw_v3.integrations.activity_setup install --tool all
uv run python -m taskpaw_v3.integrations.activity_setup check --tool all
uv run python -m taskpaw_v3.integrations.activity_setup uninstall --tool all
```

Common options: `--tool claude|codex|all` (default all), `--home PATH`
(default `Path.home()`), `--state-dir PATH` (default `<home>/.taskpaw`). Install
also accepts `--python PATH` (default absolute `sys.executable`) and
`--writer PATH` (default absolute adjacent `activity_writer.py`). Absolute paths
make hook execution independent of cwd and shell home expansion. Changing these
options replaces only marker-owned handlers.

Supported shells are fixed: Claude uses bash on macOS/Linux and Git Bash on
Windows, as established by #206; Codex uses POSIX sh on macOS/Linux. Native
Windows Codex install exits 1 with exactly **Windows Codex hook dispatch not
verified** before editing that tool's settings. `--tool all` preflight rejects
that combination before either tool is edited; Windows users install Claude
with `--tool claude`. There are no shell-selection switches or PowerShell/cmd
renderers. Reject a missing required shell, interpreter or writer before edits.

Targets are precisely `<home>/.claude/settings.json` and
`<home>/.codex/hooks.json`. Never edit config.toml, existing notify, trust records,
project settings, managed policy, or host executables. If the user uses a
nondefault Codex configuration directory outside that layout, refuse to claim
that this default installer wired it: document manual installation from the
generated example instead. `--home` supports a relocated user-home layout, not
an arbitrary `CODEX_HOME` directory. Existing manual hooks/notify are
preserved and may duplicate state writes; report detectable TaskPaw duplicates
without adopting or deleting them.

**Rendering/execution and ownership:** generate an absolute interpreter/writer
command carrying the exact argument pair
`--taskpaw-hook-id taskpaw-ai-activity-v1-<tool>`. This unique installer-generated,
per-tool token is reserved for TaskPaw ownership and stays stable across installs;
it is not a random value regenerated on each run. The writer accepts the flag
without changing state semantics. Ownership means a command handler carries that
exact marker argument/value pair for the selected tool, recognized at shell-token
boundaries. A substring, token prefix/suffix, another tool's marker, a quoted
sentence mentioning the marker, or `activity_writer.py` alone is not ownership.
No manifest or equality with a saved handler object grants deletion authority.

Use one POSIX-compatible quoting path for sh/bash, with forward-slash paths for
Windows Git Bash and literal quoting for spaces/Unicode/metacharacters. Never
interpolate stdin payload fields. Check invokes the required shell with argv
and `shell=False` (`-c` with the generated command). Ownership alone does not
permit executing arbitrary edited commands: check requires the known generated
writer-command shape and expected handler fields; deviations fail with a
reinstall diagnostic. It never executes unrelated hooks or arbitrary shell code
found in settings.

**Install and backups:** read and validate the target JSON object before any
edit; malformed JSON, duplicate keys or invalid hook shapes fail without
replacement. Preserve unknown settings and all non-owned handlers and matcher
groups. Append the required handlers in newly created event groups; replace or
remove only marker-owned handlers when reconciling an existing install, leaving
unrelated members and their order untouched. An already-correct install is a
byte-preserving no-op, including the undo record, with no new backup.

Before each actual settings edit, save the original bytes to a timestamped,
unique `<state-dir>/hook-setup/backups/<tool>-<UTC>-<nonce>.json.bak`. For an absent
target, record absence instead of inventing original bytes. Keep a small per-tool
undo record at `<state-dir>/hook-setup/<tool>.json`: target, original-existence
flag, pre-install backup locator/hash, last-written settings hash, and locators
plus matcher/other group fields for groups created by this installer. This group
metadata allows removing only those groups when empty; it never owns handlers.
Retain the first pre-install baseline through updates; later pre-edit backups
protect each edit. Use private POSIX directory/file permissions (0700/0600),
inherit user ACLs on Windows, and never print or upload backup contents.

Stage new settings in the target directory, preserve original permissions,
flush/fsync the staged file, then recheck the current target hash (or continued
absence) **immediately before `os.replace()`**. A mismatch exits 1 without
replacing the target. A failed backup also prevents the edit. Write the small
undo record atomically after a successful settings write. Report a record-write
failure with the retained backup path and exit 1; do not claim success. There is
no journal, phase protocol, OS lock, automatic crash-resume or cross-tool
transaction. Pause host settings edits and run one setup at a time: the final
hash check detects observed conflicts but is not an atomic compare-and-swap
against other writers (R3).

**Uninstall:** if the current target equals the recorded last-written hash,
restore the exact pre-install backup bytes (after validating its saved hash),
or remove the target if originally absent. Otherwise remove only handlers
carrying the exact ownership marker. Remove only groups recorded as created by
this installer that are now empty and whose other fields are unchanged; retain
pre-existing empty groups, unrelated content and groups of uncertain origin.
A missing undo record cannot authorize whole-file restoration or group deletion;
marker-owned handler removal is still possible. Modified marker-owned handlers
remain owned; removing their marker removes that deletion authority. Validate
JSON, back up actual edits, and use the same immediate hash recheck and atomic
replacement rules (recheck before unlink for an originally absent target).
After successful uninstall remove the undo record, retain backups, and leave
activity state files alone. Repeated uninstall is a byte-preserving no-op;
reinstall after uninstall takes the then-current settings as its baseline.
Backups are available for manual undo; no automatic crash recovery is promised.

**Check:** validate the installed marker-owned handlers and known generated
command shape, then run that command with only its output destination changed
to a uniquely named temporary directory beneath `state-dir`. Use synthetic
busy/waiting/idle stdin events and a fresh random session nonce. Each must exit
0 and create output with the correct tool/state/nonce and a current finite
timestamp; never accept stale output. Timeout each invocation after 5s and clean
up the invoked check process and check-owned temporary files. Capture bounded
output but display only sanitized reasons. Missing executables, incorrect
quoting, missing/disabled required entries, bad JSON, timeout or wrong output
fail loudly. Do not change live activity files or execute unrelated hooks.
Native Windows Codex check cannot claim supported dispatch and fails explicitly.
Install runs this generated-command preflight before edits and the installed-form
check afterward; a post-edit failure exits 1 and reports the backup path.

Exit codes: **0 = success, 1 = failure, 2 = CLI usage error**. Malformed settings,
unsupported platform, conflicts and partial completion are failures (1), not
additional exit codes. Print per-tool outcomes. Preflight all requested tools
before edits; if a later I/O failure leaves one tool installed, report that fact
and its backup path. Re-running converges via marker reconciliation; do not
attempt a cross-tool rollback that could overwrite later user edits.

For Codex always print: open interactive Codex, run `/hooks`, review and trust
the new TaskPaw definitions, and repeat after changing their commands. Explicitly
label check success **writer verified; Codex trust/dispatch not verified**.
Never print a `codex hooks` command, set `trusted_hash`, enable bypass flags, or
auto-trust. Activation is a later operator step, not part of this PR.

### 5. UI and documentation

Extend `Tool` and `AiMetrics` optional types, sharing pure formatting helpers
between detailed rows and `AiBadge`. Detailed text shows state, source and host,
plus CPU only for CPU source. For old metrics, infer CPU provenance only from
`observed=true`; otherwise omit unknown provenance instead of labeling legacy
rows “hook”. Unknown enum values get localized “unknown”, never crash rendering.

Add English/Chinese `ai.source.{hook,session,cpu,presence,unknown}` and
`ai.host.{vscode,other,mixed,unknown}`, `ai.vibeCoding`, `ai.probeDegraded`,
`ai.probeLimited`, `ai.hookAge`, `ai.sessionAge`, and session/CPU explanatory text.
Suggested labels: Hooks/钩子, Session activity/会话活动, CPU estimate/CPU 推测,
Process presence/进程在场, VS Code, Other host/其他宿主, Multiple hosts/多个宿主,
Host unknown/宿主未知. Add `dev_activity` schema translations for the new controls
and descriptions of existing inference controls using `schemaI18n.ts:29`'s pattern.

The Hub badge keeps its headline and appends a compact wrapping provenance
summary for state-bearing or present AI tools (for example
`claude · 会话活动 · VS Code`). Context VS Code is not counted again. Full labels
remain readable on keyboard/touch without hover-only content. Detailed rows wrap
at 375px; preserve theme status colors, text labels, reduced motion, duty bar and
online-only badge. No new UI installation action or Hub control surface.

Rewrite the guide around install/check/uninstall, defaults/limits and diagnostic
examples. State that Windows session inference uses mtime only, with no handle
probe. Document native Windows Codex refusal and include a manual hook example
clearly labeled **unverified — Windows Codex hook dispatch not verified**; its
assumed shell must be stated, and it must not imply supported installation. This
example does not add a renderer or a shell-selection option. Retain a correctly
quoted Windows Claude JSON hook example so #206's test is meaningful; update its parser assertions for quoted executable paths rather than
silently removing the test. Keep notify as a compatible legacy alternative,
remove the obsolete “Codex needs launch wrapping” instruction and the obsolete
Kimi presence-only claim (`docs/guides/dev-agent-activity.md:125`, `:130`).

## Files to change

This table is the later implementation allowlist, not authority for this planner
to edit them. This turn writes only this design document.

| Path | Change type | Reason |
|---|---|---|
| `taskpaw_v3/monitors/plugins/dev_activity.py` | Modify | Config, precedence, errors, additive metrics, context projection and cleanup (`:79`, `:282`). |
| `taskpaw_v3/monitors/process_util.py` | Modify | Exact activity identity, ancestry and global CPU ownership; preserve generic matching (`:19`, `:68`). |
| `taskpaw_v3/monitors/session_activity.py` | Add | Metadata discovery/cache and in-process macOS/Linux open-file inspection, at most 16 matched live CLI roots per check; Windows mtime only. |
| `taskpaw_v3/integrations/activity_writer.py` | Modify | Tool-specific events and explicit setup marker; preserve legacy notify (`:51`, `:86`). |
| `taskpaw_v3/integrations/activity_setup.py` | Add | Install/check/uninstall, supported-shell quoting, exact marker ownership, backups and hash-guarded edits. |
| `taskpaw_v3/tests/test_dev_activity.py`, `test_process_activity.py`, `test_activity.py` | Modify | T1/T3/T4/T6 and existing regressions (evidence table above). |
| `taskpaw_v3/tests/test_session_activity.py`, `test_activity_setup.py` | Add | T2/T5/T7 in isolated homes with fake processes. |
| `taskpaw_v3/tests/fixtures/activity_hooks/codex.json` | Add | Synthetic, docs-derived event cases and provenance metadata; no real session capture. |
| `taskpaw_v3/ui/src/components/AiActivity.tsx`, `aiActivity.helpers.ts` | Modify | Shared detail/badge provenance (`AiActivity.tsx:14`, helpers `:6`). |
| `taskpaw_v3/ui/src/i18n.ts`, `schemaI18n.ts` | Modify | zh/en activity strings and config labels (`i18n.ts:136`, `:452`, schema `:29`). |
| `taskpaw_v3/ui/src/test/aiactivity.test.tsx`, `hubdashboard.test.tsx`, `schemai18n.test.tsx` | Modify | Provenance, locale, backward compatibility and online-only badge fixtures. |
| `docs/guides/dev-agent-activity.md` | Modify | New setup workflow and inference limitations (`:48`, `:108`, `:139`). |
| `design-system/taskpaw-v3/pages/ai-activity-monitor.md` | Modify | Add provenance/context display and additive field contract (`:139`). |
| `CHANGELOG.md` | Modify | 3.9.8 issue-scoped release entry; preserve #210 entry. |
| `taskpaw_v3/__init__.py`, `taskpaw_v3/src-tauri/tauri.conf.json`, `taskpaw_v3/src-tauri/Cargo.toml`, `taskpaw_v3/src-tauri/Cargo.lock`, `taskpaw_v3/ui/package.json`, `taskpaw_v3/ui/package-lock.json` | Modify versions only | All six to 3.9.8, including both npm lock root values; the rule is checked at `taskpaw_v3/tests/test_version.py:67`. |

`HubDashboard.tsx` and `MonitorMetrics.tsx` are read/validation participants;
their existing delegation should suffice, avoiding contention with #210.
Do not bump `pyproject.toml`: it tracks V2 (`taskpaw_v3/__init__.py:6`). No new
runtime dependency or uv lock regeneration is planned.

## Execution surface

**Planner:** may read the specified run input, repository and required skill
references; inspect the supplied binary/config and stat the allowed session
trees; read installed psutil; fetch official documentation. Only this design
file is written. No production configuration, session, state, or run-directory
write is authorized. No implementation tests or builds were run in planning.

**Implementer:** source writes are confined to the preceding table. Tests must
redirect home/state/config/session roots to temporary directories and fake
process enumeration; no unit/integration test may discover the implementer's
real sessions or execute their unrelated hooks. No installation against the
operator's real home is authorized by this PR.

| Generated output / generator | Exact production command / inputs | Output / side effects |
|---|---|---|
| Hook settings and undo record / setup module | `uv run python -m taskpaw_v3.integrations.activity_setup install --tool all` from repo root; Python 3.10+, absolute writer, fixed supported shells, validated JSON; optional explicit home/state options above | Settings targets, private timestamped backups and small undo records, temporary preflight files; no CLI invocation or trust changes. Windows requires `--tool claude`; Codex/all refuses before edits. Operator-only after merge. |
| Hook check / setup module | `uv run python -m taskpaw_v3.integrations.activity_setup check --tool all`; validates installed generated commands and runs synthetic stdin via the supported shell | stdout status, ephemeral `<state-dir>/.activity-check-<nonce>/` files then cleanup; no live activity writes. Windows Codex is unsupported; use `--tool claude`. |
| Uninstall / setup module | `uv run python -m taskpaw_v3.integrations.activity_setup uninstall --tool all`; exact command markers, current settings and optional undo record | Pre-edit backup and hash-guarded baseline restoration/selective removal; remove completed undo record, retain backups. Operator-only. |
| Runtime activity / activity writer | Generated invocation: absolute Python + absolute `activity_writer.py --tool <claude\|codex> --path <state-dir>/agent-activity-<tool>.json --taskpaw-hook-id taskpaw-ai-activity-v1-<tool>`; bash for Claude (Git Bash on Windows), POSIX sh for Codex on macOS/Linux, JSON stdin | Atomic local state file and cleaned unique temp; no network, stdout decisions or session reads. Existing explicit notify command remains supported. |
| Handle snapshot / session module | In-process `psutil.Process.open_files()` for at most 16 matched live CLI roots per check on macOS/Linux, with PID/create-time validation | Process metadata only; consume at most 256 paths per root, no subprocess, files or backend role. No Windows call; mtime only there. |
| Test temp artifacts / pytest | `uv run pytest`; fixtures and tmp homes | Temporary synthetic settings/backups/state, pytest cache; never real home. |
| UI build / npm | In `taskpaw_v3/ui`: `npm run build`, using checked-in package/lock and installed dependencies | Local `dist/`/TypeScript build artifacts; no deployment. |
| Version copies / implementer | Apply scoped edits to six values; validate `uv run pytest taskpaw_v3/tests/test_version.py` | Version literals only, no dependency update or regenerated dependency tree. |

Installer backups and undo records are future local execution outputs; listing
them is not authorization to create them during this design revision. Later
development checks may create normal workspace/temp caches.

## Key implementation notes

1. Implement exact process fixtures first, then CPU ownership, then session
   metadata, then precedence. Do not tune heuristics against the operator's live
   activity; the issue's recorded scenario supplies deterministic numbers.
2. Preserve generic regex matching and old writer calls. Existing test seams will
   change from two scans to one snapshot; update fixtures to intercept all process
   and home discovery, including the newly enabled default session layer.
3. A fresh per-tool idle hook wins even over an open transcript. An expired hook
   must not leave `source=hook`. A missing CPU baseline must not synthesize idle.
4. Monotonic time controls CPU deltas, scan cadence and cache scheduling. Agent wall time compares hook timestamps/session mtime;
   no cross-machine time arithmetic is added.
5. Do not model native permissions failures as zero CPU or “no open handles.”
   Use typed unavailable results with a bounded error code. Avoid raw exception
   strings containing private paths. Missing files/process races are expected;
   denied metadata/handle calls are visible degradation.
6. `stop()` must close cached iterators and coordinate teardown with the current
   check. The lifecycle hook exists already at `taskpaw_v3/monitors/base.py:110`;
   reconfigure recreates the instance (`:115`). No observation child is spawned;
   native calls finish in-process without a promised hard cancellation deadline.
7. Installer read-modify-write must validate structure and reject duplicate JSON
   object keys; malformed/truncated JSON must never be replaced by a fresh empty
   settings object. Symlink targets and unsupported file types are refused with
   a diagnostic. Keep all test settings synthetic and secret-free.
8. Setup is a local explicit command, not monitor startup work. Host hooks are
   invoked by Claude/Codex; TaskPaw check executes only its own generated writer.
   A successful command test does not prove the host enabled/trusted/dispatched it.
9. Keep generated handler objects stable: cosmetic command changes cause trust
   churn. Unchanged install must not rewrite them. Three-second hook timeout
   bounds hook overhead; a failed hook must not emit blocking decisions.
10. Do not expand this work into the older UI mockup's duty segments or new icon
    system. Provenance text in existing shared components fulfills the issue.

## Risk assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| R1: open handles persist at an idle prompt | Unverified for idle TUI | False session busy | E6 distinguishes idle app-server from active exec only; retain inference label and primary hooks, with existing controls; A4. |
| R2: hooks differ across Codex versions/native Windows shells | Medium | Hook never updates | Refuse native Windows Codex install; label its guide example unverified. Check supported generated commands; distinguish writer success from host trust/dispatch; A1/A8. |
| R3: another writer edits settings between final hash check and replace | Low with host edits paused | Lost unrelated edit | Pre-replace hash comparison, timestamped backups and exit 1 without overwrite on detected conflict. Ask operators to pause settings edits and run one setup at a time; atomic replacement is not cross-process compare-and-swap. No demonstrated defect warrants locks/journals. |
| R4: large history, permission denial or slow native call | Medium; slow handle calls not demonstrated | Missed activity / polling stall | Finite traversal/caches and at most 16 in-process root calls on macOS/Linux; Windows mtime only. E6 supports ordinary latency, not a hard deadline. Preserve positive evidence and visible limits/errors; A3/A6/A7. |
| R5: mixed-host or concurrent same-tool sessions | Medium | Uncertain VS Code attribution / last writer wins | Conservative host classification, no propagation of ambiguous activity, document A5; do not add a session database. |
| R6: strict executable matching misses script launchers | Medium | Presence false negative | Fresh hooks still usable, basename overrides, explicit support boundary; never revert to full command substrings. |
| R7: new fields/labels confuse old UI or narrow screens | Low | Diagnostic regressions | Optional fields, legacy fixture tests, shared helpers, wrapping 375px manual check. |
| R8: #210 races release/UI files | Medium | Version drift or conflict | Rebase/integrate through driver, preserve #210 behavior, six-file 3.9.8 check; avoid HubDashboard production edits. |
| R9: hook backups contain unrelated secrets | Medium | Accidental disclosure | Private local storage, never print/upload contents, synthetic test fixtures, backup paths only in diagnostics. |

## Out of scope

- Actual operator-machine installation, hook trust review, deployment, commits,
  push, PR creation or merge during this planner stage.
- Session content, prompt/code analysis, VS Code extension APIs, Copilot/Kilo/Cline,
  remote/cloud agent detection, and automatic privilege elevation.
- Changes to V2, Hub transport/storage schemas, event IDs/ports/auth, `status.md`,
  persistent activity history, or a generalized installer framework.
- Guaranteed detection of every session in arbitrarily large histories, perfect
  hook ordering across independent sessions, or a full per-session state machine.
- Deleting/manual migration of pre-existing user hooks or notify, changing host
  policy, installing Python/shells, or updating Codex/Claude/Kimi.

## Test plan

All automated tests use temp homes, synthetic JSON and fake process metadata.
The issue's measured examples are reconstructed fixtures, not recorded private
session data. Freeze wall and monotonic clocks independently where needed.

| Test group | Cases / expected evidence |
|---|---|
| T1 — identity/CPU (`test_process_activity.py`) | 43.7% renderer versus 0.7% Codex; Claude desktop main/helpers and MCP disclaimer excluded; exact CLI under ChatGPT.app accepted; Code core/Kilo CPU cannot enter AI totals; POSIX and Windows basenames; args mentioning tool names ignored; nested Claude→Codex and shared children counted once; cycles/500-descendant/8192-process bounds; process creation/reuse/exit; absent CPU data; existing generic process-regex tests stay green. |
| T2 — sessions (`test_session_activity.py`) | Claude age 7s at 0 CPU is busy; macOS/Linux Codex age 196s held open is busy; open rollout outside newest 64 works; Windows mtime-only busy/idle boundaries and zero open-files calls; Kimi roots/depths/names; live root required; 30s/300s exact boundaries; stale archive permits CPU; empty handles alone cannot prove idle, denied/unavailable/incomplete handles cannot establish idle; fresh idle hook beats positive handles; deleted/symlink/junction/nonregular files ignored; future/nonfinite times; Windows path normalization/case rules; custom roots/empty list. |
| T3 — host/context (`test_dev_activity.py`, UI fixtures) | Claude/Codex under VS Code via intermediate helpers gives host vscode; complete unrelated chain gives other; denied/reused/cyclic ancestry gives unknown; editor at 20% alone stays idle context and headline none; busy/waiting hosted AI mirrors correctly; one Claude headline entry; ambiguous tool-wide hook with mixed roots does not falsely label VS Code busy; CPU/handle evidence attributable to one root can do so. |
| T4 — precedence/regression (`test_dev_activity.py`) | Hook idle versus session busy/CPU99; hook waiting; expired hook → session → CPU → presence; malformed/unknown hook state; timestamp skew; shared default fallback; no live process still accepts fresh hook; observe false disables both inference layers; session_activity false retains CPU; first sample no CPU; busy/waiting/off transitions, duty ratio, deduped tool list; probe failure sets degraded and prevents false idle completion. |
| T5 — setup (`test_activity_setup.py`) | Both JSON shapes; absent/existing targets; correct reinstall preserves settings/record bytes and creates no backup; exact marker tokens own handlers, while prefixes/suffixes, other-tool markers, quoted prose and writer-name matches do not; marked modified handlers removable, unmarked handlers preserved; unrelated groups/matchers/settings/order retained; remove only recorded created groups that are now empty and otherwise unchanged; bash/sh quoting and Windows Git Bash paths with spaces/Unicode/metacharacters plus injection sentinels; Windows Codex refusal with exact message and all-tools preflight makes no edits; missing executables; check busy/waiting/idle, wrong state/nonce/stale output/timeout and edited command rejection; no live activity overwrite; malformed/duplicate-key JSON; backup failure prevents edit; timestamped backup before edits; hash conflict causes no overwrite; replace/record-write failure reports failure; selective uninstall preserves later edits; exact baseline restoration and original absence; missing record never authorizes whole-file restoration/group deletion; uninstall twice and reinstall; partial outcomes exit 1; exit 2 only for usage; no execution of unrelated hooks/trust changes. |
| T6 — writer (`test_activity.py` + fixture) | Every mapped Codex event, especially PermissionRequest/Interrupt/SubagentStop; ignored Notification/unknown events; camelCase legacy inputs; invalid JSON/type; Claude legacy mapping intact; installer omits Claude SubagentStop; only allowed fields persist; sentinel prompt/tool-input contents never appear in output/logs; atomic replacement/cleanup; notify trailing arg still accepted. Fixture metadata cites E2 and says synthetic, never “captured”. |
| T7 — bounds/privacy/shutdown | Monkeypatch session content APIs to raise while permitting stat; unreadable contents on POSIX (Windows mock equivalent); no session reads or prompt/cmdline leaks; counter fixtures exceed caps; cursor resumes/closes and cache evicts/revalidates; in-process macOS/Linux calls capped at 16 matched live CLI roots total per check and 256 consumed paths per root, with PID/create-time revalidation and round-robin selection; no handle subprocess or frozen-role launch; Windows makes zero open-files calls and uses mtime only; denied/unavailable evidence is not idle, positives remain usable; stop/reconfigure closes cached iterators without leaked resources; missing directories normal versus denied directories degraded. |
| T8 — UI (`aiactivity.test.tsx`, `hubdashboard.test.tsx`, `schemai18n.test.tsx`) | Explicitly set each locale; source/host text in full rows and badge; old metrics without fields; CPU hint only when CPU-derived; hook versus session age label; mixed/unknown enum fallback; waiting/busy VS Code label; headline excludes context; degraded/limited warning; offline stale snapshot has no badge; both language schema labels; unchanged MonitorMetrics delegation. |
| T9 — whole-project gates/version | Commands below; preserve legacy state_file tests and all unrelated suites. Verify all six V3 versions equal 3.9.8; no V2/pyproject version change, no unintended lock dependency changes. |

T7 also verifies that fresh hooks short-circuit session probes and handle
positives are never reused across checks. Packaging files/roles and their tests
are unchanged; the whole-project suite still runs their existing coverage.

Required later commands (run from repository root unless noted):

```text
uv lock --check
uv run pytest
uv run python -m py_compile taskpaw.py taskpaw_hub.py macsubs.py
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

From `taskpaw_v3/ui`: `npm run lint`, `npm test` (vitest run per
`taskpaw_v3/ui/package.json:7`), and `npm run build`. Do not call a docs-only plan
“green CI”; the implementer/driver must retain actual results.

Manual/safe integration checks during implementation:

1. In a disposable temp home, run the new install/check/uninstall twice through
   bash for Claude and POSIX sh for Codex; assert unrelated synthetic hooks remain.
   Windows-style unit paths are necessary but not equivalent to native execution.
2. On native Windows CI or a disposable Windows user profile, exercise Git Bash
   for Claude and verify native Windows Codex install refuses before any edits.
   If unavailable, report native coverage missing; Windows-style fixtures do not
   establish dispatch. The unverified manual Codex example is not a support claim.
3. For an optional macOS/Linux Codex hermetic dispatch smoke, use an isolated
   home/config, no copied credentials, no real projects/sessions, and only harmless synthetic
   hooks. No trust bypass. If the installed CLI cannot run the selected lifecycle
   without authentication, retain E2 docs-derived fixtures and the unverified
   dispatch limitation rather than accessing production credentials.
4. View agent and Hub activity at 375/768/1024/1440px in zh/en, with old/new,
   busy/waiting/mixed-host/degraded fixtures and reduced motion; verify labels
   wrap, focus is visible, and no horizontal scrolling or hover-only diagnosis.
5. **Post-merge operator-only, supported tool/platform pairs:** install, check,
   review Codex `/hooks` on macOS/Linux, send a normal prompt/tool approval/interrupt
   and inspect only TaskPaw state metadata.
   Observe busy→waiting→idle and the VS Code row. This is a documented follow-up,
   not an implementation-stage production action or a prerequisite fabricated
   as already completed.

## Handoff notes (historical)

These notes record the design-stage handoff; the implementation now exists in
this branch. The decisions below are preserved as recorded.

- Bounded result: design v2 only; AC1–AC9 mapped to planned tests. Only this
  document was revised; no run-directory writes or implementation tests/builds.
  No production code, config, run state, commits, pushes or PRs were produced.
- Baseline is the supplied issue snapshot plus this frozen contract at git
  `30d24c2`; input driver verification of 0 comments/0 open PRs was not repeated.
  Local history confirms #206/#207 and the #154/#163 implementations. No separate
  #206 design exists in the searched `docs/specs` tree.
- Initial planning recorded the shared afk configuration as read:
  test/lint/build commands agree with the gates above; design-gate is off.
  This does not supersede the driver's review policy or authorize later actions.
- External verification is E1–E5 plus separately attributed driver measurements
  E6. A1 means native Windows Codex installation is refused; Claude uses bash/Git
  Bash and macOS/Linux Codex uses sh. A3 permits direct in-process handle calls
  on macOS/Linux (at most 16 roots); Windows uses mtime only. A4 includes the
  app-server/exec observation, with idle TUI behavior still unverified. Universal
  handle completeness, host activation and inference/attribution limits remain
  explicit. No sensitive payload was captured in this revision.
- Initial planning recorded the afk planner contract and its environment,
  stage-output, continuity and design-review references as read.
  This is a child stage handoff; it does not claim or resume the run directory.
- Review-cycle consumption/reservations are not known to this planner; do not
  treat that as zero. This v2 resolves DR-1/DR-2 as recorded in the Revision log;
  the driver retains acceptance authority. No disagreement is retained.
- Next action belongs to the driver: review the frozen design and its explicit
  assumptions, then execute the tests/implementation stage under the run's
  existing authority. Preserve 3.9.7/#210 changes when integrating 3.9.8 and
  revalidate citations/overlap if the base changes.
