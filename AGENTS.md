# AGENTS.md — TaskPaw

Guide for AI agents (Claude, Codex, Kimi) and humans working in this repo. Read
this first, then the [constitution](docs/constitution.md) for hard rules.

## What this project is

TaskPaw monitors AI/automation tasks and long-running services across machines on
a LAN and notifies an OpenClaw assistant when something completes or breaks.

The implemented V3 tree uses FastAPI services, self-describing monitor plugins,
React/Vite UI and a Tauri v2 desktop shell. Agent→Hub polling retains the existing
`/ping`, `/status`, `/events` protocol; the Hub stores history/outbox data in SQLite
and forwards notifications to OpenClaw.

```text
Windows/macOS V3 Agent (:5680 network, :5681 local control)
  plugins + task logs + local UI ──HTTP poll──► V3 Hub (:5690)
                                               ├─ SQLite history/outbox
                                               └─ OpenClaw (:18789)
```

These are baseline defaults, configurable under the constitution's bind/auth
rules. The [dated audit follow-up](docs/audits/2026-10-02-audit-follow-up.md)
separates merged source, open fixes and native/manual validation. V2 remains in
the repository; source presence or V3 exclusion does not prove field retirement.

## Repo layout

| Path | What |
|------|------|
| `taskpaw.py` | V2 agent — tkinter GUI + all watcher logic. Entry: `python taskpaw.py`. Config: `%APPDATA%\TaskPaw\config.json` (Win) / `~/Library/...` fallback. `APP_VERSION` 2.7.1. |
| `taskpaw_hub.py` | V2 Hub (macOS) — polling, SQLite, OpenClaw forwarding, tkinter dashboard. Data: `~/.taskpaw-hub/hub.db`. |
| `macsubs.py` | macOS subtitle-translation microservice exposing the same poll API. **Dropped from V3 monitoring.** |
| `taskpaw_v3/` | Implemented V3 monorepo: Agent/Hub FastAPI services, `core/`, monitor plugins (including media/subtitles and AI activity), LLM workers, task logs, React/Vite UI, Tauri shell, migration, integrations and packaging. Backend tests: `taskpaw_v3/tests/`; UI tests: `taskpaw_v3/ui/src/test/`. |
| `docs/specs/` | Design docs. **`2026-06-27-taskpaw-v3-design.md` is the V3 source of truth.** |
| `docs/guides/` | Operational guides — deployment, macOS/Windows signing, OpenClaw integration, dev-agent activity. |
| `docs/constitution.md` | Hard rules every change is checked against. |
| `scripts/` | Agent/Hub setup helpers (`setup-agent*`, `setup-hub*`), `build.py`, misc tooling. |
| `design-system/taskpaw-v3/` | Generated UI/UX design system (MASTER + page overrides) for the V3 frontend. |
| `Logo/` | Brand logo source (`Logo.png`). |
| `docs/audits/`, `CHANGELOG.md` | Historical audits and release notes; [current follow-up index](docs/audits/2026-10-02-audit-follow-up.md) records public issue/PR delivery and remaining validation. |
| `tests/` | Shared/V2 regression suite, run together with V3 tests by `uv run pytest`. |

## Status: V2 vs V3

- **V2 = frozen** (critical fixes only). Don't add features or refactor V2 for taste.
- **V3 is implemented** under `taskpaw_v3/`: Tauri v2 + React 19/Vite/MUI +
  FastAPI backend, self-describing plugins, migration tooling and regression tests.
  Media restoration/subtitles, shared LLM settings/fallbacks, task logs and AI
  activity are present. Work continues per the V3 design and scoped follow-ups;
  agent↔Hub poll protocol is **kept and only optimized**, not rewritten.
- `macsubs.py` is **excluded from V3 monitoring**. Actual V2/MacSubs shutdown,
  migration and rollback are operator work tracked separately in A06/V01.
- Source versions are recorded in [V2 project metadata](pyproject.toml) and
  [V3's version source](taskpaw_v3/__init__.py); the
  [version consistency test](taskpaw_v3/tests/test_version.py) checks V3 bundle/UI
  copies. They do not establish what is installed or running on a machine.

## Commands

This repo uses **uv**. (System `python3` may be 3.9; uv provides 3.10+.)

```bash
uv sync --group dev              # create/refresh the dev environment
uv lock --check                  # lockfile must be current (CI enforces this)
uv run pytest                    # run tests  ← canonical test command for THIS repo
uv run python -m py_compile taskpaw.py taskpaw_hub.py macsubs.py   # syntax gate
uv run ruff check .              # lint (taskpaw_v3 + tests + scripts; V2 excluded)
uv run ruff format --check .     # formatting gate
uv run mypy                      # type-check (scoped to taskpaw_v3/)
```

Frontend commands, from `taskpaw_v3/ui`:

```bash
npm ci
npm run lint                    # ESLint (React + TS)
npm test                        # Vitest
npm run build                   # TypeScript + Vite
npm run dev                     # development server
```

Dependency groups are defined in [pyproject.toml](pyproject.toml): base `psutil`,
optional `tray` (`pystray`/`Pillow`), `build` (PyInstaller), and `v3`
(FastAPI/Uvicorn/Pydantic/PyYAML). The `dev` group includes the V3 libraries and
check tools, so `uv sync --group dev` + `uv run pytest` needs no extra flags.
There is no `web` extra; `tray` and `build` are not needed for tests. Platform and
toolchain checks are defined in [CI](.github/workflows/ci.yml), with release
packaging in [the release workflow](.github/workflows/release.yml).

Manual V3 headless entrypoints (starting a service uses its configured state):

```bash
uv sync --no-dev --extra v3
uv run --no-dev --extra v3 python -m taskpaw_v3.agent
uv run --no-dev --extra v3 python -m taskpaw_v3.hub run
```

See [deployment](docs/guides/deployment.md) for configuration,
bootstrap, bind/token pairing and migration. V2 manual entries remain
`python taskpaw.py` / `python3 taskpaw_hub.py`; legacy packaging remains
`build.bat` / `build_hub.sh`.

V3 packaging uses the modern build entrypoint:

```bash
uv sync --frozen --extra build --extra v3
uv run python scripts/build.py
```

Read the [macOS signing guide](docs/guides/macos-signing.md) and
[Windows signing guide](docs/guides/windows-signing.md) for platform prerequisites;
a successful source build is separate from signing and clean-machine launch.

## Conventions

- Design docs use repo-relative paths only; never include machine-local home, AFK run, or plugin-cache paths.
- Python ≥ 3.10. Standard library first; dependency groups are declared in
  `pyproject.toml`: base `psutil`, optional desktop `tray`, packaging `build`, and
  V3 backend `v3`. Keep runtime additions within the approved project scope.
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
