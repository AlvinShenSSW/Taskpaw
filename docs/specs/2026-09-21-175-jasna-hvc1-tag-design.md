# #175 — Jasna output: retag HEVC `hev1` → `hvc1` so macOS can preview it

Date: 2026-09-21
Issue: #175
Driver: `/afk` (Claude implements → internal review → Codex 外门 (astra) → Kimi 终审).
Merge policy: operator authorized merge + release v3.2.1 for this run.
Scope note: mechanical, single-cause bug fix → brief design, one debate round (the
never-scale-down rule applies to the gates, not to design depth).

## Spec review

Every MP4 the `jasna` task publishes is unpreviewable on macOS. ffmpeg names the
HEVC sample entry `hev1`; Apple's AVFoundation only accepts `hvc1`, so Finder
thumbnails, QuickLook, QuickTime and Safari reject the file. Jasna's default codec
is `hevc` and our output container is `.mp4`, so this is 100% of restored files
(confirmed on 25 finished outputs). Lada fixed the same bug in its writer
(ladaapp/lada@ed2f09e). Jasna is a frozen binary with no codec-tag CLI switch, so
the correction belongs in the one place TaskPaw already owns: the publish step.

## Acceptance criteria

1. `retag_hevc_hvc1(path) -> str` in `taskpaw_v3/monitors/plugins/jasna.py` rewrites
   the ISOBMFF sample-entry type `hev1` → `hvc1` in place (four bytes), walking
   `moov → trak → mdia → minf → stbl → stsd` across **every** `trak` (a file whose
   audio or timecode track comes first is legal) and every `stsd` entry, skipping a
   track or entry it cannot follow rather than giving up on the file.
2. It writes only when the entry type is exactly `hev1` **and** that entry carries an
   `hvcC` child, and it re-reads those four bytes at the computed offset first.
   Returns `patched` / `already-hvc1` / `no-hevc-entry` / `unsupported:<reason>`.
3. It never raises, never guesses, and leaves an unrecognised file byte-identical.
   A box whose declared size cannot advance the cursor ends the walk; the number of
   boxes scanned is capped.
4. `_publish_current()` calls it on the **staging** file before `os.replace`. An
   `unsupported:` result logs a warning and the file is still published.
5. Version 3.2.0 → 3.2.1 in all six sources; CHANGELOG entry.
6. Tests: hermetic hand-built ISOBMFF fixtures (no ffmpeg in the suite) covering
   patch, idempotency, moov-after-mdat, 64-bit largesize, other codecs, missing
   `hvcC`, missing `moov`, empty/junk input, a zero-advance box size, an
   over-long declared size, a missing file, and both publish paths.

## Frozen issue contract

**In scope:** items 1–6. User-visible change: published files carry `hvc1`; nothing
else about the pipeline changes.

**Invariants:** constitution §2 (atomic publish keeps its `os.replace`; the retag
happens before it, on the staging name, so a partial write can never reach the final
name), §4 (no silent except — every guard logs or carries a why-comment; the walk
cannot loop), and the owner rules the plugin already carries.

**Non-goals:** touching `lada`; a config switch; re-encoding or remuxing; retagging
non-Jasna files as a product feature; an upstream Jasna patch.

**Causal boundary:** `taskpaw_v3/monitors/plugins/jasna.py`, `taskpaw_v3/tests/test_jasna.py`,
the six version files, `CHANGELOG.md`, this design doc.

## Assumptions

| # | Claim | Basis | Risk if wrong |
|---|-------|-------|---------------|
| B1 | Jasna's HEVC MP4s carry parameter sets only in `hvcC`, never in-band. | **Verified** on a real 1080p output and a synthetic NVENC clip by decoding the first packet: `SEI(39) + IDR(19)`, no NAL 32/33/34; `extradata_size=133`. | A renamed entry would be a non-conformant (though universally tolerated) `hvc1`; `ffmpeg -c copy -tag:v hvc1` does not strip in-band sets either, so the result would equal the remux regardless. |
| B2 | Renaming the sample entry is what makes Apple accept the file. | The `hvc1`/`hev1` distinction *is* the sample-entry name; this is exactly what Lada's fix sets. Not verifiable from Windows — the operator confirms on the Mac. | Files stay unpreviewable; nothing is lost or corrupted. |
| B3 | Jasna offers no codec-tag switch. | `jasna.exe --help` (0.10.0) and the encoder-settings key list: NVENC/AMF keys only, no `tag`. | We would be duplicating a setting Jasna could do itself; harmless. |
| B4 | The four-byte edit is safe on a multi-GB file. | **Verified**: patched synthetic file keeps its exact length and decodes to the same `framemd5` as the original and the ffmpeg remux; the write is guarded by re-reading the offset. | A wrong offset would corrupt metadata — mitigated by the `hev1` re-read guard and the `hvcC` requirement. |

