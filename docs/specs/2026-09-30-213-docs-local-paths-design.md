# #213 — Public design-doc path hygiene

Date: 2026-09-30. Branch: `issue-213-docs-local-paths`. Base: `origin/main` at `bdd8ebe`.

## Spec review

Remove private filesystem references, preserve #210's decisions, label its planning statements historical, and prevent recurrence through pytest plus one docs convention.
Source: the issue #213 body supplied by the driver; no adjacent `issue-213.md` was found in the working tree.
Inspection confirms four offending lines in `docs/specs/2026-09-30-210-hub-task-adapt-design.md` (4, 30, 38, 677).
Evidence corrects the issue's inventory: `docs/specs/2026-09-30-208-jasna-8k-vr-design.md:150` also contains a numeric Windows username in a forward-slash home path.
Include only that additional line's cleanup: a guard covering every spec cannot honestly exempt it. The #211 design is absent here.

## Acceptance criteria

- [ ] #210 uses repo-relative references or prose; original technical decisions remain intact, and planner-stage claims are explicitly historical after PR #212.
- [ ] The additional #208 basetemp reference contains no private home path; its short-path/MAX_PATH guidance remains.
- [ ] The issue's exact `git grep` acceptance command returns no matches (exit 1 is the expected no-match result).
- [ ] Every direct `docs/specs/*.md`, including this plan, passes the guard; each prohibited pattern fails a fixture with a filename and one-based line number.
- [ ] The fixed placeholder set passes, including when multiple paths occur on one line; allowed paths never excuse a forbidden neighbor.
- [ ] One AGENTS.md convention is added; full pytest, Ruff lint/format, mypy and lock checks pass; no version bump.

## Frozen issue contract

The boundary is design documentation, one AGENTS.md rule, and one self-contained test module; application behavior and dependencies are unchanged.
The guard detects home paths with a following separator, local AFK run paths, and Claude plugin-cache paths everywhere in direct spec Markdown files, including fenced examples.
No file exemptions or inline suppression markers. Only exact documented home-username placeholders are exempt; run/cache patterns always fail.
The #208 one-line correction is necessary repository evidence, not general cleanup. Preserve existing placeholder examples elsewhere and leave #211 to its own branch.
Acceptance above plus the Out of scope section is frozen; reviewer preferences cannot expand it.

## Assumptions

- The supplied issue body is authoritative; live comments, labels and open PRs were not queried. Any later scope change belongs to the driver.
- `git log` confirms PR #212 at the supplied base; its deployed state is neither inspected nor relevant.
- Direct children only (`glob("*.md")`), as specified; recursion and general secret scanning are excluded.
- The seven usernames below are the complete initial placeholder set; spelling is case-sensitive, and additions require a documented test change.
- Commands below are future verification obligations, not claims of passing results or available dependencies.

## Approach

Make narrow prose edits first, then add a test-local scanner and fixture coverage. Reuse pytest discovery and repository-root conventions instead of adding a tool, dependency, or CI job.
Resolve the root with `Path(__file__).resolve().parents[2]`, as in `taskpaw_v3/tests/conftest.py`; `test_version.py` similarly anchors sibling files to its own location.
Scan sorted Markdown paths as UTF-8 using `splitlines()` and one-based enumeration. Assert the file set is nonempty, aggregate failures, and report repo-relative POSIX filenames plus line numbers and category.

## Files to change

| Path | Change | Reason |
|---|---|---|
| `docs/specs/2026-09-30-210-hub-task-adapt-design.md` | Edit | Remove four private references; mark planning/handoff state historical. |
| `docs/specs/2026-09-30-208-jasna-8k-vr-design.md` | One-line edit | Remove the additional discovered home path. |
| `taskpaw_v3/tests/test_docs_paths.py` | Add | Repository guard and scanner regression fixtures. |
| `AGENTS.md` | One-line addition | Establish docs convention. |
| `docs/specs/2026-09-30-213-docs-local-paths-design.md` | Add | This planning deliverable; subject to the same guard. |

## Execution surface

