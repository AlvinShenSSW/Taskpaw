# Taskpaw-owned library and archive (independent of Filmpaw)

Date: 2026-10-30. Status: implemented — L1–L4 are complete locally (internal review, Codex
gate, full suite); the design was debated over 5 rounds, the last clean. EPIC: #361 (children #362 L1,
#363 L2a, #364 L2b, #365 L3, #366 L4). UI reference (approved by the operator on 2026-10-30, v4):
`design-system/taskpaw-v3/preview/media-library-v1.html`.

Changes made during implementation: `PUT /library/order` for the library priority (§6 had omitted
`position` from the editable fields); `GET /performers?group=true` pages whole name groups for the 演员 tab;
a new actor name may not start with `.` (the scan treats dot folders as hidden).

## 1. Goal

Taskpaw's media workflow archives approved films without Filmpaw running. Today
five call sites in `taskpaw_v3/workflow/finalize.py` and `agent/server/workflow_routes.py`
talk to the Filmpaw sidecar through `workflow/filmpaw_client.py` (performer search,
avatar, target proof, archive copy, archive lookup). This EPIC moves those
capabilities into Taskpaw, adds the operator's new archive targets (category
directories, a new actor directory, an optional second target), and fills the two
「后续」 pages 片库与规则 and 设置 in the 媒体处理中心.

Filmpaw stays a standalone manual tool. Taskpaw imports its NAS roots and aliases
once; afterwards the two apps keep separate data.

## 2. Operator decisions (2026-10-30, source of truth)

| # | Decision |
| --- | --- |
| D1 | Taskpaw runs independently of Filmpaw. One-time import of Filmpaw's NAS roots and aliases, then each app keeps its own data. |
| D2 | Inherit Filmpaw's scan, match and archive logic largely unchanged — "it works, don't reinvent it". |
| D3 | When the NAS has no directory for the actor, 待确认 may create one. It is created only when the operator clicks 「确认入库」, never automatically. |
| D4 | 片库与规则 has four tabs: NAS 片库, 演员 (browse + aliases), 归档规则, 归档历史. |
| D5 | Actor thumbnails come from `folder.jpg` in the actor directory and are shown larger, portrait 3:4. |
| D6 | Every 归档历史 row has a button that opens the final archive directory. |
| D7 | Category directories (类型目录): a registered directory that receives film folders directly (`<category>\<film>`), e.g. a photo-series folder or a 单体 collection. Optional number prefixes recommend it. |
| D8 | Case 1: the actor is not in the collection but the series is → put the film in the category directory instead (prefix match may preselect it). Case 2: the actor is in the library and the film also belongs to a category → default to the actor directory; the operator may add a **second target** (a category directory) at confirmation; the package is then copied twice. Source cleanup waits until both copies are verified and receipted; a failed copy retries only that copy. |
| D9 | 设置 is editable (no hand-editing `agent.yaml`); it is the same configuration as TaskPaw 设置 → 媒体工作流. Mac 信息桥 and 设备 stay as they are. |
| D10 | Home LAN, single user: no multi-user security design; keep data-safety floors (NAS append-only, verify before cleanup, single controller). |

## 3. Scope boundary

In scope: the library index, the archive engine, the replacement of
`FilmpawClient` in the controller, the 片库与规则 and 设置 pages, the 待确认 dialog
changes, removal of the Filmpaw dependency from Taskpaw code and settings.

Not in scope: Filmpaw repository changes; Mac 信息桥; 设备 page; GPU workers (the
library and archive run on the media controller only); moving, renaming or
deleting anything already in the NAS library; a third or later target; a second
target that is another actor directory; editing a library root's path in place
(remove and re-add instead; aliases survive by name); multi-user permissions.

## 4. Architecture

```
媒体处理中心 UI ──/control/workflow/library/*──► library_routes.py
                ──/control/workflow/films/*───► workflow_routes.py ─► finalize.py
                                                                     │
finalize.Finalizer ──(same five calls as today)──► LibraryArchive facade
                                                    ├─ library_store.py  (library.db)
                                                    ├─ library_scan.py   (scanner, thumbs)
                                                    ├─ library_archive.py (copy/verify/commit worker)
                                                    └─ library_import.py (Filmpaw read-only import)
```

- **New database `library.db`** in the workflow data directory (beside `workflow.db`,
  same UNC/network refusal as `workflow.db`). It is separate from `workflow.db`
  so the strict exact-DDL schema validation of `workflow.db` is untouched. It has
  its own `schema_version` row with the Filmpaw migration discipline (read the
  version before DDL, refuse newer, one transaction per step).
