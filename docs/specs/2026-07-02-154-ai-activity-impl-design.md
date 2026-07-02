# Design: #154 AI-activity monitor — implementation (P1+P2+P3)

Date: 2026-07-02 · Issue: #154 · Branch: `feat/154-ai-activity-monitor`
Display design: [`design-system/taskpaw-v3/pages/ai-activity-monitor.md`](../../design-system/taskpaw-v3/pages/ai-activity-monitor.md)

Make a dev/agent machine's AI activity visible (it showed only "idle"). AI runs
**only on agents**; the Hub aggregates + displays. Built as one self-describing
monitor plugin, so no core/UI plumbing changes.

## Backend — `dev_activity` plugin (`taskpaw_v3/monitors/plugins/dev_activity.py`)
Registered in the default registry (operator-selectable, `system=False`, passive →
auto-starts). `check()` produces a `MonitorStatus` whose `metrics` carry the `ai`
block the UI renders.

- **P1 — process presence (config-free):** one psutil sweep (reusing
  `process._scan`) matches each tool's pattern (claude/codex/kimi + a broad VS Code
  pattern). `present=True/False` per tool. Degrades to all-absent if psutil is
  missing. **This alone stops a busy dev box reading as idle.**
- **P2 — precise busy/idle:** reads `<state_dir>/agent-activity-<tool>.json`
  (written by `integrations/activity_writer.py` via each CLI's hooks/notify).
  Freshness is judged **on the agent with its own clock** (`time.time() - ts`) —
  never a cross-machine compare (#152). Stale/missing → `unknown` (never silently
  `idle`), so a crashed "busy" can't stick.
- **Aggregation (`最忙者胜`):** busy › waiting › idle › present_only › none, plus
  `busy_tools`. Mapped to the generic `MonitorStatus.state` (busy/waiting→running,
  idle/present_only→idle, none→unknown); the rich headline lives in `metrics.ai_state`.
- **Duty:** an in-instance sample ring (one per `check`) → `ratio`/`busy_s` over
  `window_seconds` (default 1800). Emits an event only on the busy↔not-busy edge.
- **Privacy:** only tool + state + timestamps; never prompts/code/session content.

## P3 — Kimi
Verified `kimi --help`: the Kimi Code CLI has **no hook/notify mechanism** (only
`acp`/`server`). So Kimi is covered by **process presence only** (the documented
fallback); if the operator builds their own signal, `agent-activity-kimi.json` is
picked up automatically.

## Docs — `docs/guides/dev-agent-activity.md`
Rewritten for the `dev_activity` monitor: per-tool `--path` convention, Claude
hooks, Codex notify, the Kimi finding, and the monitor YAML.

## UI (`taskpaw_v3/ui`)
- **`AiActivity.tsx`** — renders the `ai` block: headline (StatusDot + label,
  busy/waiting live-pulse), per-tool rows (dot + tool + busy/idle/present + age),
  and a duty bar with a text caption. `AiBadge` is the compact header variant.
  Status is never colour-only. i18n en/zh under `ai.*`.
- **`MonitorMetrics`** delegates to `AiActivity` when `metrics.ai_state` is present
  — so the agent console hero, Hub machine-row monitor line, and Hub self-monitor
  all render it with no extra plumbing.
- **Hub `MachineRow` header** shows an `AiBadge` (the fleet glance: which machine
  is running AI).

## Tests
- `test_dev_activity.py` (9): read_tool_state fresh/stale/missing/malformed;
  aggregation (busy>waiting>idle>present_only>none); check() headline+metrics;
  present_only≠idle; none; busy-edge emit; duty ratio.
- `aiactivity.test.tsx` (5): isAiMetrics; busy render + tools + present_only;
  MonitorMetrics delegation; AiBadge.

## Constitution gate
- §1 Scope: V3 only; V2 frozen/untouched. §2: no secrets; privacy-preserving.
- §4 Reliability: psutil/file errors degrade to unknown, never crash the check.
- §5 Testing: each behaviour covered; ruff/ruff-format/mypy/eslint + pytest/vitest
  all green.
