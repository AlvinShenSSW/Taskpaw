# Task Logs Page Overrides

Agent Console, issue #196. Follow MASTER.md and agent-console.md; use the Hub
Events tab's dense reverse-chronological list precedent. This local-only page
replaces the agent Events tab. The Hub retains its own Events tab.

## Layout and density

- Full-width card with a wrapping filter bar, status feedback, timeline, and
  “Load earlier” button. No marketing header or fixed-width columns.
- Medium/high density: structured rows use 8px vertical padding; compact alert
  and inline rows use 4px. Use theme spacing, colors, and Fira typography.
- At 375/768/1024/1440px, filters wrap and text breaks within the available width;
  long filenames, model labels, and tails never force horizontal scrolling.

## Filters and navigation

- Logs / 日志 replaces Events in the agent console. It stays reachable when the
  status endpoint fails. Poll every five seconds only while mounted.
- Visible labels: Day (Today / Yesterday / retained dates with counts), Task,
  Severity (Info / Warning / Error / warnings and errors), Search film/model/title.
- Filters use server-side queries. A changed selection clears the old result.
- Newest entries appear first, grouped by the agent's local calendar date. The
  open Today timeline continues across midnight, with a new date header; explicit
  historical days stay scoped. Export uses the selected calendar day.

## List anatomy

- Date header, then each row: HH:MM:SS in tabular mono, a text severity chip,
  task name and type, one localized sentence, and the film name when present.
- Mirrored alerts are secondary compact “Alert / 提醒” rows: title only until
  expanded. They use text.secondary, never reduced opacity or color alone.
- Details are a keyboard-operable labelled button with aria-expanded and a
  minimum 40px target. Expanded definition lists expose documented fields only:
  process/PID, exit code/tail, model/counts, and original alert text.
- Unknown kinds show their kind and safe key fields. Inferred interruptions say
  “may have been interrupted / 可能中断”; unclean starts identify the prior exit.
- The monitor dashboard uses the same compact renderer for its latest eight
  entries across days, titled “Recent logs / 最近日志”.

## States and export

- Initial loading shows progress; background requests use a quiet status line.
- Empty results explain that no entries match. Failures show a localized alert
  with retry; existing rows remain readable after a failed poll or older-page fetch.
- Disable paging while a request is active, and export while loading/exporting.
- Export fetches the whole filtered selected day, up to 20,000 rows, as localized
  UTF-8 text via a download link. Surface the cap, export failure, or restart.
- Boot changes discard stale entries and cursors. Filter changes and unmounts
  ignore late responses. Deduplicate by ID and compare numeric ID suffixes.

## Accessibility and safety

Use theme semantic colors with text labels, visible MUI focus, native labelled
selects, wrapping layouts, and inherited reduced-motion behavior. No custom
motion, emoji icons, layout-shifting hover, or new colors. All controls remain
keyboard accessible. Never display arbitrary config/argv/secret fields; use
documented log fields and credential redaction for display and export.
