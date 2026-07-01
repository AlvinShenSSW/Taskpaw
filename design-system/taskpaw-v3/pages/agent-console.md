# Agent Console Page Overrides

> **PROJECT:** TaskPaw V3
> **Page Type:** Local control panel (single machine)
> **Revised:** 2026-07-02 — density-adaptive layout (optimize the common
> single-monitor case). **Reference mockup:**
> [`preview/agent-redesign-v3-preview.html`](../preview/agent-redesign-v3-preview.html)
> (toggle: 单监控 / 多监控 / 当前布局).
> **Hand-corrected:** the auto-generated draft picked a "Lead Magnet + Form"
> landing pattern, which is wrong for this surface. The agent console is a
> desktop control panel for ONE machine's monitors — not a marketing page.

> ⚠️ **IMPORTANT:** Rules in this file **override** the Master file
> (`design-system/taskpaw-v3/MASTER.md`). Only deviations from the Master are
> documented here. For everything else (dark OLED style, navy palette, Fira
> Code/Sans, spacing/shadow tokens, anti-patterns), use the Master.

---

## Purpose

The console the operator opens on the machine an agent runs on (Lada box,
ComfyUI box). It controls **this machine only** — start/stop monitors, edit a
monitor's config, watch its live status. It does **not** aggregate other
machines (that's the Hub dashboard) and exposes **no** OpenClaw token. Per V3
§7, the agent control API is loopback-only.

## Page-Specific Rules

### Layout Overrides

**The layout adapts to how many monitors this machine runs.** In practice an
agent usually watches **one** process, so the old fixed two-pane (skinny 240–280px
rail + wide detail) leaves the rail nearly empty and the detail pane sparse. Pick
the form by monitor count:

**A. Single monitor (the common case) — full-width "hero", no rail.**
- Drop the left rail entirely. The one monitor becomes a full-width hero that
  fills the window with useful, live information:
  - **Status header:** large status dot + monitor name + type chip + state chip +
    last-updated (tabular).
  - **Controls:** Start/Stop (primary) + Edit config; Delete separated to the far
    side (danger).
  - **Live metrics dashboard:** now-processing banner (current file + progress),
    utilization gauges (GPU/CPU/MEM with GB sub-labels), queue/VRAM bars, and
    fps/ETA/count tiles — laid out to fill the width (e.g. a metrics column beside
    an **inline recent-events** column, so the space reads as full, not padded).
  - Recent events are shown **inline on the dashboard** (not only behind the
    Events tab), since there's room and it's the most useful fill.

**B. Multiple monitors — horizontal selector + the same hero.**
- Replace the tall rail with a **horizontal segmented selector** at the top: a
  row of pills, each = status dot + name + type chip, plus an "+ Add monitor"
  pill. Selected pill highlighted (accent border + wash, paired with the dot).
- The selected monitor renders below in the **same hero** as case A.
- (The classic vertical rail is acceptable only if the list grows large enough
  that a wrapping pill row becomes unwieldy — treat that as the exception.)

**Shared:**
- Full app height (`min-h-dvh`), **no hero-marketing band, no CTA band.**
- **Top bar:** machine name, agent version, connection state to Hub (reachable /
  Bearer ok).
- **Max width:** none — fill the window. (The Master/landing `800px` centered
  rule does **not** apply.)
- **Empty state:** when no monitors are configured, the pane shows a centered
  "No monitors yet — Add monitor" prompt with a primary button (the only place a
  prominent CTA appears).

### Spacing / Density Overrides

- **Density:** medium — denser than a landing page, lighter than the Hub
  dashboard. Left-rail rows ~40–44px tall (touch-min), main-pane sections use
  Master `--space-md`/`--space-lg`.

### Color Overrides

- Use Master navy/dark palette. Add **semantic status colors** for the monitor
  state dots and badges (do not invent per-component hex — define as tokens):
  - running/healthy → success green; idle/neutral → muted/slate;
    degraded/warning → amber; stopped/error → `--color-destructive`;
    starting/transition → accent blue (pulse).
- Status must **never be conveyed by color alone** — pair the dot with a text
  label (`color-not-only`).

### Component Overrides

- **Schema-driven config form** (per V3 §4.3): render from the plugin's
  json_schema subset; visible labels (not placeholder-only); validate on blur;
  show inline errors below the field; secret fields use a password widget and
  display `***` for stored values (never echo the real secret).
- **Start/Stop** are the primary controls — show loading state during the async
  transition, then settle to the new status; never let a button look tappable
  while it does nothing (`disabled-states`).
- **Destructive actions** (delete monitor) use the danger color, are spatially
  separated from Start/Stop, and require a confirm dialog.

---

## Page-Specific Components

- **MonitorSelector** (multi-monitor): horizontal pill row — each pill = status
  dot + name + type chip; selected highlighted; trailing "+ Add monitor" pill.
  Replaces **MonitorRow**/left-rail for the common small-N case.
- **MonitorHero** (main): large status header (dot + name + type/state chips +
  last-updated tabular) + controls + the live metrics dashboard, sized to fill
  the width; recent events shown inline alongside the metrics.
- **StatusHeader / live metric line** (e.g. Lada: file N/M, fps, %; ComfyUI:
  queue depth) — now the header of MonitorHero.
- **ConfigForm**: schema-driven, collapsible, with per-field validation.
- **EmptyState**: "No monitors yet" + Add-monitor primary CTA.

> **Deferred data note:** the design shows a **last-event time per monitor** (on
> selector pills) and **per-monitor inline events**. Today the agent exposes only
> a whole-list `dataUpdatedAt` and an agent-wide `agentEvents()` feed. Surfacing
> per-monitor freshness / filtering events by monitor needs a backend field —
> track as its own issue rather than faking it client-side.

---

## Recommendations

- Live status updates over the local control API/WebSocket; show a subtle
  "updating" affordance, not a blocking spinner, for sub-second refreshes.
- Respect `prefers-reduced-motion`: the starting-state pulse degrades to a static
  color.
- Keyboard: left/right pane focus order matches visual order; Start/Stop and the
  monitor list are fully keyboard-operable.
- This view shares the Master design system with the Hub dashboard so the two
  role-views feel like one product; only layout/density differ.
