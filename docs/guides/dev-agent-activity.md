# AI activity: hooks, session metadata and CPU

Add a `dev_activity` monitor on each development machine. The agent resolves
Claude Code, Codex and Kimi using independent hook subjects, positive session
metadata/CPU, then eligible idle estimates and presence. A quiet file never
outranks positive CPU. A hook covers only a proven producer root; other roots'
positive evidence remains available. The Hub displays the agent's result.
`busy`, `waiting`, `idle`, `present_only` and `none` retain their existing meaning.
Presence means the tool is open, not necessarily working. VS Code core CPU never
means AI activity: its context row says “Vibe coding” / “AI 编程中” only when a
busy/waiting AI signal can be attributed to it.

## Install, check and uninstall

From the repository, with Python 3.10+ and the TaskPaw environment available:

```bash
uv run python -m taskpaw_v3.integrations.activity_setup install --tool all
uv run python -m taskpaw_v3.integrations.activity_setup check --tool all
uv run python -m taskpaw_v3.integrations.activity_setup uninstall --tool all
```

These are explicit local commands, never monitor startup actions. They edit only
`~/.claude/settings.json` and `~/.codex/hooks.json`. Use `--tool claude` or
`--tool codex` to select one. `--home PATH` selects a relocated user-home layout;
`--state-dir PATH` defaults to `<home>/.taskpaw`. Install additionally accepts
`--python PATH` and `--writer PATH`; generated commands use absolute paths.
Keep the interpreter and writer at those locations after installation.

Before using the updated sidecar format, update any copied standalone writer
and reinstall the hooks using that writer's path. The new, unmerged format uses
SQLite schema 2 and rich JSON schema 3; the `.activity-v2.sqlite3` filename stays
unchanged. Experimental schema 1 and unknown/future stores are rejected unchanged
and remain unknown. Reinstall does not migrate, erase or rebuild those stores;
their handling requires a separate operator decision. Four-field legacy JSON
remains supported independently.

Pause host settings edits and run one setup at a time. Unrelated settings, hooks,
notify and trust records are preserved. Existing unmarked TaskPaw hooks are
reported and preserved too; they can produce duplicate writes. A correct reinstall
preserves settings and undo-record bytes and creates no backup. Actual edits
first create private timestamped backups in `<state-dir>/hook-setup/backups/`.
The small `<state-dir>/hook-setup/<tool>.json` undo record retains the initial
baseline and latest settings hash. Staged writes are flushed, then the target
hash is rechecked immediately before atomic replacement. This detects observed
conflicts; it is not cross-process compare-and-swap, a lock or crash recovery.

Uninstall restores the exact baseline when settings are unchanged since install.
After later user edits it removes only handlers with the exact reserved
`--taskpaw-hook-id taskpaw-ai-activity-v1-<tool>` argument pair. Exact
unchanged baseline restoration may remove installer scaffolding. Every selective
path preserves all empty groups: indices cannot prove ownership after edits.
Without the undo record, uninstall cannot restore the whole file or delete groups.
A correct reinstall with a missing record repairs a selective-only record without
claiming the original file was absent or adopting an old backup. Missing, changed
or unreadable baseline backups trigger reported selective removal, not restoration. Backups remain
for manual recovery; activity state files remain untouched. Partial I/O failures
are reported per tool with the backup locator; rerun to reconcile.
Reinstalling over externally edited settings revokes whole-file restoration,
so subsequent uninstall preserves those edits even after command updates.

Exit codes: **0 success, 1 failure, 2 CLI usage error**. Malformed/duplicate-key
JSON, symlinks, unsupported file types, missing executables and concurrent edits
are failures; setup does not replace invalid settings with empty defaults.
Backups may contain unrelated private settings: keep them local.

### What check proves

