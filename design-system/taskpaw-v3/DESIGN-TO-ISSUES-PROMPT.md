# Design → Issues — Prompt Guide for the VSCode agent

**What this is.** A reusable prompt + procedure you paste into your VSCode AI
coding agent (Copilot Chat / Claude / Codex). It teaches the agent to (1) read
this repo's **design system + mockups**, (2) reconcile the design against the
**current V3 code** to find what still needs building, and (3) slice that gap into
**well-formed issues** that respect this repo's rules.

The agent does **not** write feature code from this guide. Its job here is
*understand design → find gaps → produce issues*. Implementation happens later,
one issue at a time.

---

## 0. How to use

1. Open the repo in VSCode with the agent.
2. Decide the target page: `hub-dashboard` or `agent-console`.
3. Paste the **Copy-paste prompt** (bottom of this file) into the agent, setting
   `PAGE` to that page.
4. Review the issues it drafts; then have it run `gh issue create` (only if you
   ask — see guardrails).

---

## 1. Non-negotiable repo rules (the agent must obey these)

Pulled from `AGENTS.md` and `docs/constitution.md`. Treat as blocking.

- **V2 is frozen; V3 is greenfield under `taskpaw_v3/`.** Never refactor V2
  (`taskpaw.py`, `taskpaw_hub.py`, `macsubs.py`) for taste. All new UI work is V3.
- **The agent↔Hub poll protocol is kept and only optimized, not rewritten.**
- **Security invariants** (constitution §2–3): atomic writes, no `shell=True`,
  Bearer auth on cross-machine calls, agent control API is loopback-only, the
  event-id contract, fixed ports. A design must not violate these; if it seems to,
  raise it in the issue instead of quietly breaking an invariant.
- **Every behavioural change needs a test.** Never propose merging on red CI.
- **Don't commit or push unless explicitly asked.** Never deploy.
- **Canonical commands (must appear in each issue's acceptance criteria as
  applicable):**
  - Python: `uv run pytest` · syntax gate `uv run python -m py_compile …` ·
    lockfile `uv lock --check`.
  - V3 UI (`taskpaw_v3/ui/`): `npm ci && npm test && npm run build` (CI runs all
    three; `npm run build` = `tsc -b && vite build`, so **types must pass**).
  - Tauri shell: `cargo test --locked` (only if the shell/backend bundling is
    touched).

---

## 2. How the design is expressed (read in this order)

The design system uses a **precedence chain** — later overrides earlier:

1. `design-system/taskpaw-v3/MASTER.md` — global tokens (dark navy palette, Fira
   fonts, spacing/shadow), component defaults, anti-patterns, a11y checklist.
2. `design-system/taskpaw-v3/pages/<PAGE>.md` — **page overrides. These win over
   MASTER for that page.** This is the authoritative spec.
3. `design-system/taskpaw-v3/preview/<PAGE>-*-preview.html` — a **static visual
   mockup**. It shows *intent and arrangement*, not production code: colors mirror
   `theme.ts`, but it's hand-written HTML/CSS with fake data and toggle buttons to
   compare variants (e.g. 单监控/多监控/当前布局). **Match its layout and
   information hierarchy, not its literal markup.**

If the spec (`pages/*.md`) and the mockup disagree, the **spec wins**; note the
discrepancy in an issue so a human resolves it.

**The `theme.ts` is the real source of tokens** — the mockups use CSS variables
that copy it, but production code must import from `taskpaw_v3/ui/src/theme.ts`.

---

## 3. Where the implementing code lives (build your "current state" from here)

| Concern | Path |
|---|---|
| Hub dashboard view | `taskpaw_v3/ui/src/views/HubDashboard.tsx` |
| Agent console view | `taskpaw_v3/ui/src/views/AgentConsole.tsx` |
| Shared components | `taskpaw_v3/ui/src/components/` (`StatusDot`, `MonitorMetrics`, `EventLog`, `HubAgentManager`, `SchemaForm`, …) |
| API client + types | `taskpaw_v3/ui/src/api.ts` |
| i18n strings (en + zh) | `taskpaw_v3/ui/src/i18n.ts` |
| Theme tokens | `taskpaw_v3/ui/src/theme.ts` |
| Frontend tests | `taskpaw_v3/ui/src/test/*.test.tsx` |
| Backend (FastAPI) | `taskpaw_v3/` (core/, monitors/, packaging/) |
| V3 source of truth | `docs/specs/2026-06-27-taskpaw-v3-design.md` |

---

## 4. The reconciliation procedure

For the target `PAGE`, the agent should:

1. **Read the design:** `MASTER.md` → `pages/<PAGE>.md` → the `preview/*.html`
   mockup(s). Summarize the intended layout, components, and data each needs.
2. **Read the current code:** the matching view + the components/API/i18n it uses.
   Summarize what exists today.
3. **Diff design vs code** and classify every gap into one of these buckets:
   - **FE-layout** — rearranging existing components/data (no new data needed).
   - **FE-component** — a genuinely new UI piece (e.g. a horizontal selector).
   - **Backend/data** — the design needs a field or endpoint that doesn't exist
     yet (e.g. per-monitor last-event time). **Flag these loudly** — they block
     the FE piece that depends on them.
   - **i18n** — new/changed strings (must add **both** `en` and `zh`).
   - **Tests** — behaviour that must be covered or existing tests that must change.
