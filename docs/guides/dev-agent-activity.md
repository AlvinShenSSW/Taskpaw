# Dev-agent activity monitor — is this machine running AI?

*(V3 design §5c, issues #22 + #154. **Dev-agent machines only** — AI runs on
agents; the Hub only aggregates and displays.)*

Surface whether the **Claude Code / Codex / Kimi** running in your VSCode is
**busy** (running a task), **waiting** (needs your input), **idle** (open at the
prompt), or just **present** (the tool is running but not reporting activity). The
`dev_activity` monitor combines two signals:

- **present** — config-free: the tool's process (VS Code + the CLI) is running,
  detected via psutil. Coarse: "the tool is open", not "it's working". This alone
  already stops a busy dev box from showing as *idle*.
- **observed** — config-free (#163): when a tool is present but has no hook state,
  TaskPaw infers **busy/idle from the CPU of the tool's process subtree** — a pure
  external `psutil` read that does **not** write to, wrap, or otherwise affect the
  tool. This is what makes Kimi (which has no hooks) and an un-wired Claude/Codex/VS
  Code show *real* busy/idle instead of "present (unreported)". Observed rows are
  marked with `~<cpu>%` in the UI. Enabled by default (`observe: true`,
  `busy_cpu_percent: 8`). *Caveat:* while a tool is purely waiting on the model
  (network, low local CPU) it can briefly read idle — the hook signal below closes
  that gap, so the two are complementary.
- **state** — precise busy/idle/waiting from a small JSON file each CLI writes
  through the `activity_writer.py` wrapper (below). Most accurate (covers the
  model-thinking phase); takes precedence over the observed signal.

> **Privacy:** only the tool name, state (`busy`/`idle`/`waiting`), and a timestamp
> are ever written/read — never your prompts, code, or session content. TaskPaw
> never enters VSCode.

## 1. The wrapper

`taskpaw_v3/integrations/activity_writer.py` atomically writes one JSON file per
tool. The `dev_activity` monitor reads **`~/.taskpaw/agent-activity-<tool>.json`**,
so pass `--path` accordingly:

```json
{"tool": "claude", "state": "busy", "session": "abc", "ts": 1750000000.0}
```

- `--state busy|idle|waiting` writes that state explicitly.
- With no `--state`, it reads a Claude Code hook payload from stdin and maps the
  `hook_event_name` to a state.
- `--tool` labels the source; `--path` selects that tool's file.

Pick the interpreter that has TaskPaw V3 installed (examples use `python3`).

## 2. Claude Code setup (hooks)

Add to your Claude Code `settings.json` (user or project). Each hook pipes the
event to the wrapper, which auto-detects the state from `hook_event_name`
(`UserPromptSubmit`/`SessionStart`/`PreToolUse`/`PostToolUse` → busy,
`Notification` → waiting, `Stop`/`SubagentStop`/`SessionEnd` → idle). Note the
per-tool `--path`:

```json
{
  "hooks": {
    "UserPromptSubmit": [{ "hooks": [{ "type": "command",
      "command": "python3 /path/to/taskpaw_v3/integrations/activity_writer.py --tool claude --path ~/.taskpaw/agent-activity-claude.json" }] }],
    "SessionStart":     [{ "hooks": [{ "type": "command",
      "command": "python3 /path/to/taskpaw_v3/integrations/activity_writer.py --tool claude --path ~/.taskpaw/agent-activity-claude.json" }] }],
    "Notification":     [{ "hooks": [{ "type": "command",
      "command": "python3 /path/to/taskpaw_v3/integrations/activity_writer.py --tool claude --path ~/.taskpaw/agent-activity-claude.json" }] }],
    "Stop":             [{ "hooks": [{ "type": "command",
      "command": "python3 /path/to/taskpaw_v3/integrations/activity_writer.py --tool claude --path ~/.taskpaw/agent-activity-claude.json" }] }]
  }
}
```

### Windows: write the paths with forward slashes (`/`), not backslashes (`\`)

On Windows, Claude Code runs hook commands through **Git Bash**, where `\` is an
escape character. A backslash path such as `d:\WORKSPACE\Taskpaw\.venv\Scripts\python.exe`
reaches bash as `d:WORKSPACETaskpaw.venvScriptspython.exe`. Every event then fails
with `command not found` (exit 127) and the state file is never written (#206).
Windows accepts forward slashes, so write every path in the command that way:

```json
{
  "hooks": {
    "UserPromptSubmit": [{ "hooks": [{ "type": "command",
      "command": "d:/WORKSPACE/Taskpaw/.venv/Scripts/python.exe d:/WORKSPACE/Taskpaw/taskpaw_v3/integrations/activity_writer.py --tool claude --path ~/.taskpaw/agent-activity-claude.json" }] }]
  }
}
```

Use the same command for every hook event you wire. If a path contains a space,
wrap it in quotes (`\"C:/Program Files/…/python.exe\"` inside the JSON string).

### Check that the hook really writes

A broken hook fails quietly. The monitor treats the old file as stale and falls
back to the CPU-based estimate, so Claude still shows busy/idle, just less
precisely (`~<cpu>%` in the console instead of a hook state). After wiring the
hooks, or after changing a hook command, run the command once through bash with
a sample event:

```bash
echo '{"hook_event_name":"Stop","session_id":"check"}' | bash -c 'd:/WORKSPACE/Taskpaw/.venv/Scripts/python.exe d:/WORKSPACE/Taskpaw/taskpaw_v3/integrations/activity_writer.py --tool claude --path ~/.taskpaw/agent-activity-claude.json'
echo "exit=$?"                              # must be 0
cat ~/.taskpaw/agent-activity-claude.json   # "state": "idle", "session": "check", current ts
```

After that, the file should update every time you send Claude a prompt. If its
time never moves, the hook is failing.

## 3. Codex setup (notify)

Codex fires its `notify` program when a turn ends. In `~/.codex/config.toml`:

```toml
notify = ["python3", "/path/to/taskpaw_v3/integrations/activity_writer.py",
          "--tool", "codex", "--path", "~/.taskpaw/agent-activity-codex.json", "--state", "idle"]
```

Codex invokes `notify` with its event JSON appended as a trailing argument; the
writer ignores unrecognized args (`parse_known_args`), so the command above records
`idle` without erroring (#168).

Unlike the Claude hooks, `notify` is a list of arguments that Codex starts
directly, without a shell, so Windows paths work there as they are. In TOML, write
each backslash twice (`"d:\\WORKSPACE\\…"`) or use forward slashes.

To also flip Codex to **busy** at turn start, wrap your Codex launch (or a shell
alias) to call the wrapper with `--state busy --path ~/.taskpaw/agent-activity-codex.json`
before starting Codex. With only `notify` wired you still get idle-after-completion
and the busy→idle edge — just not the busy edge.

## 4. Kimi (#154 P3)

The Kimi Code CLI has **no hook/notify mechanism** (verified via `kimi --help` —
only `acp`/`server`, no lifecycle events). So Kimi is covered by
**process-presence only**: the `dev_activity` monitor detects the `kimi` process
and reports it as *present*, without busy/idle. If you build your own busy/idle
signal for Kimi, point it at `~/.taskpaw/agent-activity-kimi.json` and it will be
picked up automatically.

## 5. The monitor

Add one **`dev_activity`** monitor on the dev-agent machine — it watches all tools
and aggregates them (busy › waiting › idle › present › none):

```yaml
- type_id: dev_activity
  name: AI activity
  config:
    tools: [claude, codex, kimi, vscode]
    state_dir: ~/.taskpaw          # reads agent-activity-<tool>.json here
    freshness_seconds: 300          # a state file older than this → "unknown" (not idle)
    window_seconds: 1800            # duty ("% busy") window shown in the console
```

It exposes an `ai` block in `/status` (`ai_state`, `busy_tools`, per-tool
`{state, present, age_s}`, and a `duty` ratio) that the agent console and Hub
dashboard render (see `design-system/taskpaw-v3/pages/ai-activity-monitor.md`).
`present` needs no setup; wiring the hooks above upgrades it to precise busy/idle.

## Notes

- moomoo / Hub machines are excluded — this is for dev-agent boxes only.
- Freshness is judged on the agent with its own clock; the Hub compares nothing
  cross-machine (#152).
- The wrapper exits 0 on unknown events so it never breaks the host hook chain.
- The legacy single-file `state_file` monitor still works for a single tool, but
  `dev_activity` is preferred (aggregates + process fallback).
- Compatibility: if a hook omits `--path`, the writer's default
  `~/.taskpaw/agent-activity.json` is also read (matched by its `tool` field), so a
  single-tool default setup still feeds the monitor — but per-tool `--path` is
  recommended when watching more than one tool.
