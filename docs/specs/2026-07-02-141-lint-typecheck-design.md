# Design: #141 Real linting + type checking (ruff, mypy, eslint)

Date: 2026-07-02 · Issue: #141 · Branch: `ci/141-lint-typecheck`

CI's `test` job was named "lint + test" but only ran pytest — no linter or type
checker existed. Add three gates: **ruff** (Python lint + format), **mypy** (scoped
to `taskpaw_v3/`), and **ESLint** (the V3 UI). V2 is frozen and never touched.

## Ruff (Python lint + format)
- Config in `pyproject.toml`. `select = ["E", "F", "I", "W"]`, `ignore = ["E501"]`
  (line length owned by `ruff format`; remaining long lines are intentional
  strings/URLs). A pragmatic first gate — `UP`/`B` can be layered in later.
- **Scope:** the frozen V2 scripts (`taskpaw.py` / `taskpaw_hub.py` / `macsubs.py`)
  and vendored `skill/` tooling are `extend-exclude`d. Ruff runs on `taskpaw_v3/`,
  `tests/`, and `scripts/`.
- Applied `ruff check --fix` (unused imports, import sorting, f-strings, …) and
  `ruff format`. Residual 15 were fixed by hand: `# noqa: E402` on 3 intentional
  deferred imports (admin/poller/comfyui), rename ambiguous `l`→`ln`, lambda→def
  in two tests, drop 4 unused test locals. Big mechanical reformat is expected when
  adopting a formatter on greenfield code.

## Mypy (scoped to taskpaw_v3)
- `[tool.mypy]` `files = ["taskpaw_v3"]`, excludes `tests/` + `examples/`,
  `ignore_missing_imports = true`, `plugins = ["pydantic.mypy"]`. Not `--strict` —
  a first gate. **V2 is never type-checked** (issue's "do not churn it").
- Fixed all 21 source errors with behaviour-preserving edits:
  - `store.py`: annotate the heterogeneous SQL-param lists `list[Any]`; `assert
    cur.lastrowid is not None` after INSERT (it's always set).
  - `poller.py`: rename the second `prev` → `prev_ack` (one name held both a dict
    and an int|None in one scope).
  - `supervisor.py`: `assert m is not None` inside `if dead:` (dead is only True
    when the managed entry exists) — narrows the 8 union-attr accesses.
  - plugins: the computed `state` is a valid `State` literal — annotate the local /
    `_STATE_MAP: dict[str, State]` / widen `_build_status(state: State)`, importing
    the existing `State` alias from `monitors.base`.
  - The pydantic plugin resolves the earlier `HubConfig` "missing argument" false
    positives (the field has a default).

## ESLint (V3 UI)
- Flat config (`eslint.config.js`): `@eslint/js` + `typescript-eslint` recommended
  (no type-checked rules → fast, no project graph) + `react-hooks` +
  `react-refresh`. Added the dev deps and a `"lint": "eslint ."` script.
- Fixed the one real error (unused `within` import). The 4 `no-explicit-any` hits
  are dynamic rjsf/JSON-Schema/MUI-override glue — downgraded that rule to `warn`
  (surface, don't block) rather than churn the glue with `unknown`+casts. Result:
  **0 errors, 6 warnings**.
- **Build side effect fixed:** adding `typescript-eslint` hoists `@types/json-schema`
  into `node_modules/@types`, which `tsc -b` then includes globally, surfacing a
  latent strict error in `schemaI18n.ts` (`localizeSchema` returned
  `Record<string, unknown>` for `properties`). Fixed with a localized
  `as RJSFSchema` return cast (runtime shape already correct). Without this the
  frontend `build` job would go red.

## CI
- New `lint` job (ubuntu, one python): `uv run ruff check .`, `ruff format --check
  .`, `uv run mypy`.
- Frontend job now runs `npm run lint` (before test/build).
- Dev deps: `ruff`, `mypy`, `types-PyYAML` added; `uv.lock` regenerated.

## Test plan / verification
- `uv run ruff check .` + `ruff format --check .` clean; `uv run mypy` = 0 issues.
- `uv run pytest` = 434 passed. `npm run lint` = 0 errors; `npm test` = 63 passed;
  `npm run build` succeeds; `npm ci` in sync with the updated lock.

## Constitution gate
- §1 Scope: V2 excluded from ruff and never type-checked; only V3/tests/scripts +
  CI/deps touched. Vendored `skill/` left alone.
- §5 Testing: suite green; lockfiles (`uv.lock`, `package-lock.json`) authoritative
  and in sync.

## Cross-PR note
This branch and #145 both branch off origin/main; the V3 reformat may textually
conflict with #145's `auth.py` edits at merge — flagged for the operator (merge
#141 first, then rebase/resolve #145).