- **Facade.** `LibraryArchive` exposes the five methods `finalize.py` already uses —
  `performers(q)`, `thumb(id)`, `target(...)`, `archive(...)`, `lookup(op)` — with
  the same return shapes and the same two exception types (renamed
  `LibraryUnavailable` / `LibraryRefused(status, message)`), so `Finalizer`'s
  waiting, retry and failure handling stays as it is. `local_host.py` constructs
  it instead of `FilmpawClient`.
- **Controller only.** The facade, scanner and archive worker exist only on the
  media-controller role. Workers never see library paths.
- **Threads.** One background scanner thread (one scan at a time) and one archive
  worker thread (one archive task at a time, FIFO; the NAS link is the bottleneck,
  and Filmpaw's 3-way concurrency bought nothing for a single operator). Store
  access is serialized by one lock around one sqlite connection, as Filmpaw does.
- **Dependencies.** Add `zhconv>=1.4.3` and `Pillow>=13,<14; python_version >= '3.11'`
  to the `v3` extra and the `dev` group. The PyInstaller build must collect
  `zhconv`'s dictionary data file (`zhcdict.json`); a packaged smoke test proves
  `normalize("張")` works. Without Pillow (Python 3.10 only) thumbnails are skipped,
  everything else works.

## 5. Data model (`library.db`, schema version 1)

| Table | Columns | Notes |
| --- | --- | --- |
| `libraries` | `id TEXT PK` (uuid4), `kind TEXT CHECK(kind IN ('actor','category'))`, `path TEXT NOT NULL`, `path_key TEXT NOT NULL UNIQUE` (normalized casefold, trailing `\`), `label TEXT NOT NULL`, `role TEXT CHECK(role IN ('archive','readonly'))` (actor only; category is always `archive`), `prefixes TEXT NOT NULL DEFAULT '[]'` (JSON, category only, uppercase), `position INTEGER NOT NULL` (priority order among actor libraries), `last_scan_at TEXT`, `last_scan_ok INTEGER`, `last_scan_ms INTEGER`, `film_count INTEGER`, `free_bytes INTEGER`, `created_at TEXT` | Replaces Filmpaw `sources`. |
| `performers` | As Filmpaw: `id` uuid4, `name`, `name_norm`, `library_id → libraries(id) ON DELETE CASCADE`, `folder_name`, `path UNIQUE`, `first_seen_at`, `last_seen_at`, `is_missing`, `film_count`, `thumb BLOB`, `thumb_mtime REAL`, `thumb_side INTEGER` | `film_count` is new (first-level dirs inside the actor dir, counted during scan). |
| `aliases` | As Filmpaw: `id`, `name_norm`, `alias`, `alias_norm`, `UNIQUE(name_norm, alias_norm)` | Group-level, no FK (Filmpaw D5); survive library removal. |
| `settings` | `key TEXT PK`, `value TEXT` (JSON) | Archive rules (§8) and scan schedule. |
| `archive_tasks` | `operation_id TEXT PK`, `payload_hash TEXT`, `payload_json TEXT`, `status`, `phase`, progress columns, `message`, `receipt_json`, `created_at`, `updated_at` | Filmpaw's task row, workflow mode only. |
| `archive_events` | `event_key TEXT UNIQUE`, `operation_id`, `action` (`mkdir_actor`/`copy`/`verify`/`commit`), `target_index`, `outcome` (`started`/`succeeded`/`failed`), `at`, `detail` | Filmpaw's prepare/confirm journal. |
| `archived_targets` | `target_key TEXT PK` (casefold target film path), `final_hash TEXT`, `directory_identity TEXT`, `operation_id`, `committed_at` | Lets a retry recognise a copy this engine already committed (§9.5). |
| `second_targets` | `binding_hash TEXT PK`, `category_id TEXT`, `target_path TEXT`, `directory_identity TEXT`, `created_at` | The optional second target bound to one approval (§9.4). |

`normalize(s)` = `zhconv.convert(unicodedata.normalize("NFKC", s.strip()).lower(), "zh-hans")`,
identical to Filmpaw (`normalize.py:14-15`), so imported `name_norm`/`alias_norm`
values are valid as they are.

## 6. Library index (inherited from Filmpaw `scan.py`, `routes_sources.py`)

Inherited unchanged:

- A root is reachable only if `os.stat(root)` succeeds and is a directory; any
  `OSError` while listing makes the whole library unreachable and **nothing is
  written** (no performer is marked missing). Never `isdir()`-probe as a deletion test.
- Actor directory = first-level entry, `is_dir(follow_symlinks=False)`, not hidden.
  Hidden = name starts with `.`, or Windows HIDDEN/SYSTEM attribute. If an entry's
  attributes cannot be read, reject the whole snapshot.
- Match disk names to `folder_name` by `casefold()`; a case-only rename keeps the
  row id and updates `name`, `name_norm`, `folder_name`, `path`. New folder → uuid4,
  `first_seen_at = last_seen_at = now`. A row whose folder is gone → `is_missing=1`,
  never deleted; it clears when the folder returns.
- Thumbnail: `<actor>\folder.jpg` → Pillow `draft` + `thumbnail` max side 512, RGB
  JPEG quality 80; rebuild when mtime differs or `thumb_side != 512`; removed file
  clears the thumb; unreadable file keeps the old thumb and logs a warning.
- One transaction per library scan; any exception rolls back.
- Search (`GET .../performers?q=`): group match on `name_norm LIKE %nq%` (escaped)
  or `LENGTH(name_norm) >= 2 AND INSTR(nq, name_norm) > 0`, and the same two tests
  on `alias_norm`; every performer of a matched group is returned; `include_missing`
  filter (default per §10: `false` on the route; Filmpaw's own default was `true`);
  `page_size` default 50, max 200; order `name_norm, id`.
- Aliases: trimmed; empty → 422; equal to the group's own `name_norm` → 409;
  duplicate → 409; delete by id; group-level (shared by every location of the name).
- Thumb endpoint with ETag `"{thumb_mtime}-{thumb_side}"`, 304 on match.

Changed on purpose (each fixes a demonstrated Filmpaw gap or meets a new decision):

- **Add-library validation**: path normalized (`/`→`\`, trailing `\`), must be an
  existing directory at add time, `path_key` (casefold) unique, and a new root may
  not contain or be contained by an existing root of either kind (Filmpaw accepted
  overlapping roots, which later broke scans on the `performers.path` UNIQUE).
- **No blocking reachability probe in list**: the library list reports the last
  scan's result (`last_scan_ok`, time, counts) instead of `isdir()` under the lock.
- **Scheduled scans** (approved preview): scan all libraries at controller start
  (setting `scan_on_start`, default on) and every N hours (`scan_every_hours`:
  0 = off, 2, 6 (default), 12, 24). Independently of that schedule, a library whose
  last scan found it unreachable is re-probed (`os.stat`) every 15 minutes and scanned
  as soon as it is reachable (preview: 「恢复后自动重新扫描」). Manual 重新扫描 /
  全部重新扫描 stay.
- **Category libraries** are not scanned for performers; a scan only counts their
  first-level film directories (`film_count`) and records reachability.
- **Editable fields**: label, role (actor), prefixes (category). Path is fixed.
- **Fuzzy candidates** (rule `fuzzy`, default on): additionally compare with all
  whitespace and the characters `・·.` removed from both sides. Fuzzy hits are listed,
  never preselected.

## 7. Filmpaw import

- Source: `%APPDATA%\Filmpaw\library.db` by default, or a path the operator picks.
  Opened with `sqlite3.connect("file:...?mode=ro", uri=True)`; Taskpaw never writes it.
- Imports `sources(unc_path, label)` as actor libraries (`role=archive`, appended
  to the priority order) and `aliases(name_norm, alias, alias_norm)`. Performer rows
  are **not** copied; each imported library is scanned afterwards.
- Re-import adds only roots whose `path_key` is new and aliases not yet present;
  it never changes or deletes Taskpaw data. An overlapping root is reported and skipped.
- The UI shows a preview (roots with performer counts, alias count) before importing.

## 8. Archive rules (stored in `settings`)

| Key | Default | Meaning |
| --- | --- | --- |
| `multi_location` | `priority` | When one actor has several archive-role locations: `priority` (library order), `most` (most film folders), `recent` (location of the newest receipt for that actor). Always overridable in 待确认. |
| `alias_match` | true | Search aliases as well as names. |
| `preselect_single` | true | Preselect when exactly one candidate exists (still requires 「确认入库」). |
| `fuzzy` | true | §6 fuzzy candidates (listed, not preselected). |
| `allow_new_actor` | true | Offer 「新建演员目录」. |
| `new_actor_library` | first archive-role actor library | Default library for a new actor directory. |
| `new_actor_name` | `actor` | `actor` = prefill with the scraped actor name; `ask` = empty field. |
| `category_prefix` | true | When no actor candidate exists, preselect the category whose prefix matches the film number. |
| `second_target_hint` | true | When an actor candidate exists and a category prefix matches, show the 「加入第二目标」 suggestion (never added automatically). |
| `category_always` | true | List category directories in 待确认 even when an actor matched. |

Fixed rules (no switch; shown read-only): NAS append-only (no overwrite, delete or
move); an existing film folder of the same name (casefold) in the target refuses
the archive; every file is SHA-256 verified before a copy counts; source cleanup
only after receipts; with a second target both copies must succeed first; a retry
repeats only the unfinished step; read-only libraries are never targets.

## 9. Targets and the archive engine

### 9.1 Target kinds

| Kind | Request | Target parent | Proof failure codes |
| --- | --- | --- | --- |
| `performer` | `performer_id` | performer `path` | unknown → `missing`; `is_missing` → `missing`; read-only library → `readonly` |
| `category` | `category_id` | category `path` | unknown → `missing` |
| `new_actor` | `library_id`, `actor_name` | library root (the actor dir is created by the archive task) | name invalid → `invalid`; library not actor/archive → `readonly`; `<root>\<name>` already exists (casefold) → `actor_exists` (the UI says to choose the existing actor; it is listed after the next scan, or at once if this engine created it, §9.3) |
| `manual` | `target_dir` (absolute) | that directory | as today (#51) |

Status codes, inherited from Filmpaw's rule (`transfer.py:36-37, 578-581`): a
**permanent** refusal (every code above, a changed proof, a payload mismatch) is
`LibraryRefused(412, message, code)`. `LibraryRefused` carries the `code`
(`missing | readonly | actor_exists | invalid | permission | changed`), and
`Finalizer.preflight` maps `error_code` from `code`, not from the HTTP status
(today's status mapping at `finalize.py:169-171` would turn every 412 into `offline`).
The film folder name and the new actor name are also validated against
`RelativePath` (`models.py:124-145`: reserved device names, NFC, control
characters) at proof time and refused with `invalid`, so `confirm` never meets a
pydantic error. Only transient conditions (a retryable `OSError`) raise `LibraryUnavailable`.
The in-process engine never raises 409/503, so `Finalizer._submit`'s "busy, submit
again" branch is never entered for a permanent refusal.

Film folder name and actor name both pass Filmpaw's `_name` rule (non-empty, not
`.`/`..`, no trailing space or dot, none of `/\:\0<>"|?*`, not starting with the
staging prefix).

### 9.2 Target proof (inherited from `transfer.workflow_target`)

`target(kind…, film_dir, final_hash=None)` returns
`{target_path, absent, archived, expected_target: {target_path, directory_identity}, error_code?}`:

- `directory_identity` = `[st_dev, st_ino]` of the target parent from `os.lstat`
  (stored as strings, arbitrary precision); a symlink/reparse point (attribute 0x400)
  or non-directory parent is refused.
- `absent` = no entry in the parent whose `casefold()` equals the film folder name
  (staging-prefixed names ignored).
- `archived` = not absent **and** `archived_targets` holds this target path with the
  same `final_hash` and the same current directory identity (§9.5). This is a cheap
  record check used for display and for admitting a re-confirm; the archive task
  re-verifies content before it relies on it (§9.3).
- Retryable `OSError` (Filmpaw's network WinError/errno list in `directory_reads.py`)
  → `LibraryUnavailable` (offline); other `OSError` → refused 412.

### 9.3 Archive task (inherited from `transfer.create_workflow_task` and the worker)

Request: `operation_id`, `package_dir`, primary target (kind + ids + `expected_target`),
optional `second` target `{category_id, expected_target}`, `files [{path, size, sha256}]`.

- **Idempotency**: same `operation_id` + same payload hash → current snapshot;
  different payload → refused 412. Unknown `operation_id` in `lookup` → `None`.
- **Preflight, for every target before anything is copied**: re-prove the target
  (identity must equal `expected_target`, else 412); conflict check (existing film
  folder → task `failed`, message 「目标已有同名影片目录」, unless it is `archived`
  and passes the re-verification below); the package's file set must equal the
  approved list exactly (paths and sizes); free space ≥ package size per target;
  write probe (create `.taskpaw-archive-<task>.probe` exclusively, then delete it —
  the only file the engine ever deletes on the NAS). A conflict in the second target
  is therefore found before the primary is copied.
- **Re-verifying an `archived` target**: every approved path must be present with
  its approved size and re-hash to its approved `sha256` — the same check restart
  recovery uses. Files that are not in the manifest (e.g. artwork a media server
  added) are ignored and listed in the task message. Pass → that target is recorded
  as committed without copying. Fail → task `failed`, message
  「目标目录内容与批准清单不一致」; nothing is copied, nothing is cleaned.
- **New actor directory** (primary `new_actor` only): journal `mkdir_actor started`,
  `os.mkdir(<root>\<name>)` (exclusive), journal `succeeded`, and in the same step
  insert the performer row (uuid4, `folder_name=name`, `is_missing=0`), so a later
  failure leaves an actor the operator can pick on retry. `FileExistsError` →
  `failed` with `actor_exists`.
- **Copy**: staging dir `<parent>\.taskpaw-archive-<task>-<uuid>`; each file opened
  `xb` (exclusive), copied in 1 MiB chunks, SHA-256 computed from the bytes read,
  `flush` + `os.fsync`; mtime restored; progress persisted at most every 0.2 s.
- **Verify**: re-hash every staged file; each must equal both the copy-time hash and
  the approved `sha256`.
- **Commit**: re-check the conflict, then `os.rename(staging, <parent>\<film>)` —
  on Windows `os.rename` never replaces an existing destination (`FileExistsError`
  → conflict). Record `archived_targets` with `directory_identity` = the `lstat`
  identity of the film folder's **actual parent at commit time** (for new_actor that
  is `<root>\<name>`, not the root), so a later `performer` proof of the same folder
  matches. Then the second target, if any, runs copy/verify/commit.
- **Journal**: `started` before and `succeeded`/`failed` after each of
  `mkdir_actor`, `copy`, `verify`, `commit`, per target index. A journal or DB write
  failure stops the worker (task `needs_review`) until restart, as Filmpaw.
- **States**: task `queued | running | completed | failed | needs_review`; phase
  `queued | preflight | copying | verifying | committing | finished`; per-target
  status. An error during commit → `needs_review`; any other error → `failed`.
- **Restart recovery** (Filmpaw `T:1361-1446`): unmatched `started` events → that
  target `needs_review`, except a `commit started` whose staging dir no longer exists
  and whose `<parent>\<film>` re-hashes to the approved manifest → `succeeded`, and
  its `archived_targets` row is written then. `copying`/`verifying` targets →
  `failed`. `queued` tasks (nothing started) are put back on the worker queue.
  Nothing else is resumed; staging leftovers are kept and their path is put in the
  message.
- **Receipt** (only when every target committed):
  `{task_id, target_path, directory_identity, files: [{path,size,sha256}] (sorted),
  second: {target_path, directory_identity} | null}`.

Not inherited, on purpose: Filmpaw's ancestor-handle guards with denied write
sharing, alternate-data-stream checks, `GetFinalPathNameByHandle` canonicalization,
deny-write source opens, and its manual-UI batch/context cache, cleanup service and
stop controls. The source is Taskpaw's own private final package, and every byte
written is hashed against the operator-approved manifest, so a changed source fails
verification instead of reaching the NAS. Single user on a home LAN (D10).

### 9.4 Second target

- Only a category directory; only when the primary is `performer` or `new_actor`;
  at most one.
- `ApprovalBinding` is **not** changed: it is a strict hashed model and old stored
  approvals must keep validating. The choice is stored in `library.db`
  `second_targets` keyed by the approval's binding hash (`digest(binding)`, the value
  `approve` stores, `approval_store.py:193`; unique per confirm because each binding
  carries a fresh `evidence_id`). `Finalizer.confirm` **always** writes the row for a
  `library_performer` or `library_new_actor` binding — `category_id`/`target_path`
  NULL when no second target was chosen — immediately before `approve`. A row whose
  binding never became an approval is inert. `_submit` reads it by `digest(binding)`
  and passes `second` to `archive()`. A `library_performer`/`library_new_actor`
  binding with **no** row fails the operation with 「入库目标记录缺失，请重新确认入库」
  instead of silently archiving one copy. Other schemes never carry a second target.
