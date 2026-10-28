# Native macOS release builds

The release workflow builds native Mac installers on `macos-15` (arm64) and
`macos-15-intel` (x86_64). Agent and Hub are separate artifacts. In Actions →
release → Run workflow, select `role=hub` to build the Mac Hub desktop and its
independent service. The default `role=agent` and tag-triggered builds keep the
existing Agent matrix, including Windows. There is no Windows Hub package.

Hub artifacts are `taskpaw-hub-macos-arm64` and `taskpaw-hub-macos-intel`;
Agent artifact names retain the `taskpaw-agent-` prefix. Each Mac artifact
contains a DMG, an app ZIP and `macos-verification.json`. Raw `.app` uploads lose
executable file permissions; the ZIP preserves them. The Hub DMG includes the
separate `Manage Hub Service.command`; the ZIP uses the app's bundled CLI. See
[Hub installation and management](deployment.md#mac-hub-desktop-and-background-service).
These are Actions artifacts, not an automatic GitHub Release or deployment.

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
atomic isolation. The ordinary Agent/manual startup paths retain their existing
stale-instance reclaim behavior. The managed Hub service disables stale-port
reclamation, but its smoke also uses a fixed per-user LaunchAgent label and must
not run alongside a real installation.

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
uv run python scripts/build.py  # Agent by default
# Or build the Mac Hub on that qualified host:
TASKPAW_BUILD_ROLE=hub uv run python scripts/build.py
```

Select one role per build invocation; a Hub build does not install a production
service. Its qualification fixture temporarily installs and removes only its own
service in an isolated home on the disposable host.

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

For Agent builds, the isolated frozen backend must announce the right readiness
role/base and return actual host-metrics CPU/memory/disk/network values. This
exercises its Python, pydantic_core and psutil extensions. ZIP extraction and
read-only DMG mount/copy repeat signatures, architecture and actual readiness.
Agent fixtures use their own config and ephemeral ports, then stop only their
owned process group and verify released sockets. Hub builds use the separate
service qualification below; an Agent smoke result cannot qualify a Hub.

Formal mode requires accepted app and DMG notarization, staples/validates the
app before the final ZIP and validates the DMG ticket. Timeout/rejection is an
error, never success. The verification JSON excludes credential references,
account/history, tokens/descriptors and raw native output. **Notarization
traceability is incomplete:** durable submission-ID/submitted-hash/final-byte
mapping remains deferred. Accepted/staple booleans do not close that acceptance.

## Hub runtime verification

`scripts/macos_hub_smoke.py` exercises the actual frozen Hub backend through
`hub-service` on the qualified disposable native host. It copies the Hub app to
the fixture home's `Applications` directory, uses private configuration and DB,
and starts controlled Agent/OpenClaw peers. It checks the protected descriptor's
Hub role/base, authenticated current status, polling, `status.md`, local history
and outbox delivery to the fake peer. No production Agent queue or real OpenClaw
notification is used.

The fixture checks identical reinstall without restart, explicit service restart
with rotated boot identity, owned runtime crash recovery, helper cleanup, and
continued sampling while a fake notification send is slow. Stop must release
both ports and the DB lease and remove the live descriptor. Uninstall must
preserve the fixture configuration and history. Uncertain stop prevents fixture
runtime/data removal and fails the build rather than being labeled clean.

The same Hub qualification runs against the app and extracted ZIP/DMG copies.
DMG verification additionally checks the fixed install entry, service manager
and installation instructions. The native fixture runs the install entry with
the real frozen manager and a harmless replacement for `open`; this proves
installation ordering without claiming a real dashboard window opened. The report
records `role: hub`, source/architecture/signature evidence and a `hub_runtime`
section. A successful run may establish `service_lifecycle_verified`,
`polling_and_outbox_verified`, `crash_recovery_verified`,
`slow_send_stop_and_resource_release_verified` and
`client_disconnect_verified` for those fixtures. These are acceptance checks to
run; this guide is not a record that a particular build has passed them.

`client_disconnect_verified` means a short-lived authenticated HTTP client
closed while polling continued. It does **not** launch or close a Tauri window.
The report deliberately leaves `native_window_close_verified: false` and
`login_reboot_verified: false`. Record real GUI waiting/connection/reconnection,
close/quit/crash while aggregation continues, login/reboot startup, and visual
acceptance separately on clean native Macs. Do not turn those fields true from
Rust tests, a successful build or HTTP-only smoke. A hosted runner lacking the
required GUI login domain must fail service qualification; unit tests cannot
substitute for it.

## Manual distribution boundary

Actual formal credentials, clean dual-architecture downloads, installation and
Gatekeeper require operator evidence; local unit tests or build-host smoke do
not establish them. On corresponding clean disposable/exclusive Intel and arm64
Macs, record source/version/architecture, signing identity/Team consistency,
hardened runtime/timestamp, accepted notarization and app/DMG ticket validation,
Gatekeeper assessment and GUI start. For Agent, record sidecar readiness/native
metrics and owned-backend cleanup on desktop close. For Hub, record the
independent service's readiness and continued sampling when the desktop closes,
plus the separate GUI/login checks above. Build-host cleanup does not establish
either installed desktop lifecycle.

Ad-hoc Hub and Agent artifacts are not Developer ID signed or notarized; a valid
`codesign` check alone does not establish Gatekeeper acceptance or successful
launch of the copied Hub runtime. For ad-hoc downloads, expect an
unidentified-developer warning. If the operator
trusts the artifact, follow Apple's current
[Privacy & Security / Open Anyway guidance](https://support.apple.com/102445).
Since #357 the ad-hoc Hub's `Install Hub.command` and `Manage Hub Service.command`,
once the operator has approved the command itself, clear `com.apple.quarantine`
from the one installed `TaskPaw Hub.app` before running its backend, so macOS asks
once rather than for the command, the backend and the window. That is an operator
convenience for a trusted ad-hoc artifact, not Gatekeeper acceptance: never remove
quarantine or disable Gatekeeper to manufacture formal-build acceptance evidence.
