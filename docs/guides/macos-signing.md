# Native macOS release builds

The release workflow builds Agent installers on `macos-15` (arm64) and
`macos-15-intel` (x86_64). Windows keeps its existing installer/signing path.
Mac artifacts are a DMG and an app ZIP; raw `.app` uploads lose executable file
permissions. The ZIP preserves them. These are Actions artifacts, not an
automatic GitHub Release or deployment.

## Signing modes and migration

Unset, empty and whitespace-only signing inputs select **ad-hoc signing**.
Ad-hoc is signed, but is neither Developer ID distribution nor notarization.
Partial configuration is an error. A failed formal build never retries ad-hoc.

Traditional `APPLE_CERTIFICATE`, `APPLE_CERTIFICATE_PASSWORD`, `APPLE_ID` and
`APPLE_PASSWORD` automation is retired, including a complete six-variable group.
Tauri CLI 2.11.3 passes these passwords through native command arguments, which
violates the repository security contract. Alternate Tauri API-key variables are
also rejected; do not mix credential transports. Remove the deprecated inputs
and migrate explicitly. The identity display name must be replaced by its exact
certificate fingerprint. Diagnostics name fields without printing values.

A formal build requires **all four public references**:

| Variable | Value |
| --- | --- |
| `APPLE_SIGNING_IDENTITY` | Exact 40-hex SHA-1 fingerprint of Developer ID Application certificate |
| `APPLE_TEAM_ID` | 10 uppercase alphanumeric team identifier |
| `TASKPAW_MACOS_SIGNING_KEYCHAIN` | Absolute path to an existing private, owner-only keychain outside the repository/artifacts |
| `TASKPAW_MACOS_NOTARY_PROFILE` | Existing profile name; 1–128 ASCII alphanumeric/dot/underscore/hyphen characters, starting alphanumeric |

The operator pre-provisions the private keychain/profile through approved Apple
interfaces. It must already be unlocked, be in the user's existing search list,
and allow unattended `codesign` access to that exact identity. The build never
imports/unlocks credentials, changes a search list/ACL/trust setting, or deletes
an operator keychain/profile. `notarytool` uses only profile/keychain references.
[Apple TN3147](https://developer.apple.com/documentation/technotes/tn3147-migrating-to-the-latest-notarization-tool)
describes secure password prompting for profile provisioning; do not put a
password in command arguments or shell history.

GitHub-hosted VMs do not automatically contain this pre-provisioned material.
They build ad-hoc by default. Repository reference variables cannot provision a
keychain: a configured formal mode without its keychain/profile fails early.
The hosted workflow also detects deprecated secret presence and fails rather
than silently ignoring an old formal configuration. Formal automation on hosted
VMs needs a separately approved safe provisioning process; this guide does not
add one. Formal builds are supported on operator-provisioned **dedicated clean
disposable native Intel/arm64 runners or VMs**, not a shared signing Mac.

## Required runtime-smoke isolation

Every actual app, ZIP and DMG backend smoke needs a clean dedicated disposable
host with **no real TaskPaw and no independent concurrent TaskPaw launch** during
the entire session. A temporary HOME and released ephemeral ports do not provide
atomic isolation: production startup has an existing stale-instance reclaim
path. This change does not alter that production behavior.

The reviewed fresh hosted job sets
`TASKPAW_MACOS_SMOKE_ISOLATION=github-hosted-fresh` and validates native GitHub job
metadata. Its fixtures run serially, and no other step starts TaskPaw. Generic
CI=true, self-hosted/reused runners or merely copying that flag are insufficient.

For a genuinely disposable native host, the trusted operator/provisioner creates
an owner-only, nonsymlink JSON attestation outside the repository/artifacts,
then sets `TASKPAW_MACOS_SMOKE_ISOLATION=disposable-native` and
`TASKPAW_MACOS_SMOKE_ATTESTATION` to its absolute path. The exact record is:

```json
{
  "version": 1,
  "kind": "disposable-native",
  "session_id": "<UUID for this exclusive session>",
  "boot_session_uuid": "<current kern.bootsessionuuid>",
  "target": "<aarch64-apple-darwin or x86_64-apple-darwin>",
  "dedicated": true,
  "clean": true,
  "disposable": true,
  "no_real_taskpaw": true,
  "no_independent_taskpaw_launches": true
}
```

Record the disposable runner/VM identity and the controller enforcing exclusivity
in the manual check record. The file is a trusted execution assertion, not proof
of physical isolation, and must not relabel a shared machine. The build cannot
self-issue it. Boot/target/flags and immutable session contents are checked before
every backend spawn; a cooperating-helper lock only serializes these helpers.
Missing/shared/invalidated context refuses runtime verification before spawning.
Structural/signature-only evidence is partial and cannot pass the full build
or authorize installer upload. `--skip-tauri` does not run the backend and makes
no runtime or installer acceptance claim.

## Build and automatic verification

On the qualified native host, install the locked build dependencies and run:

```bash
uv sync --frozen --group dev --extra build --extra v3
uv run python scripts/build.py
```

Optional `TASKPAW_BUILD_TARGET` must match the native CPU/Python/rustc host.
Cross/Rosetta/universal builds are rejected. Mac `TASKPAW_BUNDLE_TARGETS` accepts
`app`, `dmg`, `app,dmg`, or empty (both). Version/role stamping is unchanged.

PyInstaller signs native archive contents and the onefile backend before
embedding them. Formal code shares the selected Developer ID/Team and an empty
entitlement profile with library validation enabled. The historical ad-hoc
backend main-executable permissions remain for compatibility; this is not a
claim that each is necessary. Native archive code is classified by its Mach-O
header: main executables retain that exact profile, while dylibs and bundles
require an empty profile (macOS 15 signing omits library entitlements by default).
The build does not force library privilege grants. Tauri builds the app with signing disabled, then the build helper
signs nested code inside out and verifies every native archive entry. It never
uses deep signing as a repair.

The isolated frozen backend must announce the right readiness role/base and
return actual host-metrics CPU/memory/disk/network values. This exercises its
Python, pydantic_core and psutil extensions. ZIP extraction and read-only DMG
mount/copy repeat signatures, architecture and actual readiness; nothing is
installed into Applications. Fixtures use their own config and ephemeral ports,
then stop only their owned process group and verify released sockets.

Formal mode requires accepted app and DMG notarization, staples/validates the
app before the final ZIP and validates the DMG ticket. Timeout/rejection is an
error, never success. The verification JSON excludes credential references,
account/history, tokens/descriptors and raw native output. **Notarization
traceability is incomplete:** durable submission-ID/submitted-hash/final-byte
mapping remains deferred. Accepted/staple booleans do not close that acceptance.

## Manual distribution boundary

Actual formal credentials, clean dual-architecture downloads, installation and
Gatekeeper require operator evidence; local unit tests or build-host smoke do
not establish them. On corresponding clean disposable/exclusive Intel and arm64
Macs, record source/version/architecture, signing identity/Team consistency,
hardened runtime/timestamp, accepted notarization and app/DMG ticket validation,
Gatekeeper assessment, GUI start and actual sidecar readiness/native metrics.
Normal desktop-close ownership acceptance depends on the separate lifecycle
work; do not infer it from this smoke cleanup.

For ad-hoc downloads, expect an unidentified-developer warning. If the operator
trusts the artifact, follow Apple's current
[Privacy & Security / Open Anyway guidance](https://support.apple.com/102445).
Do not remove quarantine or disable Gatekeeper to manufacture acceptance.