- `Finalizer._accept` additionally requires the receipt's `second.target_path` to
  equal `<category path>\<film>` when a second target was bound, and the receipt
  file list to equal the final manifest (one file list covers both copies).
- Cleanup is unchanged: it starts only after `_accept`, i.e. after both copies.

### 9.5 Retrying only the failed copy

When the second copy fails (e.g. a same-name folder already exists in the category,
or the share is offline), the task fails and the archive stage becomes failed, as
today. The operator re-confirms (the existing 「上次入库未完成…可重新确认入库」 path).
The new target proof reports the primary as `archived` (same `final_hash`, same
directory identity, recorded in `archived_targets`); `Finalizer.preflight` reports it
as archived instead of `duplicate`, and `Finalizer.confirm` accepts `absent or
archived`. `TargetIdentity.expected_absent` stays `True` and from now on means
"the film folder is absent, or holds this engine's verified copy of this exact
package". The new task re-verifies that copy's content (§9.3) and skips copying it;
only the failed copy runs again. A folder of the same name that this engine did not
commit is never treated as archived.

### 9.6 Candidates (replaces the Filmpaw search in `Finalizer.preflight`)

For each scraped actor name: search names (+ aliases when `alias_match`), then fuzzy
(§6), excluding missing performers (`include_missing=false`, as today's
`filmpaw_client.py:122`). Candidates are archive-role performer locations; read-only locations are
listed for information only. Preselection:

