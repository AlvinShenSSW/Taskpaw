# Chinese subtitle output suffix (#261)

## Spec and frozen contract

New Chinese subtitle outputs from standalone AV translation and Jasna AV
translation must be named `<actual video stem>.zh.srt`. The current standalone
planner constructs `.srt`; Jasna's `zh_target_for()` appends `_ZH_EXT = ".srt"`.
The change labels Chinese outputs explicitly for the operator's media players.

Acceptance criteria:

1. Standalone jobs target `<source stem>.zh.srt`, including dotted and Unicode
   names; their Japanese intermediate remains `<source stem>.ja.srt`.
2. Jasna jobs target `<restored media stem>.zh.srt`, for both current `-破解.mp4`
   outputs and reused legacy `_restored.mp4` files.
3. Full transcription/translation, translation-only resume, silent-file output,
   retries, publication and Japanese/checkpoint cleanup use the same job targets.
4. Existing `.srt`, `.chs.srt`, `.zh.srt` and other already-recognized subtitles
   retain the existing skip semantics. Existing files are never overwritten;
   no user library files are migrated or renamed by this release. If a source's
   `.ja.srt` or `.zh.srt` would belong to another listed video's longer stem,
   refuse that source's subtitle work as a naming collision. In particular,
   `a.mp4` alongside `a.zh.mp4` must not generate `a.zh.srt` for the wrong film;
   the existing `m.mp4`/`m.ja.mp4` collision protection must remain effective.
5. English schema descriptions, Chinese form descriptions and the integration
   guide distinguish new `.zh.srt` outputs from still-supported legacy `.srt`.
6. Bump all six V3 version files from main's 3.9.12 to 3.9.13, add release notes,
   pass regression/check gates, obtain independent Opus 5.5 review, and publish
   the Windows installer. V2's package version is unchanged.

The causal boundary is the two target constructors, target ownership checks,
their tests and current documentation. No existing subtitle attribution,
translation policy, lifecycle, locking,
atomic publication, checkpoint format or UI layout change is intended. The
desktop rename/conversion BAT is separate and is not part of this issue.

## Approach and consumers

Change standalone `plan_tree()`'s Chinese path and Jasna `_ZH_EXT`. Keep targets
derived from the actual media stem; do not strip arbitrary suffixes from video
names. The existing `SubsJob` and translator consume these explicit paths,
so they need no new abstraction. The Japanese target still reserves the same
stem, preserving same-stem input collision checks. Path reservation alone does
not preserve cross-role ownership: D261-01 disproved that assumption using
`judge()` on `a.mp4`, `a.zh.mp4`, `a.zh.srt`. Add a small pure ownership-conflict
helper beside `judge()`, using the same normalized stems and recognized-video
pool. A competing video with stem `<source stem>.ja` or `<source stem>.zh`
owns the corresponding generated target. Standalone planning reports this as
a collision; existing shared work rechecks return `subtitle name collision`
before work proceeds at those existing check points. Existing Chinese subtitles still take priority
and are left untouched. The longer-stem video remains eligible for its own
unambiguous targets. Jasna's existing recheck path uses the same rule, including
its not-yet-restored media list, without changing restore scheduling. These are snapshot checks, not a new atomic
filesystem ownership guarantee: a competing video introduced after the final
work recheck remains subject to the existing directory-mutation race. Publication
continues to use the existing atomic no-overwrite path; this issue does not add
publication callbacks or change stop/no-speech lifecycle behavior.

Existing subtitle recognition in `monitors/subs/existing.py` already recognizes
non-Japanese language tags including `zh`, as well as bare `.srt`. Keep that
attribution unchanged. Old subtitle fixtures must remain in regression tests rather than
being globally rewritten. Test helpers representing *new output* switch to
`.zh.srt`; helpers seeding old library subtitles keep `.srt`.