4. **Order by dependency:** backend/data issues before the FE issues that consume
   them. Prefer **thin vertical slices** — each issue independently shippable and
   reviewable; FE-only work must not be blocked behind backend work when it can
   land against the current data.
5. **Preserve invariants:** if a design would touch a security/contract
   invariant, say so explicitly in the issue and propose a compliant approach.

---

## 5. Issue output format

One issue = one concern. Use this template:

```md
### <concise imperative title>   e.g. "Hub dashboard: one full-width row per machine"

**Why / design source**
- pages/<PAGE>.md → <section>
- preview/<PAGE>-…-preview.html → <which toggle/state>
- (one sentence on the user-visible problem this solves)

**Bucket:** FE-layout | FE-component | Backend/data | i18n | Tests
**Size:** S | M | L
**Blocked by:** #<n> (if any)

**Scope**
- Bullet the concrete changes. Name the files.

**Out of scope**
- What this issue deliberately does NOT do (prevents scope creep).

**Acceptance criteria**
- [ ] Behaviour: … (observable, testable)
- [ ] Test: new/updated test in `…test.tsx` (or `uv run pytest` for backend)
- [ ] i18n: en + zh keys added/updated (if any strings)
- [ ] a11y: status not color-only; focus visible; keyboard order matches visual
- [ ] Green CI: `npm ci && npm test && npm run build` (UI) / `uv run pytest` (py)
- [ ] No V2 files touched; no security/contract invariant weakened

**Affected files**
- taskpaw_v3/ui/src/…
```

Repo issue-creation matches `gh` + the existing numbered/reviewer style (see
history: #95, #113, #124; reviewers "Codex 外门 → Kimi 终审"). Example:

```bash
gh issue create \
  --title "Hub dashboard: one full-width row per machine (drop 300px card grid)" \
  --label "v3,frontend,ui" \
  --body-file /tmp/issue-machinerow.md
```

Only run `gh issue create` when the operator asks — otherwise output the drafts
for review.

---

## 6. Worked starter backlog (seed — verify against HEAD before filing)

These are the gaps implied by the two current redesigns. The agent should
**re-derive and refine** them from §4 rather than trusting this list blindly (the
code may have moved). Use it as a model of the right granularity.

**Hub dashboard** (`pages/hub-dashboard.md` + `hub-redesign-v3-preview.html`):
1. *FE-layout* — Replace the wrapping 300px `MachineCard` grid with a full-width
   **`MachineRow`** (one server per row); remove the click-to-expand `Collapse`;
   render monitors + `MonitorMetrics` **flush** (drop the `pl:3` indent). Update
   `hubdashboard.test.tsx` (the old tests assert the button/expand behaviour).
2. *FE-layout + i18n* — Split agent CRUD into its own **Manage tab**: move
   `HubAgentManager` off the fleet page; add tab + `hub.manage` (en/zh).
3. *FE-layout* — **Move the recent-events feed off the dashboard** into the
   **Events tab**; add a **server filter** dropdown there (the `api.hubEvents`
   client already accepts a `server` param — confirm, then wire the UI).

**Agent console** (`pages/agent-console.md` + `agent-redesign-v3-preview.html`):
4. *FE-component* — **Single-monitor hero**: when ≤1 monitor, drop the left rail;
   render the monitor as a full-width hero (status header + controls + metrics).
5. *FE-component* — **MonitorSelector**: horizontal pill selector for the
   multi-monitor case, replacing the tall rail.
6. *FE-layout* — **Inline recent events** on the agent dashboard (reuse
   `EventLog` + `agentEvents()`), filling the previously empty space.
7. *Backend/data* — **Per-monitor last-event time + per-monitor event filter**
   (design shows it; today only whole-list `dataUpdatedAt` + agent-wide
   `agentEvents()` exist). **Blocks the per-pill freshness in #5 and per-monitor
   inline events in #6** — land the API field first, or ship #5/#6 against
   agent-wide data and follow up.

---

## 7. Copy-paste prompt (paste into the VSCode agent)

```
You are working in the TaskPaw repo. Read design and produce ISSUES ONLY — do not
write feature code.

Target page: PAGE = "hub-dashboard"   # or "agent-console"

Follow design-system/taskpaw-v3/DESIGN-TO-ISSUES-PROMPT.md exactly:
1. Read, in order: design-system/taskpaw-v3/MASTER.md,
   design-system/taskpaw-v3/pages/${PAGE}.md, and the matching
   design-system/taskpaw-v3/preview/${PAGE}-*-preview.html mockup.
2. Read the current implementation (the matching view in taskpaw_v3/ui/src/views/
   plus the components, api.ts, i18n.ts, theme.ts, and *.test.tsx it touches).
3. Diff design vs code. Classify each gap: FE-layout / FE-component /
   Backend-data / i18n / Tests. Flag any backend/data dependency loudly.
4. Output a dependency-ordered list of issues using the template in §5, as thin
   vertical slices. Respect the §1 rules: V2 frozen, tests required, both en+zh
   i18n, green CI gates in acceptance criteria, no invariant weakened, and do NOT
   commit/push or run `gh issue create` unless I explicitly say so.

Start by summarizing the design intent and the current state in a few lines, then
list the issues.
```
