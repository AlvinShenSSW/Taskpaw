# CLAUDE.md

The canonical agent guide for this repo is **[AGENTS.md](AGENTS.md)** — read it
first (project overview, repo layout, commands, conventions), and
**[docs/constitution.md](docs/constitution.md)** for the hard rules.

Claude-specific notes:

- **Tooling is installed under `~/.local`** (gh, node, codex, kimi) — not always
  on a fresh shell's PATH. Prefix with `export PATH="$HOME/.local/bin:$PATH"` (or
  use full paths) when a command isn't found. Python 3.10+ comes from `uv`
  (system `python3` is 3.9).
- **Canonical test command is `uv run pytest`** (there is no `web` extra). See AGENTS.md.
- **V2 is frozen, V3 is greenfield under `taskpaw_v3/`.** Don't refactor V2 for
  taste; new work follows [docs/specs/2026-06-27-taskpaw-v3-design.md](docs/specs/2026-06-27-taskpaw-v3-design.md).
- The V3 UI design system lives in
  [design-system/taskpaw-v3/](design-system/taskpaw-v3/) — read `MASTER.md` and
  the relevant `pages/*.md` before building any V3 frontend page.
- Don't commit or push unless explicitly asked.
