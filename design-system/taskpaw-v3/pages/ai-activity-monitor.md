# AI Activity Monitor — Display Design (agent + hub)

> **PROJECT:** TaskPaw V3 · **Feature:** issue #154 (dev-agent「AI 活动」监控)
> **Created:** 2026-07-02
> **Reference:** builds on [`agent-console.md`](agent-console.md) and
> [`hub-dashboard.md`](hub-dashboard.md); those + [`../MASTER.md`](../MASTER.md)
> still govern. This file only adds the AI-activity-specific rendering.

> ⚠️ Rules in `agent-console.md` / `hub-dashboard.md` / MASTER **override where they
> conflict**; this doc specializes the "AI 活动" monitor within them. Dark OLED navy
> palette, Fira Code/Sans, `theme.ts statusColors`, **status never color-only**
> (dot + text), SVG icons only (no emoji), transitions 150–300ms, `prefers-reduced-
> motion` respected — all inherited.

---

## 1. Purpose

Surface, per **agent machine**, whether it is actively running AI (Claude / Codex /
Kimi) — **busy vs idle**, which tools, and a short-window (30 min) duty view. AI runs
**only on agent machines**; the Hub aggregates and displays. Two distinct signals,
never conflated:

- **`ai_state`** — busy / waiting / idle, from each tool's hooks/notify (precise:
  "a task is running").
- **`ai_present`** — VS Code + `claude/codex/kimi` processes alive, from the
  `process` plugin (coarse, config-free: "the tool is open"). **present ≠ busy.**

---

## 2. State model → tokens (single source; reused on both surfaces)

The machine-level headline (from the `最忙者胜` aggregation, freshness-gated):

| Headline | 中文 | dot state (`statusColors`) | live pulse | label rule |
|----------|------|----------------------------|:---------:|------------|
| BUSY | 在跑 AI · `<tools>` | `running` `#22C55E` | yes | list busy tools |
| WAITING | 等待输入 · `<tool>` | `starting` `#38BDF8` | yes | tool needs input |
| IDLE | AI 空闲 | `idle` `#64748B` | no | tools open, none busy |
| PRESENT_ONLY | AI 在场 · 未上报 | `idle` `#64748B` | no | processes up, no hooks wired |
| NONE | 无 AI 活动 | `unknown` | no | all stale/missing |

Per-tool dot uses that tool's own state (busy→`running`, waiting→`starting`,
idle→`idle`, stale/unknown→`unknown`). **Every dot is paired with a text label**
(reuse `StatusDot`, which already carries `aria-label` + optional live pulse).

- `PRESENT_ONLY` is the fix for today's "一直显示空闲" bug: a box with the tools
  running but no hooks configured must read **"AI 在场 · 未上报"**, not "空闲".
- Tool icons: `ServiceIcon` SVG set (add `claude` / `codex` / `kimi` / `vscode`
  marks) — **no emoji** (MASTER anti-pattern).

**Duty (近 30m):** a compact bar (`AiDutyBar`) built from stored state-transition
events — busy segments filled `running` green, idle/gap muted. Caption:
`忙 18m / 30m · 60%` (mono/tabular, Fira Code). Window is configurable; default 30m.

---

## 3. Agent console — the「AI 活动」MonitorHero

The monitor named **"AI 活动"** is just another monitor on this machine, so it obeys
`agent-console.md` exactly:

- **Multi-monitor:** it is one **`MonitorSelector` pill** (dot + "AI 活动" + type
  chip); selecting it (aria-pressed) swaps the hero below.
- **Single-monitor** (a pure dev box that only runs the AI monitor): it is the
  full-width hero directly (no rail), per case A.

**Hero body (fills width; metrics column beside an inline-events column):**

```
┌─ AI 活动 ───────────── ● 在跑 AI · claude ·· 2 分钟前更新 ─┐
│                                                              │
│  工具                              近 30 分钟                 │
│  ● claude   busy    忙 3m      ▉▉▉▁▁▉▉▉▉▁  忙 18m/30m · 60%  │  ← inline
│  ○ codex    idle    12m 前                                    │    events
│  ◍ kimi     在场·未上报  —      主机                          │    column
│  ○ vscode   运行中             CPU 22%  RAM 41%  GPU 5%       │    (state
│                                                              │    changes)
└──────────────────────────────────────────────────────────────┘
```

- **Status header:** large `StatusDot` (live pulse when BUSY/WAITING) + "AI 活动"
  name + **state chip** (在跑AI / 等待输入 / 空闲 / 在场·未上报 / 无活动) +
  last-updated (tabular, relative — "2 分钟前更新"). No Start/Stop controls (this
  monitor observes; it has nothing to start) — the controls slot from MonitorHero
  is omitted, keeping the header shape.
- **工具 sub-rows:** one row per tool = `StatusDot` + tool `ServiceIcon` + name +
  state word + **relative time** ("忙 3m" / "12m 前" / "—"). Stale tool → `unknown`
  dot + "未上报". `ai_present`-only tool → "在场·未上报".
- **近 30m `AiDutyBar`** + caption, right of the tool list.
- **主机 vitals:** reuse `MonitorMetrics` (CPU/RAM/GPU gauges) from the machine's
  `host_metrics` — so a "CPU 低 but AI busy" contradiction is visible in one place.
