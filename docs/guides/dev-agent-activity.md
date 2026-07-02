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
- **state** — precise busy/idle/waiting from a small JSON file each CLI writes
  through the `activity_writer.py` wrapper (below).

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

## 3. Codex setup (notify)

Codex fires its `notify` program when a turn ends. In `~/.codex/config.toml`:

```toml
notify = ["python3", "/path/to/taskpaw_v3/integrations/activity_writer.py",
          "--tool", "codex", "--path", "~/.taskpaw/agent-activity-codex.json", "--state", "idle"]
```

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
