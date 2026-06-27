# Dev-agent activity monitor (Claude Code / Codex busy vs idle)

*(Optional — V3 design §5c, issue #22. Dev-agent machines only.)*

Surface whether the Claude Code / Codex running in your VSCode is **busy**
(running a task), **idle** (waiting at the prompt), or **waiting** (needs your
input). It works by having each CLI emit its own lifecycle events through a tiny
wrapper that writes a state file, which the V3 **`state_file`** monitor reads.

> **Privacy:** only the state (`busy`/`idle`/`waiting`) and a timestamp are ever
> written — never your prompts, code, or session content. TaskPaw never enters
> VSCode.

## 1. The wrapper

`taskpaw_v3/integrations/activity_writer.py` atomically writes, by default,
`~/.taskpaw/agent-activity.json`:

```json
{"tool": "claude", "state": "busy", "session": "abc", "ts": 1750000000.0}
```

- `--state busy|idle|waiting` writes that state explicitly.
- With no `--state`, it reads a Claude Code hook payload from stdin and maps the
  `hook_event_name` to a state.
- `--tool` labels the source; `--path` lets each tool use its own file.

Pick the interpreter that has TaskPaw V3 installed (examples use `python3`).

## 2. Claude Code setup (hooks)

Add to your Claude Code `settings.json` (user or project). Each hook just pipes
the event to the wrapper, which auto-detects the state from `hook_event_name`
(`UserPromptSubmit`/`SessionStart` → busy, `Notification` → waiting,
`Stop`/`SubagentStop` → idle):

```json
{
  "hooks": {
    "UserPromptSubmit": [{ "hooks": [{ "type": "command",
      "command": "python3 /path/to/taskpaw_v3/integrations/activity_writer.py --tool claude" }] }],
    "SessionStart":     [{ "hooks": [{ "type": "command",
      "command": "python3 /path/to/taskpaw_v3/integrations/activity_writer.py --tool claude" }] }],
    "Notification":     [{ "hooks": [{ "type": "command",
      "command": "python3 /path/to/taskpaw_v3/integrations/activity_writer.py --tool claude" }] }],
    "Stop":             [{ "hooks": [{ "type": "command",
      "command": "python3 /path/to/taskpaw_v3/integrations/activity_writer.py --tool claude" }] }]
  }
}
```

## 3. Codex setup (notify)

Codex fires its `notify` program when a turn ends. In `~/.codex/config.toml`:

```toml
notify = ["python3", "/path/to/taskpaw_v3/integrations/activity_writer.py",
          "--tool", "codex", "--path", "~/.taskpaw/codex-activity.json", "--state", "idle"]
```

To also flip Codex to **busy** at turn start, wrap your Codex launch (or use a
shell alias) to call the wrapper with `--state busy` before starting Codex. If
you only wire `notify`, the monitor still shows idle-after-completion and the
busy→idle `done` event, just without the busy edge.

## 4. The monitor

Add a `state_file` monitor on the dev-agent machine (one per tool if you split
the files):

```yaml
- type_id: state_file
  name: Claude Code (VSCode)
  config:
    path: ~/.taskpaw/agent-activity.json
    busy_alert_seconds: 1800   # alert if "busy" for >30 min (possibly stuck)
    stale_seconds: 0           # 0 = off; set if the wrapper updates periodically
```

State mapping: busy → `running`, idle → `idle`, waiting → `idle` (with a
"waiting" detail). Transitions emit events (started / waiting / finished), and
the watchdogs alert on busy-too-long and (if enabled) a stale file.

> `stale_seconds` only helps if something refreshes the file on a cadence; the
> hooks above are edge-triggered, so leave it at `0` unless you add a periodic
> heartbeat writer.

## Notes

- moomoo / Hub machines are excluded — this is for dev-agent boxes only.
- The wrapper exits 0 on unknown events so it never breaks the host hook chain.