Planner writes only this design. The run directory stays read-only. Implementer writes only the four implementation files above; no generated deliverable is needed.
Read dependencies: AGENTS.md, `docs/constitution.md`, specs, existing tests, `pyproject.toml`, `uv.lock`, and `.github/workflows/ci.yml`.
Execute from the repository root with the existing dev environment; if absent, `uv sync --group dev --frozen` consumes manifest/lock and creates the environment and dependency cache, without lock changes.
`uv run pytest taskpaw_v3/tests/test_docs_paths.py` and `uv run pytest` consume tests/specs/config and emit diagnostics, temporary fixtures, bytecode and pytest caches.
`uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, and `uv lock --check` consume source/tool config and emit diagnostics/tool caches; no tracked rewrites are intended.
`git diff --check` and the issue's grep command are read-only checks. Do not run packaging, publication or application services.

## Key implementation notes

Exact Python regular-expression texts (compile separately; no global flags):

- Unix home: `/(?:Users|home)/(?P<user>[^/\\\r\n]+)/`
- Windows home, accepting either slash and case-insensitive drive/prefix: `(?i:[a-z]:[\\/]users[\\/])(?P<user>[^/\\\r\n]+)[\\/]`
- AFK run directory: `\.afk[/]runs[/]`
- Claude plugin cache: `\.claude[/]plugins[/]cache`

The notation above specifies regex text, not executable implementation. Bracketed slashes keep this specification from quoting forbidden literal run/cache paths.
The exact allowlist is `youruser`, `example`, `you`, `USER`, `<user>`, `hubert`, `Tester`; compare the entire captured `user`, without lowercasing or prefix matching.
Use all matches, not only the first. Home-pattern overlap on a Windows forward-slash path may be deduplicated by file/line/category; never skip the other categories after an allowed username.
Keep scanning/assertion helpers in the new test module. The repository test is `test_specs_have_no_machine_local_paths`; fixtures exercise the same scanner and failure formatter.
For #210: remove the checkout target while retaining branch/revision; cite issue #210 in prose; replace the plugin location with the skill name; retain only the repo-relative CI/config references at the final occurrence.
Add a short historical notice near #210's header naming PR #212; label its Handoff notes as historical and rewrite its first bullet as “At the planner stage…” so “no implementation” is not a current claim.
For #208: replace the private basetemp with prose directing use of a short temporary directory and creation of its parent; preserve the MAX_PATH motivation without inventing another absolute path.
Under AGENTS.md Conventions add exactly: “Design docs use repo-relative paths only; never include machine-local home, AFK run, or plugin-cache paths.”

## Risk

| Risk | Likelihood / impact | Mitigation |
|---|---|---|
| False positives from explanatory examples | Medium / low | Fixed documented placeholders; fixture coverage; no broad suppressions. |
| A placeholder masks another leak | Medium / medium | Inspect every match and category; mixed-line regression. |
| Historical rewrite changes #210 decisions | Low / medium | Limit edits to provenance/header/handoff wording and review the diff. |

## Out of scope

History rewriting, #211 branch edits, V2, `ui-preview.html`, runtime/UI changes, version/changelog/dependency changes, and cleanup beyond the identified references.
No deployment, commits, pushes, PR creation or merge authority is granted by this plan.

## Test plan

1. Parameterize rejected fixtures across both Unix roots, Windows backslash/forward-slash paths (including the numeric-username case), lowercase Windows prefix, spaces in usernames, AFK run and plugin-cache patterns.
2. Use synthetic non-allowlisted usernames in test fixtures only. Cover relative and home-prefixed run/cache strings, cache with no trailing separator, and placeholder-home paths containing forbidden run/cache suffixes.
3. Parameterize every allowed username across the home syntaxes; reject placeholder prefixes/suffixes, an unlisted case variant, and mixed allowed/forbidden paths on the same line. Ordinary repo-relative references pass.
4. Temporary Markdown fixtures cover violations on line 2, multiple files/lines, fenced text, CRLF, and exact relative filename/line diagnostics; assert the shared validation raises on each bad fixture and succeeds on good ones.
5. Run the focused module, then full pytest and all checks in Execution surface. Run the issue's exact grep command; inspect `git diff --check` and changed paths. No browser/manual app test is needed.

## Handoff

Plan only: no implementation or test results are claimed. Implement the bounded changes, preserving historical decisions and the additional #208 inventory correction; return failures honestly to the driver.
Keep forbidden example strings in Python fixtures, not specs. The future guard must pass this design without a special exemption; rebased #211 must pass independently.
