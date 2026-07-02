# Design: #163 external-probe real busy/idle capture for dev tools

Date: 2026-07-03 · Branch: `afk/163-ai-activity-real-capture`

## Problem
`dev_activity` (#154) can only tell **present** (process running) vs **state**
(busy/idle) — and state exists only when the user manually wired a hook (Claude
`settings.json`, Codex `notify`). Kimi has no hooks; VS Code is context-only. So in
practice the UI shows "present (unreported)" and never real busy/idle. The operator
wants **真实捕捉**: know whether Claude / Codex / Kimi / VS Code are actually working,
**without the tool cooperating and with zero impact on it** (an external probe).

## Approach — layered, precedence: hook-state > observed > presence
1. **hook state (P2, existing)** — `read_tool_state`, unchanged. Most accurate when
   wired (covers the model-thinking phase). Wins when fresh.
2. **observed activity (P-obs, NEW)** — when there is no fresh hook state, classify
   busy/idle from **CPU usage of the tool's process subtree**, sampled between checks.
   Idle CLI at a prompt ≈ 0% CPU; actively generating / running a tool (child procs
   consuming CPU) raises the subtree CPU. Pure external `psutil` read — no writes to,
   or wrapping of, the tool (addresses "外部探针,对工具无影响").
3. **presence (P1, existing)** — `_detect_present`, the final "present" fallback.

### Why CPU (not hooks-only, not child-count)
- **Universal**: works for all four tools with no per-tool cooperation, incl. Kimi
  (no hooks) and VS Code (editor).
- **Child-count rejected**: CLIs may keep persistent helper children (MCP servers);
  VS Code always has many helpers — child *presence* is not "busy". Child *CPU* is,
  and it's already included in the subtree CPU sum.
- **Known limit (documented)**: a pure network wait (model streaming) is low local
  CPU, so CPU alone may briefly read idle during "thinking". Hooks (P2) cover that
  phase; the two are complementary. Tool-execution and rendering do show CPU.

## Mechanics
- `process_util.scan_activity(patterns)` — ONE `process_iter(["pid","ppid","name",
  "cmdline","cpu_times"])` sweep. Builds a ppid→children index, and for each tool's
  matched **root** pids walks the subtree (bounded) summing `cpu_times` (user+system)
  seconds. Returns `{tool: {"present": bool, "cpu_seconds": float}}`. Stateless; pure
  read; per-proc `psutil.Error` swallowed.
- `dev_activity` instance keeps `_prev_cpu {tool: cpu_seconds}` + `_prev_mono`.
  `_observe()` computes `pct = 100 * max(0, cur - prev) / elapsed` per tool (first
  sample → 0; clamp negatives from exited children). No root (`cpu_seconds is None`)
  → observation unavailable → None.
- `check()`: `present` from `_detect_present` (unchanged). If `observe` and a tool is
  present with **no fresh hook state**, set its `state = "busy" if pct >=
  busy_cpu_percent else "idle"`, and add `cpu` + `observed:true` to the row.
- `aggregate()`: the machine headline (busy/waiting/idle) counts **AI tools only**;
  VS Code (context, `ai=false`) shows its observed state in its own row but never
  drives the "AI busy" headline (it's an editor, not an AI). `present_only` was
  already AI-gated.

## Config (`DevActivityConfig`, backward-compatible)
- `observe: bool = True` — enable the external CPU probe.
- `busy_cpu_percent: float = 8.0` (gt=0) — subtree CPU% at/above which observation
  reports busy.

## No-root / degrade
Per-process socket-style constraints don't apply (CPU/cmdline of same-user procs need
no root on macOS). Any psutil fault degrades observation to "unavailable" (falls back
to presence), never crashes the check (like the existing presence path).

## UI
`AiActivity.tsx` already renders `tool.state` (busy/idle dot + label). Observed state
flows through as busy/idle, so the row now shows real activity instead of "present
(unreported)". Add a subtle "~" / "观测" hint + CPU% for observed rows so the operator
can tell probe-derived from hook-reported. `ai_state` headline unchanged in shape.

## Tests
- `scan_activity`: subtree CPU summed over children; foreign procs excluded; psutil
  missing/denied/zombie degrade.
- CPU-delta: first sample 0; second sample busy vs idle; exited-child clamp.
- `check()` integration: present + no hook state + high CPU → busy; low → idle; hook
  state still wins; VS Code observed-busy does NOT set the AI headline.
- Existing presence/aggregate/duty/emit tests preserved (observe=False where they
  assert the pure present_only path).

## Constraints
- V3 only; `docs/constitution.md`. UI follows `design-system/taskpaw-v3/`. AI runs only
  on agents; Hub aggregates (#154).

## Follow-up (out of scope here)
Optional `hooks install/uninstall` command to auto-wire Claude/Codex hooks for the
most accurate P2 signal — the observation probe already gives out-of-the-box real
capture, so hook wiring is an accuracy enhancement, not a prerequisite.