Check verifies TaskPaw's installed handlers, then runs only the known generated
writer through the supported shell with synthetic busy/waiting/idle stdin. A
random session/turn nonce, correct legacy state, fresh timestamp and committed
rich fact must appear in isolated
`.activity-check-*` files under the state directory. Each invocation has a 5s
timeout; check files are cleaned up. Live activity files and unrelated hooks are
never executed or overwritten. Edited commands fail with a reinstall diagnostic.
A successful check verifies the writer, not the host's policy, trust or dispatch.

Claude uses bash (Git Bash on Windows). The legacy JSON projection maps SessionStart,
UserPromptSubmit, PreToolUse, PostToolUse (busy), PermissionRequest and Notification
(waiting), Stop and SessionEnd (idle). Notification is filtered to
`permission_prompt` only; `idle_prompt` inactivity reminders do not mean waiting.
Reinstalling updates existing TaskPaw handlers to this matcher.
New installs include SubagentStart/SubagentStop as independent child facts;
a child stop attempt cannot mark its parent finished. The writer retains the
legacy four-field parser projection, which is not the rich whole-tool result.
All handlers are synchronous commands with a three-second timeout.

### Codex lifecycle hooks and trust

On macOS/Linux, setup writes `~/.codex/hooks.json`, using POSIX sh. Open interactive
Codex, run **`/hooks`**, and review/trust the new TaskPaw definitions. Repeat that
review after command changes. Setup never alters trust or bypasses host policy.
Check explicitly reports **writer verified; Codex trust/dispatch not verified**.
A custom `CODEX_HOME` outside the selected home layout is not supported by the
default installer: use a manual hook in that configuration directory instead.