An incomplete old run retains `.ja.srt` and the existing checkpoint. Inspection
of `CheckpointStore.key()` and the translator confirms keys hash serialized
cues, not the output path. Restart uses the new
Chinese target while retaining existing resume/skip checks. Any incompatible
target-derived persistence discovered during implementation must be documented
and tested before the contract is considered satisfied.

## Files and execution surface

| Paths | Allowed change / purpose |
| --- | --- |
| `taskpaw_v3/monitors/plugins/avsubs.py`, `jasna.py` | Chinese target suffix and matching current descriptions |
| `taskpaw_v3/monitors/subs/existing.py`, `job.py`, corresponding tests | Pure generated-target ownership check and explicit naming-collision skip reason; preserve attribution and atomic publication |
| `taskpaw_v3/tests/test_avsubs.py`, `test_jasna_subs.py`, `test_jasna_naming.py` | Explicit target and lifecycle regression coverage; preserve legacy fixtures |
| `taskpaw_v3/ui/src/schemaI18n.ts`, related UI tests if necessary | Chinese output-name help text |
| `docs/guides/openclaw-integration.md`, `README.md` if relevant | Current output examples |
| `taskpaw_v3/__init__.py`, `taskpaw_v3/src-tauri/{tauri.conf.json,Cargo.toml,Cargo.lock}`, `taskpaw_v3/ui/{package.json,package-lock.json}` | Synchronized version copies |
| `CHANGELOG.md`, this design | Release notes and frozen contract |

Read/execute existing shared subtitle modules and packaging scripts without
changing their behavior. Run `uv run pytest` for tests, `uv lock --check`, Ruff,
mypy, and the UI lint/test/build commands. Use an isolated worktree and temporary
test roots. No real transcription, translation API calls, media files or running
Agent configuration are required.

Build with `uv sync --frozen --extra build --extra v3` followed by
`uv run python scripts/build.py`, using Agent role and the committed version.
This generates the backend sidecar, UI dist and Tauri Windows NSIS/MSI bundles
under ignored build directories. The generator may install the declared npm
dependencies and download build tool dependencies. Publish the verified EXE
and checksum in the 3.9.13 release; do not run the installer or deploy it.

## Risks, assumptions and verification

- **Old-library regression:** retain fixtures for bare `.srt`, `.chs.srt` and
  `.zh.srt`, and exercise scan and existing pre-work collision checks.
- **D261-01 / target ownership:** test `a.mp4` with `a.zh.mp4`, `m.mp4` with
  `m.ja.mp4`, case/Unicode aliases, and a competing video present at a work recheck.
  Verify the shorter film is reported as a collision instead of publishing a
  subtitle attributed to the longer one. Existing unambiguous legacy subtitles
  still mark their original film complete.
- **Test helper masking:** use literal expected filenames in target tests and
  keep old-library seed helpers distinct from new-output assertion helpers.
- **Resume / silent-file paths:** reuse the existing mocked lifecycle suite,
  update only output expectations, and require the Japanese intermediate and
  checkpoint cleanup assertions to continue passing.
- **Player behavior:** this issue guarantees the requested filename. Actual
  Jellyfin/Infuse playback is not exercised against the user's live library.
- **Reviewer/model availability:** a local Claude CLI exists; exact Opus 5.5
  response identity must be verified by the external gate, not inferred from an
  alias. An unavailable gate is not approval and prevents release readiness.
- **Windows packaging:** toolchain availability and actual bundle metadata are
  verified by the build; successful unit tests alone do not establish a working
  installer or signing status.

Tests first: literal naming assertions must fail against the old constructors.
Then run the affected subtitle modules, version consistency tests, repository
checks, the independent gate and final full suite on the reviewed revision.
Confirm the new-version installer and checksum before publication.

## Non-goals

No V2 changes, existing-library migration, SMI conversion, new monitor or
translation model, unrelated architecture work, automatic installation, or
service restart. Historical dated design documents retain their original names.
