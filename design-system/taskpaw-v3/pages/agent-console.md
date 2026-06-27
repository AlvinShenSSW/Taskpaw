# Agent Console Page Overrides

> **PROJECT:** TaskPaw V3
> **Page Type:** Local control panel (single machine)
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

- **NOT** the Master's "Real-Time / Operations Landing" sections. Use an
  **app-shell two-pane layout**, full app height (`min-h-dvh`), no hero, no CTA
  band:
  - **Left rail (240–280px):** list of this machine's monitor instances
    (Lada / ComfyUI / folder / process / custom), each row showing name + a
    status dot (running / idle / degraded / stopped). Selected row highlighted.
  - **Main pane (fills remaining width):** the selected monitor's live status
    header + Start/Stop controls + schema-driven config form (collapsed by
    default; "Edit config" expands it).
  - **Top bar:** machine name, agent version, connection state to Hub
    (reachable / Bearer ok), and a single global Start-all / Stop-all overflow.
- **Max width:** none — fill the window. (The Master/landing `800px` centered
  rule does **not** apply.)
- **Empty state:** when no monitors are configured, main pane shows a centered
  "No monitors yet — Add monitor" prompt with a primary button (this is the only
  place a prominent CTA appears).

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

- **MonitorRow** (left rail): status dot + name + type chip + last-event time.
- **StatusHeader** (main): big current-state label, live metric line
  (e.g. Lada: file N/M, fps, %; ComfyUI: queue depth), last-updated timestamp
  using tabular figures (`number-tabular`).
- **ConfigForm**: schema-driven, collapsible, with per-field validation.
- **EmptyState**: "No monitors yet" + Add-monitor primary CTA.

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