1. Exactly one actor group with archive locations → preselect it if
   `preselect_single`; several locations of that one group → choose by
   `multi_location`.
2. Several different actor groups → no preselection.
3. No actor candidate → if `category_prefix` and a category prefix matches the film
   number (case-insensitive `startswith`), preselect that category; else nothing.
4. `second_target_hint`: actor candidate preselected and a prefix matches → the
   response carries `second_hint: category_id`.

The preflight response keeps its current fields and adds `categories [{id, label,
path, prefixes, film_count, prefix_match}]`, `selected {kind, id}`, `second_hint`,
`new_actor {library_id, name}` (defaults from §8), `archived` for the selected target.

### 9.7 TargetIdentity encoding (frozen; persisted in every approval and receipt)

| Kind | `native_identity.scheme` | `root_id` (uuid5 of `_PERFORMER_NS`) | `volume_or_export_id` | `export_identity` | `relative_path` |
| --- | --- | --- | --- | --- | --- |
| performer | `library_performer` | `taskpaw-performer:<performer_id>` | performer id | performer path | `<film>` |
| category | `library_category` | `taskpaw-category:<category_id>` | category id | category path | `<film>` |
| new_actor | `library_new_actor` | `taskpaw-new-actor:<library_id>:<name_norm>` | library id | library root path | `<name>/<film>` |
| manual | `manual_directory` (unchanged) | unchanged | target dir | target dir | `<film>` |