The [official hook reference](https://learn.chatgpt.com/docs/hooks) supplies the
JSON shape and synthetic fixtures. Local Codex 0.153.4 binary inspection confirmed
event names, not host execution. In the legacy JSON projection, UserPromptSubmit, SessionStart, PreToolUse,
PostToolUse, PreCompact, PostCompact, SubagentStart and SubagentStop mean busy;
PermissionRequest means waiting; Stop, Interrupt and SessionEnd mean idle.
Notification and unknown events do not write state. Actual authenticated host
dispatch remains an operator check, not a recorded automated result.

### Windows support

Use `install --tool claude` and `check --tool claude`. Native Windows Codex and
`--tool all` refuse before settings edits with:
**Windows Codex hook dispatch not verified**. Claude's Git Bash commands must
use forward slashes and literal quoting for paths with spaces or metacharacters
(#206). Setup resolves Git Bash from `CLAUDE_CODE_GIT_BASH_PATH` when set;
otherwise it checks `bin/bash.exe` and `usr/bin/bash.exe` under the Git install
located via `git.exe` on PATH, `%ProgramFiles%/Git`, or
`%LOCALAPPDATA%/Programs/Git`. System32 paths (including the WSL bash launcher)
are rejected. A manual Claude example:

```json
{
  "hooks": {
    "UserPromptSubmit": [{"hooks": [{
      "type": "command",
      "command": "'C:/Program Files/Python/python.exe' 'D:/TaskPaw/taskpaw_v3/integrations/activity_writer.py' --tool claude --path 'C:/Users/Example/.taskpaw/agent-activity-claude.json'",
      "timeout": 3
    }]}]
  }
}
```

**Unverified — Windows Codex hook dispatch not verified.** The following manual
example assumes a POSIX-compatible shell such as Git Bash. That assumption is
unverified for native Codex; this is not supported installer output or proof of
Windows dispatch. Do not use it without verifying your Codex version's shell:

```json
{
  "hooks": {
    "UserPromptSubmit": [{"hooks": [{
      "type": "command",
      "command": "'C:/Program Files/Python/python.exe' 'D:/TaskPaw/taskpaw_v3/integrations/activity_writer.py' --tool codex --path 'C:/Users/Example/.taskpaw/agent-activity-codex.json'",
      "timeout": 3
    }]}]
  }
}
```

Windows-style quoting tests are not native Windows dispatch tests. Session
inference on Windows uses **mtime only**, with no open-files calls. Default
Windows home layouts are assumed; use explicit session roots for relocated data.

### Legacy notify and explicit state

Codex notify remains a compatible end-of-turn alternative in `config.toml`:

```toml
notify = ["python3", "/path/to/taskpaw_v3/integrations/activity_writer.py",
          "--tool", "codex", "--path", "~/.taskpaw/agent-activity-codex.json", "--state", "idle"]
```

Notify passes an extra JSON argument, which the writer still tolerates. It only
reports completion; lifecycle hooks provide busy/waiting as well. Setup leaves
notify alone. An explicit `--state busy|waiting|idle` is also supported for custom
integrations, including Kimi. The writer's default `~/.taskpaw/agent-activity.json`
still feeds the legacy single-file monitor or matching tool's shared fallback.
Unknown/malformed events exit quietly without writing; write errors fail with a
sanitized message.

## Observation defaults and limits

```yaml
- type_id: dev_activity
  name: AI activity
  config:
    tools: [claude, codex, kimi, vscode]
    state_dir: ~/.taskpaw
    freshness_seconds: 300
    window_seconds: 1800
    observe: true
    busy_cpu_percent: 8
    session_activity: true
    session_busy_seconds: 30
    session_idle_seconds: 300
    session_scan_interval_seconds: 30
    session_max_files: 64
    session_roots: {}
```

Default roots are Claude `~/.claude/projects` (`*.jsonl`), Codex
`~/.codex/sessions` (`rollout-*.jsonl`), and Kimi `~/.kimi-code/sessions` plus
`~/.kimi/sessions` (`*.jsonl`, including observed `wire.jsonl`/`context.jsonl`).
Only regular eligible files count. No contents, prompts or code are read.
Kimi now benefits from session metadata and CPU even without lifecycle hooks.

Overrides replace only that tool's defaults, for example
`session_roots: {codex: ["/local/sessions"], kimi: []}`. Empty lists disable lookup.
Paths expand home, not shell variables; relative roots resolve against startup
cwd. `CODEX_HOME` is not silently substituted. Roots must not overlap within a
tool; at most eight are accepted. Root symlink/junction aliases are resolved to
their real paths; non-directories are rejected. Symlinked files/directories
inside a root are ignored, including links leading outside it. Missing default
directories are normal; denied metadata is degraded.

A live CLI is required for session inference. Writes at age ≤30s mean busy;
with complete checks, ages >30s and ≤300s can mean idle. Older archives allow CPU
fallback. Up to 5s future skew is clamped; further-future/nonfinite times are
invalid. On macOS/Linux an eligible transcript currently held open by a validated
CLI root means busy regardless of age. An empty handle list alone never proves
idle. Denied or incomplete evidence permits positive signals but cannot establish
idle. Handles are never reused across polls.

Discovery consumes at most 512 entries and 64 directories per tool per check,
to depth 8, with a queue of 256 and a 100ms budget between discovery operations.
Cursors resume later; newest discovered candidates are capped at 64 (configurable
1–256), re-statted each check and evicted after 600s without revalidation. The
scan does not promise the globally newest file in arbitrarily large histories.
Up to 16 live CLI roots total are inspected round-robin per check on macOS/Linux,
consuming at most 256 handle paths per root. Native `open_files()` returns its list before that cap applies; it has no hard
timeout. Native timing/completeness and idle TUI handles remain unverified.
Fresh associated hook subjects bypass only their covered roots. Stop sets intent
before timed acquisition and returns within the supplied/common monitor budget.
A blocked probe owns its late cleanup; API return does not mean its resources
are already closed. Its return/exception closes iterators exactly once and no
late results/events are committed. A permanently hung native syscall cannot be
force-cancelled safely. No new probe is opened by a stopped instance.

CLI identity uses exact executable basenames (`claude`, `codex`, `kimi`), then
exact argv[0]/name basenames when the executable is not a known CLI. Windows
strips `.exe` and ignores case. A real `codex` inside ChatGPT.app counts; renderer/framework CPU,
Claude desktop, prompt arguments mentioning tools, Code core and Kilo do not.
Node/Python launchers also accept exact tool script names, `kimi-cli`, the Kimi
entry `@moonshot-ai/kimi-code/dist/main.mjs`, and Python's `-m kimi_cli`.
On Windows, node argv[1] additionally accepts package-qualified
`@anthropic-ai/claude-code/cli.js` and `@openai/codex/bin/codex.js`, including
slash/case variations. Unrelated same-basename scripts and prompt arguments do
not match. This covers legacy Claude npm JS launchers; newer Claude packages may
use a native executable. Actual Windows npm dispatch remains unverified.
Other arguments do not establish tool identity.
`process_patterns` still accepts regexes but **only matches basenames in this
monitor**; the generic process plugin retains full-command matching. A vscode
process_patterns override is rejected because VS Code is context only.
CPU belongs to the nearest AI root once, including its children (at most 500).
Sweeps retain at most 8192 processes and ancestry at most 32 parents. New/reused
roots/PIDs have no inherited baseline; the first sample is presence-only. Readable
identity deltas survive other denied descendants, but partial coverage cannot
prove idle. A newly observed child contributes its full CPU only if created after
the previous sample under a continuous root; clock correction/reuse disables this
shortcut. Children older than their apparent parent are nonownership, not partial
CPU; elevated/denied live children remain diagnosed partial reads. CPU is percent
of one core. Network waits can look idle when hooks/session evidence are unavailable.

## Reading diagnostics

Both agent rows and the Hub badge show **source** and **host**. Examples:
`claude · Session activity · VS Code`, `codex · Hooks · Other host`.
`observed=true` and `cpu` still mean CPU-derived only. Hook age and session age
have distinct labels. The sampled duty ratio is unchanged and resets on restart.
Bounded `probe_errors` show tool/layer/error codes only; failures set the monitor
to degraded while retaining useful independent evidence. `probe_limited` means
incomplete observation, not idle. Failed observation alone does not emit an idle
completion for the previously active tools; unrelated-tool failures do not
suppress their idle transition. Paths, session IDs, process IDs and command
arguments are never Hub metrics or diagnostics.

Host attribution is conservative: mixed same-tool roots are “Multiple hosts”;
missing/denied/reused/cyclic ancestry before a validated VS Code ancestor is
“Host unknown”. Once that ancestor is validated, older missing parents do not
invalidate the VS Code host. A tool-wide hook/recent
write cannot identify which of mixed hosts is active, so it does not mark VS Code
busy. Root-specific CPU or open handles can. VS Code is never in `busy_tools`.

## Independent sessions and conservative unknown

Automatic hooks retain their legacy JSON projection and also publish a private
`<JSON path>.activity-v2.sqlite3` cache. Both per-tool and shared default sidecars
are read, even if the shared JSON now names another tool. Session/turn/child IDs
are hashed in the cache; prompts, transcript contents, tool inputs/outputs and
arbitrary argv are never stored/read for this purpose. Optional direct-parent
PID/create-time stays local. A helper parent that cannot match a same-tool CLI
root remains unbound; process absence alone cannot prove its session ended.

Claude prompt_id requires Code v2.1.196+ and is absent before first input; Codex
turn_id is event-specific. Older/missing IDs remain unknown for whole-tool idle
authority. Legacy direct/--state writes and notify arguments still work with the
four core JSON fields; fresh busy/waiting remains useful, while legacy idle cannot
clear independently known active subjects. Both official PermissionRequest inputs
lack tool_use_id; tool-name or receipt-time pairing is not performed.

Stop/SubagentStop can be continued by other host hooks. They are stopping attempts,
not irreversible finality; indistinguishable same-scope delayed/continued progress
stays unknown, including observe=false and quiet model waits. Codex Stop creates
a continuation prompt, without a guessed same/new turn_id. Narrow final proof is
Codex Interrupt for its identified main turn, an exactly bound session end, or a
complete confirmed exit/reuse of a previously bound producer. None closes an
unrelated turn/session/child. Any independent valid busy wins; unresolved activity
defers completion rather than turning silence into success.

For SessionEnd, the monitor has a separate write stage that records an internal
`verified_session_end` witness using its existing exact same-tool root binding.
Hook payloads and a nonzero parent PID cannot create that proof. Fact readers
stay read-only; confirmation opens only existing sidecars. Each participating
per-tool/shared store commits its own witness and scoped retirement together.
The commits are not jointly atomic: partial publication remains unknown and
cannot announce all-idle/off. Retry uses the durable copy without renewing its
original 24h horizon. Unbound or wrong-incarnation finals cannot clear activity.

There is one current rich projection link per physical JSON/sidecar pair, shared
across tools. Every publication uses a fresh nonce, including duplicate facts.
JSON replacement and SQLite commit are not jointly atomic: failed JSON writes
may still commit facts; failed commits cannot borrow an older duplicate's link.
Mismatched linkage remains unknown. A resolved link validates only that exact
current projection after covered fact retirement, not future callbacks or other
subjects. Publishing another tool replaces the link; the previous tool's stored
facts still reduce normally without requiring its own JSON.

Facts cap at2048, current sessions64/turns256/tools64, with256 exact unknown
summaries and64 possible tool overflow latches, within8MiB. CLI reclamation uses
300s; a monitor with shorter freshness retires busy to unknown earlier, not
permission for the standalone writer to delete it earlier. Transactional expiry/
reclamation transfers unresolved evidence into persistent unknown before deleting
it. Any transfer/delete/insert/commit failure rolls back; unknown schema/corruption
is unavailable and never silently rebuilt. Resolved tombstones retain their
original 24h horizon; copies/retries do not renew it. Covered stored facts and
summaries retire transactionally; expired proof cannot close new callbacks.
Unknown summaries and file idle watermarks do not expire. A's summary survives
B ending, time, restart and reinstall. A collapsed overflow latch has lost identity and
cannot automatically clear; persistent unknown is an explicit bounded-storage
cost, not zombie busy. No reset UI or automatic cache deletion is provided.

Normal capacity refusal preserves all old facts and commits bounded unknown
coverage, while the rejected writer still returns nonzero with
`fact_committed=false`. An admitted tool uses an exact unknown summary when space
permits, otherwise its persistent overflow latch. A refused65th tool has no
metadata row, so an existing row carries a fixed store-wide coverage-loss bit:
every reader of that sidecar sees unknown, including the unadmitted tool. Later
local overflow writes preserve this bit; time, restart and unrelated final facts
cannot clear either latch. Independent valid busy/waiting remains usable. Actual
SQL/I/O/commit failures still roll back the complete transaction; when the store
cannot physically be written, new unknown evidence cannot be promised durable.

Open handles and recent-write positives require mtime strictly newer than the
retained idle watermark; expiry never re-enables an old handle. New CPU/writes
remain independently useful. Discovery admits cached active/current-date paths
before overflow, diagnoses exclusions, and computes date tokens once per call.
An error-free completed cycle remains valid across ordinary yields only for the
same roots and revalidated cache, up to max(2*scan_interval,2*poll_interval).

These are bounded heuristics, not perfect per-session tracking. Native TUI idle
>5min, VS Code host dispatch, Windows npm/elevated-process and normal packaged
activation remain **UNVERIFIED**. Synthetic payloads, fake process metadata and
controlled blocked threads establish their own contracts, not native host behavior.
Use session_activity:false for CPU fallback or observe:false for hooks/presence.
After deployment, operator-controlled disposable CLI/VS Code/Windows acceptance
must record version/platform, state/source and limits without prompt/transcript
contents. Activation is separate from automated fixtures and is not performed by
this change.
