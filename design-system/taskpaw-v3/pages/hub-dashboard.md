# Hub Dashboard Page Overrides

> **PROJECT:** TaskPaw V3
> **Generated:** 2026-06-27 12:24:33
> **Page Type:** Dashboard / Data View

> ⚠️ **IMPORTANT:** Rules in this file **override** the Master file (`design-system/MASTER.md`).
> Only deviations from the Master are documented here. For all other rules, refer to the Master.

---

## Page-Specific Rules

### Layout Overrides

- **Max Width:** 1400px or full-width
- **Grid:** 12-column grid for data flexibility
- **Regions (not landing sections):** 1. Top bar (title, global health summary, refresh/connection state), 2. Fleet grid — one card per machine/agent with status + key live metrics, 3. Selected-machine detail (its monitors + recent events), 4. Events/alerts feed. No marketing hero, no "Start trial" CTA — this is an internal observability surface.

### Spacing Overrides

- **Content Density:** High — optimize for information display

### Typography Overrides

- No overrides — use Master typography

### Color Overrides

- **Strategy:** Dark or neutral. Status colors (green/amber/red). Data-dense but scannable.

### Component Overrides

- Avoid: No indication of progress
- Avoid: No feedback after submit
- Avoid: No feedback during loading

---

## Page-Specific Components

- No unique components for this page

---

## Recommendations

- Effects: Real-time chart animations, alert pulse/glow, status indicator blink animation, smooth data stream updates, loading effect
- Feedback: Step indicators or progress bar
- Forms: Show loading then success/error state
- Feedback: Show spinner/skeleton for operations > 300ms
- CTA Placement: Primary CTA in nav + After metrics
