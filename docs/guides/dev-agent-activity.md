# AI activity: hooks, session metadata and CPU

Add a `dev_activity` monitor on each development machine. The agent resolves
Claude Code, Codex and Kimi in this order: **fresh hook → session metadata →
attributed CPU → process presence**. The Hub displays the agent's result.
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
`--taskpaw-hook-id taskpaw-ai-activity-v1-<tool>` argument pair. Only recorded,
unchanged installer-created groups may be deleted when empty. Without the undo
record, uninstall cannot restore the whole file or delete groups. Backups remain
for manual recovery; activity state files remain untouched. Partial I/O failures
are reported per tool with the backup locator; rerun to reconcile.

Exit codes: **0 success, 1 failure, 2 CLI usage error**. Malformed/duplicate-key
JSON, symlinks, unsupported file types, missing executables and concurrent edits
are failures; setup does not replace invalid settings with empty defaults.
Backups may contain unrelated private settings: keep them local.

### What check proves

Check verifies TaskPaw's installed handlers, then runs only the known generated
writer through the supported shell with synthetic busy/waiting/idle stdin. A
random session nonce, correct state and fresh timestamp must appear in isolated
`.activity-check-*` files under the state directory. Each invocation has a 5s
timeout; check files are cleaned up. Live activity files and unrelated hooks are
never executed or overwritten. Edited commands fail with a reinstall diagnostic.
A successful check verifies the writer, not the host's policy, trust or dispatch.

Claude uses bash (Git Bash on Windows). New wiring covers SessionStart,
UserPromptSubmit, PreToolUse, PostToolUse (busy), PermissionRequest and Notification
(waiting), Stop and SessionEnd (idle). Notification is filtered to
`permission_prompt|idle_prompt`. New installs omit SubagentStop: a child finishing
must not mark its parent idle. The writer retains its legacy parser behavior.
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
event names, not host execution. UserPromptSubmit, SessionStart, PreToolUse,
PostToolUse, PreCompact, PostCompact, SubagentStart and SubagentStop mean busy;
PermissionRequest means waiting; Stop, Interrupt and SessionEnd mean idle.
Notification and unknown events do not write state. Actual authenticated host
dispatch remains an operator check, not a recorded automated result.

### Windows support

Use `install --tool claude` and `check --tool claude`. Native Windows Codex and
`--tool all` refuse before settings edits with:
**Windows Codex hook dispatch not verified**. Claude's Git Bash commands must
use forward slashes and literal quoting for paths with spaces or metacharacters
(#206). A manual Claude example:

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
tool; at most eight are accepted. Symlinks/junctions and non-directories are
rejected. Missing default directories are normal; denied metadata is degraded.

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
consuming at most 256 handle paths per root. Native `open_files()` returns its
list before that cap applies; it has no hard timeout. macOS measurements were
fast, but Linux timing and universal handle completeness remain unverified.
Fresh hooks bypass session probes. Stop/reconfigure closes discovery iterators.

CLI identity uses exact executable basenames (`claude`, `codex`, `kimi`), then
argv[0]/name only when the executable is unavailable. Windows strips `.exe` and
ignores case. A real `codex` inside ChatGPT.app counts; renderer/framework CPU,
Claude desktop, prompt arguments mentioning tools, Code core and Kilo do not.
Interpreter launchers require explicit basename overrides or hooks.
`process_patterns` still accepts regexes but **only matches basenames in this
monitor**; the generic process plugin retains full-command matching.
CPU belongs to the nearest AI root once, including its children (at most 500).
Sweeps retain at most 8192 processes and ancestry at most 32 parents. New/reused
PIDs have no baseline; the first sample is presence-only. CPU is percent of one
core. Network waits can look idle when hooks/session evidence are unavailable.

## Reading diagnostics

Both agent rows and the Hub badge show **source** and **host**. Examples:
`claude · Session activity · VS Code`, `codex · Hooks · Other host`.
`observed=true` and `cpu` still mean CPU-derived only. Hook age and session age
have distinct labels. The sampled duty ratio is unchanged and resets on restart.
Bounded `probe_errors` show tool/layer/error codes only; failures set the monitor
to degraded while retaining useful independent evidence. `probe_limited` means
incomplete observation, not idle. Failed observation alone does not emit an idle
completion. Paths, session IDs, process IDs and command arguments are never Hub
metrics or diagnostics.

Host attribution is conservative: mixed same-tool roots are “Multiple hosts”;
missing/denied/reused/cyclic ancestry is “Host unknown”. A tool-wide hook/recent
write cannot identify which of mixed hosts is active, so it does not mark VS Code
busy. Root-specific CPU or open handles can. VS Code is never in `busy_tools`.

These are heuristics, not perfect per-session tracking. One last-writer hook file
per tool means simultaneous sessions can overwrite each other. Housekeeping can
look active; idle interactive Codex TUI handle behavior remains unverified (the
observed idle app-server had no rollout handle). Use `session_activity: false` to
retain CPU fallback, or `observe: false` for hooks/presence only.

After deployment, the operator can install/check, review Codex `/hooks`, then send
a normal prompt, approval and interrupt to observe busy→waiting→idle and VS Code
attribution. This production activation is separate from automated fixture tests.