- **Inline recent events** (right column): AI state transitions only
  (busy→idle→waiting), reusing `EventLog`, filtered to this monitor (per the
  agent-console deferred per-monitor-events note).

**Interaction (inherits agent-console):** 5s poll; subtle "updating" affordance
(not a blocking spinner); live pulse degrades to static glow under reduced-motion;
pill row + hero fully keyboard-operable; selection paired with `aria-pressed`.

---

## 4. Hub dashboard —「机群」MachineRow integration

Per `hub-dashboard.md`, the 机群 tab is **one full-width `MachineRow` per machine**,
monitors flush beneath. AI activity appears in **two places** — glance + detail:

**(a) Row header — an `AiActivityBadge` (the fleet glance).**
Inserted into the single wrapping header line, right after the online/disabled chip
(before the CPU/MEM mini-bars):
```
● dev-mac  192.168.1.50:5680  [online]  [● 在跑AI·claude]   CPU▉▂ MEM▉▍  16:41
● build-01 192.168.1.51:5680  [online]  [○ 无AI活动]        CPU▁  MEM▂   16:41
```
- Badge = small `StatusDot` + short label (在跑AI·claude / 等待输入 / AI空闲 /
  在场·未上报 / 无AI活动). This answers "哪台在跑 AI" without scanning monitors.
- Offline machine: header only, **no AI badge** (consistent with "offline → single
  header row").

**(b) Monitor line — the「AI 活动」row (the detail), flush beneath the header** like
any monitor, no indent/disclosure:
```
   ● AI 活动   在跑 AI · claude          claude ● busy  codex ○ idle  kimi ◍ 在场
                                          近30m ▉▉▉▁▁▉▉▉▉▁  忙18m/30m·60%
```
- state dot + "AI 活动" + detail (headline text), then its "metrics" region =
  **per-tool chips** (dot + tool + state) + the **`AiDutyBar` (近30m)** shown flush
  (the AI monitor's analogue of GPU/CPU gauges). Separated from other monitors by
  the same thin divider.

**Interaction (inherits hub-dashboard):** 5s fleet poll; no controls on the
dashboard; status blink/pulse for busy; no layout shift on hover; a11y tab order.

---

## 5. Data contract (what the agent's status provider exposes)

The agent computes the aggregation locally and exposes it as **flat keys inside the
`dev_activity` monitor's `metrics`** (the standard monitor snapshot the Hub stores +
forwards and the UI renders — no client-side timezone math, per #152). The UI's
`isAiMetrics` keys off `metrics.ai_state`:

```jsonc
"metrics": {
  "ai_state": "busy",              // busy|waiting|idle|present_only|none
  "busy_tools": ["claude"],        // tools currently busy (for the label)
  "tools": [
    {"tool":"claude","state":"busy","present":true,"age_s":180,"ai":true},
    {"tool":"codex","state":"idle","present":true,"age_s":720,"ai":true},
    {"tool":"kimi","state":null,"present":true,"age_s":null,"ai":true},
    {"tool":"vscode","state":null,"present":true,"age_s":null,"ai":false}
  ],
  "window_s": 1800,                 // duty window (s)
  "duty": {"busy_s": 1080.0, "ratio": 0.60}
}
```
Notes on the shipped contract: `state` is `busy|waiting|idle` when fresh, else
`null` (stale/missing → unknown). `ai` is false for context tools (VS Code) whose
presence alone must not read as "AI running". **`duty` is an in-memory, sampled
approximation** (busy-sample fraction over `window_s`, computed each poll); it
resets on agent restart and carries no `segments` — a persisted, segment-accurate
duty (from stored transitions) is a possible later enhancement.

`age_s` (seconds since that tool's last event, computed on the agent) drives the
relative-time labels; the UI formats "忙 3m" / "12m 前". Duty is derived on the Hub
from stored transitions (or precomputed by the agent for the current window).

---

## 6. New / reused components

| Component | Surface | Reuse |
|-----------|---------|-------|
| `AiActivityHero` | agent | wraps `StatusDot` + tool rows + `AiDutyBar` + `MonitorMetrics` + `EventLog` in the MonitorHero shell |
| `AiActivityBadge` | hub header | `StatusDot` + short label chip |
| `AiActivityLine` | hub monitor row | tool chips + `AiDutyBar`, in the MachineRow monitor slot |
| `AiDutyBar` | both | small segmented bar + mono caption (new; shared) |
| `ServiceIcon` (+claude/codex/kimi/vscode) | both | extend existing SVG set |

---

## 7. a11y + anti-pattern checklist (per MASTER)

- [ ] Status never color-only — every dot has a text label + `aria-label`.
- [ ] SVG tool icons (ServiceIcon), **no emoji**.
- [ ] Live pulse (busy/waiting) degrades under `prefers-reduced-motion`.
- [ ] Contrast ≥ 4.5:1 for labels/captions on dark bg.
- [ ] Duty bar has a text caption (not a bar-only signal).
- [ ] 5s refresh uses a subtle "updating" affordance, no layout shift.
- [ ] "AI 在场·未上报" clearly distinguished from "AI 空闲" (the core bug fix).
- [ ] Keyboard: agent pills/hero + hub rows in visual tab order.