## Approach

A minimal ISOBMFF box walk, not a parser: descend the six-level path, iterate the
`stsd` entries, patch the first `hev1` entry. `_iter_boxes` yields
`(type, box_start, payload_start, box_end)` and refuses anything malformed rather
than guessing — a declared size below the header length, or past the parent's end,
ends the walk, as does a scan cap. The publish step calls it on the staging file so
the atomic rename is untouched and a failure costs nothing.

Alternative considered and rejected: `ffmpeg -i staging -c copy -tag:v hvc1 out` —
correct but rewrites 5–23 GB per file for a four-byte change, adds an ffmpeg
dependency to the publish path, and (per B1) produces the same stream.

## Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Box walk mis-computes the type offset | low | corrupted metadata in one file | the structural descent plus the `hvcC` requirement are the real guards — every write lands at `box_start + 4` of a box the walk reached, and each child is clamped to its parent, so a write cannot escape the real `moov`; hermetic fixtures cover every layout. The `hev1` re-read before writing is a cheap tripwire for an arithmetic slip inside the iterator, not a check on the walk itself. |
| The video track is not the first `trak` | **observed in the wild** (ffmpeg writes audio-first on `-map 0:a -map 0:v`) | the fix silently does nothing and the file stays unpreviewable | scan every track; a track without a sample table is skipped, not fatal |
| A future Jasna writes `hvc1` itself | low | no-op | `already-hvc1` status |
| Non-HEVC output (h264/av1 via the codec field) | expected | no-op | `no-hevc-entry` |

## Test plan

See AC 6. Both publish paths are covered end to end: a staging file that is a real
(hand-built) `hev1` MP4 is published as `hvc1`, and an unparseable staging file is
still published unchanged with `_done` incremented.

## Out of scope

A one-off pass over an existing output folder is not a product feature; the same
helper can be called from a REPL for that (done once for the operator's 25 files).

## Debate record (round 1 → clean)

Critic: Opus subagent, 20 hand-built ISOBMFF fixtures, real ffmpeg files and
implementation mutations.

- **E-1 P1 Fixed** — `_find_box` took only the first `trak`, so an audio-first file
  (reproduced with `ffmpeg -map 0:a -map 0:v`) was left untagged and unreported. The
  walk now scans every track and every entry.
- **E-2 P2 Fixed** — an `hev1` entry without `hvcC` aborted the whole file instead of
  letting a later entry be patched.
- **E-3 P2 Fixed (doc)** — the re-read guard was credited in the risk table as the
  mitigation it is not; the row now names the structural guards and the mutation
  `noreread` is acknowledged as untested-by-design.
- **E-4 P2 Fixed** — `test_publish_retags_before_renaming` passed against a mutation
  that retagged *after* the rename; the test now asserts the path it receives and
  that the final name does not exist yet.
- **E-5 P2 Fixed** — `os.fsync` under `_launch_lock` measured 0.348 s for a 6 GiB
  output on NVMe (Windows flushes the whole file's dirty cache) and bought nothing:
  atomicity is the `os.replace`, and a crash before write-back leaves the staging
  name, which `plan_queue` never counts as done. Dropped; the lock-invariant
  docstring now names the retag.
- **E-6 minor Fixed** — added a `size == 0` fixture and a no-`trak` case; corrected
  the `size == 0` comment (ISO 14496-12 §4.2: last box *in the file*); the scan cap
  is documented as a backstop, since the `size < header_len` check is what
  guarantees termination.
- **E-7 minor Fixed** — the `unsupported:` warning is now asserted with `caplog`, and
  a successful retag logs at INFO so the log carries evidence the fix ran.
- **E-8 minor Fixed** — dropped a stale "D-13" citation pointing at #173's doc.
- **E-12 minor Fixed** — a missing staging file no longer produces two extra log
  lines on top of the `os.replace` failure.
- **E-9, E-10, E-11 — no finding.** No input was found that writes at a wrong
  offset; `ffmpeg -c copy -tag:v hvc1` was shown *not* to strip in-band parameter
  sets (so B1's fallback reasoning holds even for a future Jasna that emits them);
  and the 78-byte sample-entry header, 8-byte `stsd` prefix, 64-bit `largesize`,
  `moov`-after-`mdat`, `free`/`skip` siblings and multi-entry `stsd` were all
  verified against the real function.

Mutation checks after the fixes: reverting to first-`trak`-only fails 2 tests,
aborting on `no-hvcC` fails 1, and retagging after the rename fails 2.