`object_id` stays the JSON of the parent's `directory_identity` (for new_actor, the
library root's). `actor_ids` stays `(target.root_id,)` (`finalize.py:243`), so it is
never empty. The three new literals are added to `NativeIdentity.scheme`; existing
records keep their old literals and still validate. `export_object_id` now means a
Filmpaw-era binding (§9.8). Code that changes: `Finalizer._target` (build per kind),
`_submit` (dispatch per scheme, pass `second`), `_accept` (`export_identity /
relative_path` already yields `<root>\<name>\<film>` for new_actor; add the second
target check), `board.py:1196` (manual check unchanged; new kinds get labels).

### 9.8 Cutover from Filmpaw

- Archives approved through Filmpaw and still open at upgrade hold
  `export_object_id` bindings with Filmpaw performer ids. `_submit`/`_poll` fail such
  an operation once with 「升级前未完成的归档，请重新确认入库」; re-confirming goes
  through the Taskpaw library. Manual-directory bindings are kind-compatible and
  simply resubmit to the new engine.
- If Filmpaw had already copied that film before the upgrade, the re-confirm sees a
  same-name folder the new engine did not commit and blocks with 「目标已有同名影片目录」;
  the operator checks it by hand. Accepted (P2, rare: the release notes ask the
  operator to let open archives finish before upgrading).

