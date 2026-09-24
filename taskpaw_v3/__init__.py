"""TaskPaw V3 (greenfield desktop agent/hub).

`__version__` is the single source of truth for the V3 app version the backend reports
(agent/hub `/ping`). Keep it in lockstep with the desktop bundle version in
`src-tauri/tauri.conf.json` and `src-tauri/Cargo.toml` — they are bumped together on
release. (The repo's `pyproject.toml` version tracks the frozen V2 package separately.)
"""

__version__ = "3.3.0"
