# AGENTS.md — TaskPaw

Guide for AI agents (Claude, Codex, Kimi) and humans working in this repo. Read
this first, then the [constitution](docs/constitution.md) for hard rules.

## What this project is

TaskPaw monitors AI/automation tasks and long-running services across machines on
a LAN and notifies an OpenClaw assistant when something completes or breaks.

Three-tier architecture (V2, current):

```
Windows/macOS agent (taskpaw.py, :5678)  ──HTTP poll──►  Hub (taskpaw_hub.py, macOS)
  monitors Lada / ComfyUI / folder / process / custom      ├─ SQLite history (~/.taskpaw-hub/hub.db)
  REST: /ping /status /events (Bearer-gated)               └─ POST ──► OpenClaw (:18789)
MacSubs (macsubs.py, :5679)  ──poll──────────────────────────┘   (being retired in V3)
```

## Repo layout

| Path | What |
|------|------|
| `taskpaw.py` | V2 agent — tkinter GUI + all watcher logic. Entry: `python taskpaw.py`. Config: `%APPDATA%\TaskPaw\config.json` (Win) / `~/Library/...` fallback. `APP_VERSION` 2.7.1. |
| `taskpaw_hub.py` | V2 Hub (macOS) — polling, SQLite, OpenClaw forwarding, tkinter dashboard. Data: `~/.taskpaw-hub/hub.db`. |
| `macsubs.py` | macOS subtitle-translation microservice exposing the same poll API. **Dropped from V3 monitoring.** |
| `taskpaw_v3/` | **V3 monorepo (greenfield, scaffolded).** FastAPI backend + agent (`agent/`) + hub (`hub/`) + `core/` + monitors-as-plugins (`monitors/`) + React/Vite UI (`ui/`) + Tauri shell (`src-tauri/`) + migration tooling (`migrate/`) + `integrations/`, `packaging/`. Tests in `taskpaw_v3/tests/` (22 files). |
| `docs/specs/` | Design docs. **`2026-06-27-taskpaw-v3-design.md` is the V3 source of truth.** |
| `docs/guides/` | Operational guides — deployment, macOS/Windows signing, OpenClaw integration, dev-agent activity. |
| `docs/constitution.md` | Hard rules every change is checked against. |
| `scripts/` | Agent/Hub setup helpers (`setup-agent*`, `setup-hub*`), `build.py`, misc tooling. |
| `design-system/taskpaw-v3/` | Generated UI/UX design system (MASTER + page overrides) for the V3 frontend. |
| `skill/` | In-repo skills shipped with the code: `afk`, `codex-review`, `kimi-review`, `cto-pr-review`, `implementation-pilot`, `spec-planner`, `ui-ux-pro-max`. Each `SKILL.md` is self-contained. |
| `BUG_AUDIT.md`, `CODE_AUDIT_REPORT.md`, `CODEX_AUDIT_FINDINGS.md`, `CHANGELOG.md` | Audit history; most P0/P1/P2 fixed in v2.7. |
| `tests/` | pytest suite (smoke-level today; grow per-issue). |

## Status: V2 vs V3

- **V2 = frozen** (critical fixes only). Don't add features or refactor V2 for taste.
- **V3 = greenfield** under `taskpaw_v3/` (monorepo, now scaffolded): Tauri v2 +
  React 19/Vite/MUI + FastAPI backend; monitors are self-describing plugins;
  agent↔Hub poll protocol is **kept and only optimized**, not rewritten. The tree
  already carries agent/hub/UI/Tauri shell, migration tooling, and a backend test
  suite; work continues per the V3 design doc. First new scenario: monitor the
  moomoo (MQT) trading server's four life-signs.

## Commands

This repo uses **uv**. (System `python3` may be 3.9; uv provides 3.10+.)

```bash
uv sync --group dev              # create/refresh the dev environment
uv lock --check                  # lockfile must be current (CI enforces this)
uv run pytest                    # run tests  ← canonical test command for THIS repo
uv run python -m py_compile taskpaw.py taskpaw_hub.py macsubs.py   # syntax gate
```

> `uv run pytest` is the whole story here — there is no `web` extra. Optional
> extras are `tray` (GUI) and `build` (PyInstaller), neither needed for tests.

Run the apps (manual / desktop):

```bash
python taskpaw.py        # agent (Windows primary; tkinter)
python3 taskpaw_hub.py   # Hub (macOS)
```

Package: `build.bat` (Windows .exe), `build_hub.sh` (macOS).

## Conventions

- Python ≥ 3.10. Standard library first; runtime deps limited to `psutil`
  (always) and `pystray`/`Pillow` (the optional `tray` extra, GUI only).
- Match the surrounding file's style. V2 files are large single-module scripts by
  design — keep new V2 fixes localized; do real restructuring only in V3.
- See [docs/constitution.md](docs/constitution.md) for security/reliability
  invariants (atomic writes, no `shell=True`, Bearer auth, clean shutdown,
  event-id contract, ports). Treat those as blocking.

## Working agreement for agents

- Confirm/keep to the operator's scope; never pick work yourself in AFK mode.
- Every behavioural change needs a test; never open a PR on red CI.
- Reviewer ≠ implementer (Codex 外门 → Kimi 终审 under default `/afk`); flag any
  degraded review to the operator.
- Don't commit/push unless asked. Never deploy (merge ≠ deploy).
