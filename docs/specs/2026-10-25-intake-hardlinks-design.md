# Same-volume intake hard links (#314)

The operator reserves INBOX exclusively for Taskpaw. New snapshots may share file data with INBOX; their directory entries remain separate. Existing snapshots and their serialized digests stay unchanged. Taskpaw remains 3.10.2.

## Intake and compatibility

- Both held roots already require local NTFS. Compare their native volume identities and use `os.link` for each same-volume file. Directories retain the current creation path.
- A fully linked package requires only the existing free-space reserve. Cross-volume or explicit unsupported-link errors use the existing verified copy path, enforcing package size plus reserve before fallback copying. Other errors retain their normal failure behavior.
- Carry a `linked` marker from the private-copy result into `SnapshotMember`. False is omitted from serialization. Linked files must have equal physical source/copy identities; directories and non-linked members retain independent identities.
- Retain the exact post-link change token for linked members, omitting absent metadata from legacy serialization. This is needed because creating a hard link changes NTFS metadata without changing content.
- Keep original admitted observations intact. Track only Taskpaw's own successful-link metadata changes for held-source rechecks, including multiple input names sharing a physical file. Hash linked bytes once while the existing source handles deny writers; retain size, identity and publication checks. Existing copy verification remains unchanged.

## Observation and processing

- Deferring dirty notifications/observations is limited to a locally owned active snapshot package; it remains seen. The held worker's final membership/content checks and the following scan retain real-change detection. Unknown owners and unrelated packages retain existing behavior.
- For completed linked snapshots, compare the exact stored member and admitted observation before normalizing Taskpaw-caused metadata changes: path, physical identity, size and modification time must still match.
- The captured post-link token is the fast path. Return/scrape can legitimately add links later; if only the change token differs, verify the held file against its stored hash before accepting it. Cache that exact verified token for the current process to avoid repeating a large read each scan. This is a bounded source-proof cache, not a new setting or journal.
- Apply the same proof before a delayed dirty notification can invalidate approvals. Unmatched members, content changes and incomplete observations retain existing invalidation. Do not ignore change tokens globally.

## Cleanup authority

- Keep exact approval-to-manifest root/path/identity/size/hash checks.
- Allow the workcopy/input physical alias only through the current film/input revision's already validated intake snapshot, matching an explicitly linked member and a distinct approved work path. Do not alter the global identity-key definition or add cleanup DTO flags.
- Release input references by root, native identity and exact case-folded path. Deleting one INBOX name must not release references to another name of the same file.
- Clear read-only on the identity/hash-verified native deletion handle before unlink, preserving other attributes. Both cleanup scopes delete only their own names. The intake reclaimer already clears read-only; its accounting must describe logical unlinked bytes rather than claim physical bytes freed.

## Validation and delivery

Use native Windows fixtures for shared file IDs, supported-link allocation behavior, source-name removal, source-content mutation, fallback/capacity and both read-only cleanup scopes. Exercise actual intake publication, immediate/delayed rescans and later processing-created links, followed by archive and both cleanup scopes. Pin an old snapshot's canonical/completion digests from the unchanged implementation. Preserve existing independent-copy corruption tests by explicitly forcing their fallback path.

Kimi K3 reviews the resulting change. Integrate into the existing controller branch, run the final full suite and PR CI, merge only when green, and build the controller installer without a version bump. After operator installation, use read-only observations of a new live intake's file identities and physical allocation; directory logical sizes count each hard-link name and cannot establish duplicate disk allocation.
