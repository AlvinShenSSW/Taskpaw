# #191 — AV 翻译 library safety: recognise existing subtitles, fail-closed scan, never overwrite; version 3.7.1

Date: 2026-09-25 (design v4, FROZEN — debate rounds 1–4: F1–F19 folded in; round 4 CLEAN)
Issue: #191. Driver: `/afk` (Opus 5.5 leads; Opus pilot; Codex 外门 gpt-6-astra high; Kimi 终审). Merge: leave-open.
Upstream: #179 (avsubs), #177 (Jasna AV 翻译), #187 (naming), #189 (progress, v3.7.0).

## Spec review

The owner's standalone AV 翻译 run re-subtitled films that already had Chinese subtitles, because
only an exact `<video stem>.srt` counts today (`avsubs.plan_tree`; Jasna `subs_kind`). Evidence
(read-only audit of the owner's library, 2026-09-25):

| Film | Existing subtitle | Videos in folder | Rule that must catch it | Today |
|---|---|---|---|---|
| LMNO-047 | `LMNO-047-破解-4K-C.zh.srt` (tokens swapped + `.zh`) | 1 | (c) (now also (a): the duplicate `<stem>.srt`) | re-subtitled → duplicate |
| LMNO-079 | `LMNO-079-破解-C-4K-C.zh.srt` | 1 | (c) | queued (owner stopped the task) |
| PQRS-218/564/566/768/770/822 | `<stem>.chs.srt` | 1 each | (b) | queued |
| PQRS-860 | `PQRS-860-破解-C.chs.srt` (= `<stem>.chs.srt`) | 1 | (b) | queued |
| PQRS-948 | `<stem>.chs.srt` + `dl.example.com@pqrs00948.srt` | 1 | (b), (c) | queued |
| LMNO-005 | exact `<stem>.srt` from February | 1 | (a) | planned anyway: `exists_quietly` is False on ANY `OSError` |
| DEFG-594 cd1–cd4, HIJK-* cd1–cd5 | one `<part stem>.srt` per part | 2–5 | each part only its own | correct today; must stay correct |

And `SubsJob._publish` uses `os.replace`: had LMNO-005's translation succeeded, the owner's own
`.srt` would have been replaced by a machine translation.

Owner's rules (2026-09-25): a film counts as already subtitled — and is skipped entirely — when
**(a)** `<stem>.srt` exists, or **(b)** `<stem>.<any tag>.srt`/`.ass` exists and the tag is not
Japanese, or **(c)** its folder holds only this one video and any non-Japanese subtitle file. Plus: a
scan that cannot read the NAS skips the film (never "no subtitle"); publishing never overwrites an
existing file.

## Acceptance criteria

- [ ] AC1 **Shared recognition** (`subs/existing.py`, new, pure, never raises) — from ONE directory
  listing (file names):
  - `norm(s) = NFC(s).casefold()`; subtitle extensions `.srt .ass .ssa .vtt`; names starting `._`
    are never subtitles (nor videos); our temp names (`… .tmp`) are not subtitles (extension).
  - **Tags (F12):** for (a)/(b), a subtitle's tags are only the part of its normalised base AFTER the
    stem it is attributed to (`base[len(stem)+1:]`), split on `.`, `-` and `_` — so (a) has no tags and
    a Japanese-looking token inside the video's own name (`ABC-123-JP.srt`, `Japanese_Wife_01.srt`)
    never disqualifies it. For (c), the tags are the whole base split the same way, minus every token
    that also occurs in the video's own stem. Japanese tags: `ja jp jpn jap japanese 日语 日文 日本語`
    (F3). A subtitle with a Japanese tag never counts as Chinese.
  - **Attribution (F2, F17):** each subtitle is attributed to the video in the listing — for
    attribution ANY common video extension counts (`JASNA_VIDEO_EXTENSIONS` ∪ the caller's set), so a
    sibling `.mkv` in an mp4-only task still owns its subtitle — (plus any `extra_videos` the caller
    names, e.g. Jasna's not-yet-restored media) whose normalised stem is
    the **longest** one with `base == stem` or `base.startswith(stem + ".")`. A subtitle counts under
    (a)/(b) only for the video it is attributed to. So `Movie.part2.srt` belongs to `Movie.part2.mp4`,
    never to `Movie.mp4`; `X-cd1.srt` never to `X-cd10.mp4` (no dot boundary).
  - **(a)** attributed and `base == stem` (any subtitle extension). **(b)** attributed, `base` =
    `stem + "." + tags`, no Japanese tag. **(c)** only when the caller enables it, the video is in
    the listing and is the ONLY qualifying video there (the caller's filter: extension set, no
    `.tmp.`, no `._`), and some subtitle file has no Japanese tag (any name) **and is not attributed
    to another video** (F17: `Movie.mp4` + `Movie.part2.mkv` + `Movie.part2.srt` in an mp4-only task is
    not credited to `Movie`).
  - Accepted residual (F19): (c) subtracts the video's own stem tokens, so `X-JP.mp4` + `X.jp.srt`
    alone in a folder counts as subtitled (the only Japanese marker equals a stem token) — contrived,
    non-destructive (the film is skipped, nothing is written).
  - `judge(video_name, names, is_video, *, rule_c: bool, extra_videos=()) -> Existing(chinese:
    Optional[str], ja_transcript: Optional[str])` — the matching file names; `ja_transcript` =
    `<stem>.ja.srt` present.
- [ ] AC2 **avsubs scan** (`plan_tree`): each video is judged (rule_c on) from the listing of its own
  directory that the walk already holds — no per-file existence probe for subtitle state remains
  (the per-video `source_identity` stat and per-entry errors stay; both already fail closed).
  Recognised → counted in `done` (`queue_pre_done`, 已有字幕). `translate_only` = not Chinese and
  `ja_transcript`. An unreadable directory stays an error + skip (existing).
- [ ] AC3 **Jasna** (`plan_subs`, rule_c **off** — C4/F1): ONE `os.scandir(output_folder)` name
  listing per planning pass. A `FileNotFoundError` counts as an empty listing ONLY when the output
  folder is not a root (its parent differs from itself and its `pathlib` name — `Path(out).name`, which
  ignores a trailing separator — is non-empty; F18: a share or
  drive root never counts as missing) and its parent can be listed and does not contain it, names
  compared with `os.path.normcase` + NFC (F5/F13: on Windows an unreachable server or
  share — WinError 53/67 — also maps to `FileNotFoundError`, which must not read as "empty"); every
  other failure raises → the existing "planning failed" path (subtitles off for this run, alert). This
  exception applies to the planning listing only, never to the AC5 re-check listings. The media a film's subtitles belong to (new `-破解` name, else
  legacy `_restored`, else the future new name) is resolved **from the same listing** (F6), and
  `SubsPlan` carries each film's kind **and** media; `_setup_subs` uses them and never judges or
  probes again. Not-yet-restored media are passed as `extra_videos` for attribution.
- [ ] AC4 **Never overwrite** (`SubsJob._publish`): write `<target>.<gen>.tmp`, then move it into place
  without replacing: pre-check (target present → refuse) then Windows `os.rename` (refuses an
  existing target: `FileExistsError`, verified on NTFS incl. case-variant, read-only, open and
  directory targets) or POSIX `os.link(tmp, target)` + `tmp.unlink()` (EEXIST refuses); a filesystem
  without hard links → pre-check + `os.replace` (race window; debug log). `rename`/`link` are as atomic
  as `os.replace` for constitution §2. A refused move removes the tmp. Result is a **distinct type**
  (F8) — `PublishResult` = `ok` | `exists` | `error(text)` — so mypy flags every unconverted caller:
  avsubs 741, 743, 946, 954, 1212, 1255; Jasna 1461, 2306, 2455, 2463, 2584 (Stop-path callers only
  log). The no-speech empty pair: `.srt` refused → the empty `.ja.srt` this call just wrote is removed.
- [ ] AC5 **Re-check before work** (placement pinned, F7):
  - **Before ASR** — avsubs in `_start_asr` after the GPU hold is held; Jasna in `_start_subs` after
    the carried-hold transfer / `take` (so `_carried` is already consumed and no per-poll listing
    happens while the GPU is refused). The directory listing is taken **outside** the plugin's lock;
    judging and the settle happen under it, releasing the hold (`release_gpu`) and requesting advance.
    A skip hands the freed lease to the earliest waiter — accepted (the task loses that turn).
  - **Before submitting a full film's translation:** under the lock, right after `publish_ja`
    returned `ok` (avsubs `_poll_asr_locked`, Jasna `_poll_subs_locked` — the same class of I/O as the
    publish itself, F15). avsubs translate-only films submitted at Start are **not** re-checked (the
    Start scan is seconds old; no SMB I/O under the N8 launch lock). Jasna translate-only films ARE
    re-checked before their submit in `_start_subs` (they are submitted hours after the scan): listed
    outside the lock before the submit (between `_gpu_give(carried)` and `with self._launch_lock`); no
    GPU hold exists there. **Every skip branch sets `_advance_requested = True`** (Jasna's `check()`
    dispatches only on a request or a GPU retry; without it the `subs_only` walk would stall).
  - **The listing helper never raises (F14):** any exception while listing → "unreadable"; so the
    Jasna before-ASR listing (outside the K1 fence) can never strand the hold.
  - Chinese present → settle `skipped`, reason `subtitle exists` (已有字幕); listing error → settle
    `skipped`, reason `subtitle state unreadable` (fail closed; retried next Start). A skip neither
    adds to nor resets the failure streak (existing behaviour).
- [ ] AC6 **Refused publish / skip outcome:** `.srt` refused or AC5 "subtitle exists" → settle
  `skipped` (`subtitle exists`) and **discard the `.ja.srt` only if this job published it in this
  run** (F9; a pre-existing library transcript stays). `.ja.srt` refused (a transcript appeared
  mid-run) → settle `skipped` (`transcript exists`); next Start plans it `translate_only`. Reporting:
  `subtitle exists` / `transcript exists` → one info log line per film (no alert, like the existing
  `cancelled` skip); `subtitle state unreadable` → additionally ONE deduplicated alert per run
  (constitution §4; like `_alert_no_key`, F16). Counts show in the queue's 跳过 (F11).
- [ ] AC7 **Texts**: avsubs/Jasna field help (EN + zh), avsubs module docstring, README row,
  openclaw guide, Start idle note `N already have subtitles`; CHANGELOG; version 3.7.0 → 3.7.1.
- [ ] AC8 Tests per the plan; `uv run pytest`, ruff, mypy, UI lint, vitest green.

## Frozen issue contract

**In scope:** AC1–AC8. **User-visible changes allowed:** more films recognised as already subtitled
(skipped / counted 已有字幕); films whose subtitle state is unreadable are skipped for the run instead
of processed; a publish that would overwrite is refused and the film skipped; wording; version 3.7.1.

**Invariants:** constitution §2/§4/§5; restore planning (`plan_queue`, `restored_output_for`) and the
GPU lease, scheduling, translation and #189 progress semantics unchanged except that recognised
films never get a job; cd1…cdN parts stay independent; no new threads; `check()` / `start()` never
raise; no shell=True; tests never touch the network, real exes or default ports; no key anywhere.

**Corrections from repository evidence:**

| # | Issue text | Evidence | Correction |
|---|---|---|---|
| C1 | "re-check the target just before publishing" | the tmp is written first; check-then-`os.replace` races | move-without-replace refuses (AC4); the pre-check is defence in depth on every platform |
| C2 | "scan fails closed on a stat error" | avsubs' walk already lists every directory (`os.scandir`) | subtitle state is judged from that listing; the remaining per-video `source_identity` stat and per-entry errors already fail closed (AC2) |
| C3 | "apply the same rule wherever Jasna plans subtitles" | Jasna's output folder is flat (many restored videos + staging) | a/b apply; see C4 |
| C4 | rule (c) for Jasna | flat output folder: with one restored video + another film's pre-placed subtitle, (c) would credit the wrong film forever (critic F1, simulated) | rule (c) is off for Jasna |

**Non-goals (OUT-OF-SCOPE, recorded):** Jasna's restore planning also uses `exists_quietly`
(`restored_output_for`) and would re-restore on a transient output-folder error — separate issue if
wanted; cleaning up duplicates already written (e.g. LMNO-047); image subtitles (`.sup`, `.idx`);
content-based language detection (a bilingual `x.zh.ja.srt` counts as Japanese → a duplicate may be
written; non-destructive); the owner's current stopped run (they restart it after upgrading).

**Causal boundary:** `monitors/subs/existing.py` (new), `subs/job.py` (`_publish`, `PublishResult`),
`subs/__init__.py`, `plugins/avsubs.py` (plan_tree, `_start_asr`, full-film submit, publish callers,
idle note, field text), `plugins/jasna.py` (`plan_subs`/`SubsPlan`/`subs_kind`, `_setup_subs`,
`_start_subs`, full-film submit, publish callers, field text), `ui/src/schemaI18n.ts` +
`ui/src/i18n.ts` (help texts), README, guide, CHANGELOG, version files, tests, this doc.

## Assumptions

| # | Claim | Basis | Risk if wrong |
|---|---|---|---|
| A1 | Windows `os.rename` refuses an existing target | VERIFIED (critic experiment, NTFS, Python 3.13, Win 11 26200: existing, case-variant, read-only, open, directory targets → `FileExistsError`) | — |
| A2 | Over SMB the server refuses the same rename | unverified here (no share reachable; the NAS is off-limits); MS-FSCC FileRenameInformation `ReplaceIfExists=FALSE` must fail | overwrite only in a microsecond race after the pre-check |
| A3 | `os.link` onto an existing file fails (EEXIST / `FileExistsError`) | VERIFIED on Windows NTFS; POSIX link(2) | no-hard-link filesystems use the pre-check fallback |
| A4 | The NAS returns complete directory listings | normal SMB behaviour | a partial listing could miss a subtitle → AC4 still refuses the overwrite |
| A5 | The owner's affected folders each hold one video | VERIFIED by the read-only audit (evidence table) | — |

## Test plan

- `test_subs_existing.py`: every evidence row (names only), cd1…cdN and cd1/cd10 folders,
  `Movie.mp4` + `Movie.part2.mp4` + `Movie.part2.srt` (attribution), `ABC-123.mp4` + `ABC-123.C.mp4`,
  single- vs multi-video folders, rule_c off, `extra_videos`, Japanese tags in every form
  (`x.ja.srt`, `x.JP.ass`, `x.ja-JP.srt`, `x.ja_jp.srt`, `x.jap.srt`, `x.日语.srt`, `x.jpn.zh.srt`),
  `.ass/.ssa/.vtt`, `._A.srt`, `x.srt.3.tmp`, NFC vs NFD, case-insensitivity, the video missing from
  the listing, garbage input never raises.
- avsubs: `plan_tree` over a fixture tree mirroring the evidence table → the recognised films are
  done, the rest planned; unreadable subfolder still errors + skips; AC5 before ASR (subtitle dropped
  in → skipped, no spawn, lease released and handed on; listing error → skipped unreadable) and before
  a full film's submit; translate-only Start submits not re-listed; AC6 refused `.srt` / `.ja.srt` /
  empty pair → settles, our own `.ja.srt` discarded, a library one kept, files byte-identical; #189
  tracker ends terminal; failure streak unchanged.
- Jasna: `plan_subs` a/b from one listing, rule (c) off (the F1 scenario), missing output folder →
  empty listing (subs still on), other listing error → planning failed; media resolved from the
  listing (legacy vs new); `_setup_subs` does not re-probe; AC5 placement (no listing while the GPU
  is refused; `_carried` consumed); AC6; restores unaffected.
- `test_subs_job.py`: `_publish` never overwrites (real `os.rename` in a tmp dir; POSIX link path and
  the no-hard-link fallback via monkeypatch); tmp always cleaned; existing content byte-identical;
  `PublishResult` values.
- Round-2 cases: `ABC-123-JP.mp4` + `ABC-123-JP.srt` / `ABC-123-JP.chs.srt`, `Japanese_Wife_01.mp4` +
  `.srt`, Jasna `JAP-001-破解.srt` → recognised (F12); `Movie.mp4` + `Movie.part2.mkv` +
  `Movie.part2.srt` with an mp4-only filter → not credited to `Movie` (F17); output folder missing with
  a listable parent → empty listing; unreachable (parent unlistable / WinError 53-style
  `FileNotFoundError`) → planning failed (F13); listing helper raising anything → unreadable skip and
  the hold released (F14); Jasna translate-only submit re-check, and a skipped translate-only film
  lets the walk continue to the next `subs_only` film (F15); submit re-check under the lock after
  `publish_ja` ok (F15); unreadable skips → one alert per run (F16); `Movie.mp4` + `Movie.part2.mkv` +
  `Movie.part2.srt` with an mp4-only filter and rule (c) on → not recognised (F17); a root output
  folder (`\\host\share`, `Z:\`) with a transient `FileNotFoundError` → planning failed, and a
  case-variant configured path still found by the parent check (F18); `X-JP.mp4` + `X.jp.srt` →
  recognised (F19 residual, pinned).
- Texts: help strings EN + zh parity (existing i18n test).

## Handoff notes

One Opus pilot, tests first. The evidence table gives every name needed; no library access. Never
touch the live agent/config/library; no WhisperJAV/GPU; no network.