## 10. Control routes (controller, loopback app, existing auth)

Library: `GET/POST /library/libraries`, `PATCH/DELETE /library/libraries/{id}`,
`POST /library/libraries/{id}/scan`, `POST /library/scan-all`, `GET /library/scan-status`,
`POST /library/performers/{id}/aliases`, `DELETE /library/aliases/{id}`,
`GET/PUT /library/rules`, `GET /library/import/preview`, `POST /library/import`,
`GET /library/history` (filter `pending|new|category|manual|second`, q, page),
`GET /library/history/{receipt_id}`, `POST /library/open {path}` (only a path inside
a registered library root or a receipt target; 403 otherwise; uses the existing
`_open` helper). All under `/control/workflow`.

The existing `GET /performers` and `GET /performers/{id}/thumb`
(`workflow_routes.py:313-328`) are kept as the one performer API, now served from
the library: `GET /performers` gains `library_id`, `filter`
(`multi|alias|missing|nothumb`), `include_missing` (**default `false`**, today's
behaviour for the 待确认 search; the 演员 tab passes `true`), `page`, `page_size`. Its response
stays `{items: [...]}` with every item keeping today's fields `{id, name,
source_label, unc_path, has_thumb, aliases: string[]}` (`api.ts:444-445`,
`ReviewDialog.tsx:303`) — new fields (`library_id`, `is_missing`, `film_count`,
`alias_ids`) and envelope keys (`total`, `page`, …) are added, none renamed;
`GET /performers/{id}` returns one performer with all locations of its name group,
aliases and recent receipts.

Existing film routes: `GET /films/{id}/target` gains `category_id`, `library_id` +
`actor_name` parameters and the §9.6 fields; `POST /films/{id}/confirm` body becomes
`{performer_id | category_id | new_actor{library_id,name} | target_dir, second_category_id?}`.

History is read from `workflow.db` archive receipts (already persisted) joined with
film facts and cleanup state. `ArchiveReceipt` keeps no target kind or second target
(`models.py:1254-1267`, `extra="forbid"`), so each row is joined by `operation_id`
to `library.db` `archive_tasks` (payload: kind, second target; receipt: both paths).
Filmpaw-era receipts have no task row and fall back to the binding's scheme
(`export_object_id` → actor, `manual_directory` → manual). No new history table.

## 11. UI

Follows the approved preview and `design-system/taskpaw-v3`. Pages render inside
the existing `MediaWorkspace`; the 「后续」 badge is removed from 片库与规则 and from
设置, which is renamed 「设置」 (Mac 信息桥 keeps its badge).

- **片库与规则** — tabs NAS 片库 (actor libraries and category directories as cards,
  add/edit/remove, scan progress, auto-scan summary), 演员 (search, filters, 3:4
  `folder.jpg` thumbnails 54×72 in the list and 144×192 in the drawer, locations,
  alias add/delete), 归档规则 (§8 switches, priority order, fixed rules), 归档历史
  (table with 打开目录 per target, receipt drawer with file hashes and cleanup state).
- **设置** — embeds the existing `WorkflowSettingsSection` (role, directories, tools,
  Miko, worker access) plus a 片库 card (scan schedule, library data path, Filmpaw
  import). The 「Filmpaw 数据目录」 field is removed.
- **待确认 dialog** — actor candidates with larger thumbnails; 「新建演员目录」 option
  (name + library, live duplicate warning, created only on confirm); 「或放到类型目录」;
  the 「第二目标（可选）」 block with the hint button and a category picker; two target
  paths when a second target is set; the success state lists both. When the primary
  is already `archived` (a re-confirm after a failed second copy) the dialog shows
  「第 1 份已入库，本次只复制第二份」 on that target.
- First run (no libraries): 片库与规则 shows the import / add-library empty state;
  待确认 shows a notice linking to it.

## 12. Removal of Filmpaw from Taskpaw

Delete `workflow/filmpaw_client.py` and its tests; remove `filmpaw_data_dir` from
`WorkflowSettings`, `workflow_settings.py`, `WorkflowSettings.tsx`, the setup wizard
copy and i18n. `WorkflowSettings` (pydantic, default `extra="ignore"`) keeps loading
an old `agent.yaml` that still has the key. Replace user-visible 「Filmpaw」 labels
(e.g. `board.py:1286`, waiting text 「等待 Filmpaw 运行」) with 片库 wording.

## 13. Risks and assumptions

| Risk / assumption | Mitigation |
| --- | --- |
| A: on Windows `os.rename` fails with `FileExistsError` when the destination exists (also on SMB). | Verified 2026-10-30 on local NTFS, Python 3.12.9: both a non-empty and an empty existing destination directory raise `FileExistsError` (WinError 183). L2 keeps a test for it. SMB behaviour is assumed; to be checked on the operator's NAS (`待实机`). |
| A: `[st_dev, st_ino]` from `os.lstat` is stable for an SMB directory across calls. | Inherited from Filmpaw, which runs this against the same NAS today. |
| A: zhconv's data file is missing from the PyInstaller bundle. | L1 build check + packaged smoke. |
| Scan of a large library blocks other library calls. | One transaction per library under the store lock; scans run in the background thread; list reads the last result. |
| Hashing large packages twice (copy + verify) costs time on a second target. | Inherited cost; accepted. |
| The operator's existing approvals/receipts (made through Filmpaw) must still display in 归档历史. | History reads `workflow.db` receipts, which Filmpaw-era archives already wrote. |

## 14. Child issues and frozen contracts

Order L1 → L2a → L2b → L3 → L4; each branch stacks on the previous one. Every
child: tests first, internal review, Codex gate (gpt-6-astra xhigh), full local
suite on the final commit; CI once at the end of the stack. **The five children ship
together as one release**: L2a removes Filmpaw while adding or importing a library
needs the L3 UI, so no intermediate child is released on its own (each stays
merge-safe for the code base and its own tests).

### L1 — Library index (backend)

Acceptance criteria:
1. `library.db` schema v1 (§5) created in the workflow data dir; version discipline as §4; refuses UNC/network paths like `workflow.db`.
2. Libraries CRUD with §6 validation (normalize, exists, unique casefold, no nesting either way, editable label/role/prefixes only).
3. Scanner with every inherited rule in §6 (unreachable → no writes; hidden; casefold; case-only rename; missing; thumbnails; per-library transaction) and `film_count` for performers and categories.
4. Search, filters, paging, aliases and thumb endpoints with §6 semantics, plus fuzzy.
5. Scheduled scans (start + every N hours) and manual scan / scan-all with a status endpoint.
6. Filmpaw import (§7) read-only, idempotent, with preview.
7. Rules get/put (§8) with defaults and validation.
8. Routes in §10 for the above, controller role only; dependencies added; build collects zhconv data.
Non-goals: archive engine, finalize changes, UI.

### L2a — Archive engine with performer/manual parity; Filmpaw removed

Acceptance criteria:
1. `LibraryArchive` facade with the five methods and exceptions (§4, §9.1 status rule); `local_host.py` uses it.
2. Target proof (§9.2) and the archive worker (§9.3) for `performer` and `manual` targets: idempotency, preflight of all targets, copy/verify/commit, journal, states, restart recovery (incl. re-queue), receipt, `archived_targets`.
3. `library_performer` encoding (§9.7) in `_target`/`_submit`; candidate search in `Finalizer.preflight` served by the library with today's single-candidate preselection; existing `/performers` and `/thumb` routes served by the library (§10).
4. Cutover (§9.8).
5. `filmpaw_client.py` deleted; `filmpaw_data_dir` removed from backend **and** from `WorkflowSettings.tsx`, the setup wizard copy and i18n in the same change, so settings saves keep working (old yaml still loads); Filmpaw wording removed from user-visible text.
Non-goals: category/new-actor/second target, rules-driven preselection, history, new UI pages.

### L2b — Category, new actor, second target, rules, history

Acceptance criteria:
1. `category` and `new_actor` targets (§9.1, §9.7), including performer-row insert at mkdir and `actor_exists`.
2. Second target (§9.4) with the always-written row, and retry-only-the-failed-copy with content re-verification (§9.3, §9.5).
3. Rules-driven candidates and preselection (§9.6) and the extended preflight/confirm contract (§10).
4. `_accept` checks the second target; cleanup still starts only after `_accept`.
5. History routes with the `archive_tasks` join and Filmpaw-era fallback, and `/library/open` (§10).
Non-goals: UI.

### L3 — 片库与规则 and 设置 pages (UI)

Acceptance criteria: §11 片库与规则 (four tabs) and 设置 as in the approved preview,
wired to L1/L2a/L2b routes; badges per §11; first-run empty state; vitest coverage for
each tab's main flows; tsc and lint clean.

### L4 — 待确认 dialog (UI)

Acceptance criteria: §11 待确认 dialog behaviour wired to the §9.6 preflight and the
new confirm body; candidate thumbnails; new actor; category; second target with hint;
success state with both targets; vitest coverage; tsc and lint clean.
