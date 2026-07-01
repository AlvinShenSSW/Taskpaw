# Hub Dashboard Page Overrides

> **PROJECT:** TaskPaw V3
> **Generated:** 2026-06-27 12:24:33 · **Revised:** 2026-07-02 (layout redesign)
> **Page Type:** Dashboard / Data View
> **Reference mockup:** [`preview/hub-redesign-v3-preview.html`](../preview/hub-redesign-v3-preview.html)
> (toggle: 仪表盘（纵向）/ 事件页 / 管理页 / 当前 3.0 对比)

> ⚠️ **IMPORTANT:** Rules in this file **override** the Master file (`design-system/taskpaw-v3/MASTER.md`).
> Only deviations from the Master are documented here. For all other rules, refer to the Master.

---

## Purpose

The macOS Hub aggregates every polled agent on the LAN. It is an **internal
observability surface**: fleet health at a glance, each machine's live monitors,
an aggregated event history, and the controls to manage which agents are polled.
No marketing hero, no "Start trial" CTA.

## Navigation — four tabs

The Hub is organized as four sibling tabs. The split is deliberate:
**observing and managing are separate concerns**, and events are their own view.

| Tab | Role | Mutating? |
|-----|------|-----------|
| **机群 / Fleet** | The dashboard. Fleet health + one row per machine with live monitors. Read-only. | No |
| **管理 / Manage** | Add / edit / enable / delete polled agents + the polling token. | Yes (CRUD) |
| **事件 / Events** | Aggregated event history across the whole fleet, filterable. | No |
| **设置 / Settings** | Language / about / Hub host config. | — |

Settings **and** Manage stay reachable even when the Hub itself is unreachable
(the agent list may be empty → the add form still shows), same rationale as #87.

---

## Page-Specific Rules

### Layout Overrides

- **Max width:** none / full-width. Data-dense but scannable.
- **机群 (Dashboard) — one full-width row per machine.** NOT a wrapping grid of
  fixed-width cards. Replaces the old 300px `MachineCard` grid.
  - **Row header (single wrapping line):** health dot → machine name → `ip:port`
    → online/disabled chip → *(spacer)* → host **CPU / MEM mini-bars** →
    last-seen (mono, tabular).
  - **Monitors render directly beneath, flush with the header** — no
    click-to-expand, no nested/indented box. Each monitor: state dot + name +
    detail, then its **full metric gauges** (GPU/CPU/MEM/VRAM/queue/fps) shown
    **flush** (no left indent). Monitors separated by thin dividers.
  - **Offline machine:** single header row, no monitor list.
  - **No management controls and no events feed on this page** — the dashboard is
    purely for observing. (Both moved to their own tabs; see below.)
- **管理 (Manage):** a list of registered agents, each row with an enable toggle,
  edit (name / ip / port, inline), and delete (danger color, confirm dialog);
  an "add agent" form; and the polling-token field (password) with Save / Clear.
- **事件 (Events):** aggregated fleet event history (**moved here from the old
  dashboard's bottom feed**). A filter bar (level: info/done/warn/alert · server)
  over a full reverse-chronological timeline. Each line: time (mono) + level
  badge + source machine + message.

### Spacing Overrides

- **Content Density:** High — optimize for information display. Rows are
  comfortable but not sparse; metrics sit inline rather than behind a disclosure.

### Typography / Color Overrides

- Use Master typography and the dark navy palette. Status colors (green/amber/
  red/slate) per `theme.ts statusColors`. Status is **never color-only** — always
  paired with a labelled dot or text (a11y §1).

### Component Overrides

- Always show progress/feedback for async ops (>300ms → skeleton/spinner).
- Auto-refresh the fleet on a 5s poll; the Events tab polls only while open.

---

## Page-Specific Components

- **MachineRow** — full-width row: health dot + name + `ip:port` + online chip +
  host CPU/MEM mini-bars + last-seen, with its monitors + metrics listed flush.
- **FleetHealth** — labelled ok / degraded / offline tally.
- **HubAgentManager** (Manage tab) — CRUD list + add form + polling token.
- **EventLog** (Events tab) — filterable aggregated history.

---

## Recommendations

- Effects: status blink/pulse for starting; smooth 5s data refresh; no layout
  shift on hover.
- Feedback: show spinner/skeleton for operations > 300ms; success/error after
  every mutation on the Manage tab.
- Keyboard/a11y: tab order matches visual order; every control reachable.
