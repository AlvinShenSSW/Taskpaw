# Mac Hub desktop and independent user service

## Spec review and frozen contract

The operator requested an installable Mac monitoring Hub, with a desktop
dashboard and an independent background service, after delivery of the Mac Agent
DMG. This implements the Mac portion of #52 and the minimum service installation
needed to use it without a Python checkout. Home LAN, one user, one Hub; Windows
retains media execution, approval, archive and cleanup. The dashboard may keep
the existing local Hub device/notification configuration screens. Connecting to
the Hub does not give it ownership of the Hub process or remote Agent controls.

Publication is a reviewed PR and a verified Mac Hub installer; no automatic merge
or deployment. The previous explicit merge applied to PR #325 only. Do not
replace/restart the installed Agent, install the new LaunchAgent, consume live
Agent events or send real notifications on the operator's machine. Version stays
at the branch baseline; Windows #322/#323 own 3.10.3/3.10.4 and their branches.

Acceptance criteria:

1. A dedicated Mac Hub app never spawns, owns, restarts, reclaims or kills a Hub
   backend. Closing, quitting or crashing it cannot stop aggregation. Agent
   desktop ownership/close behavior remains unchanged.
2. The dashboard opens without a running Hub. Missing/invalid credentials show
   local connection/setup guidance; a new valid protected Hub descriptor allows
   connection. Service restart rotates credentials/boot identity and reconnects
   without retaining old query data. One bounded-frequency reader exits cleanly
   with the shell. Repeated identical failures do not rebuild a window repeatedly.
3. Connections use the existing protected role-bound descriptor, canonical
   loopback base and in-memory credentials. No token-bearing redirects or LAN
   control expansion. Preserve backend response validation. No extra consumer
   or acknowledger of Agent `/events`.
4. Existing fleet, durable Hub history and read-only media views remain usable.
   Show data age/last success/unreachable reason and the existing notification
   health (pending, oldest backlog, last delivery, failures/dead letters,
   disabled/missing token). Locally retained responses age while sampling fails
   or the machine sleeps; they must not remain current healthy evidence.
5. An explicit separate installer/manager installs the bundled frozen backend
   as one per-user LaunchAgent. No Python/uv checkout is needed. It runs after
   login independently of the GUI, restarts on failure with throttling, and has
   bounded logs. Opening the GUI never installs/starts this service.
6. Install/reinstall/upgrade/start/stop/uninstall operate only on this managed
   installation. Preserve existing YAML, DB, cursors, outbox and media boundary.
   Reject unrelated/conflicting managed paths or service registration. A failed
   or uncertain stop cannot authorize overwriting/removing runtime files. Repeat
   install of unchanged running bytes is harmless. Interrupted/failed upgrades
   leave a recoverable prior runtime; no DB rollback or destructive cleanup.
7. Service startup never reclaims an independently running Hub's ports. Existing
   DB lease and socket claims decide conflicts; failure must not kill another
   Hub, attach silently or start a second event consumer.
8. Native hosted Hub builds prove Hub role, actual Hub readiness, controlled
   polling/history/outbox, stop/restart/crash cleanup and installer integrity.
   Agent runtime smoke cannot stand in for Hub validation. Source tests, native
   packaging and actual installed GUI/service behavior are distinct evidence.

Non-goals: Windows Hub packaging, Agent UI/temperature work, version allocation,
new media actions, queue protocol/body persistence, exactly-once delivery,
enterprise service framework, system-wide/root daemon, pre-login or logged-out
availability, new broker/scheduler, automatic production installation.

## Existing evidence and approach

`src-tauri/src/main.rs` already supports explicit attach through an empty backend
command, validates a private descriptor, and stores an optional owned child.
Packaged Hub still takes the spawn path. Add a Mac Hub branch before command
resolution: no child and no restart supervisor. Use a small Hub connection module
to read the protected descriptor periodically, retain the last applied identity,
and rebuild the embedded dashboard when that identity changes. Use a packaged
local waiting page when unavailable. Preserve navigation restrictions; never
deliver credentials to a foreign page. Stop/join the reader on application exit.
The packaged Hub role cannot be turned into an owning Agent by a runtime override.

Reuse the existing dashboard. Add Hub-only health presentation/types in new
modules; avoid the Windows Agent/status/power components and version files.
Necessary shared API integration must be conditional on Hub requests and tested:
refuse redirects, bound outstanding request duration/body reads, and preserve
existing Agent behavior and public fields. Keep local Hub configuration separate
from remote media execution; the latter remains read-only as in PR #290.

HD-DES-01: existing HubSettings delegates pending changes to RestartBanner, whose
unsupported path says to quit/reopen TaskPaw. That is false for this client.
Keep the injected restart capability false and change only the HubSettings caller
to explain that pending settings require the explicit background-service restart
command (or stop/start); closing/reopening the dashboard does not apply them.
Do not change shared Agent restart hooks or invoke service restart from the GUI.

Add one Mac-only `hub-service` entry to the frozen backend and a small service
module. Ship an explicit install/manage command beside Hub.app in the DMG, with
the equivalent fixed CLI documented for ZIP installs. It reads the installed app
from a stable Applications location, copies and verifies the executable into a
private persistent HubService directory, and registers a fixed LaunchAgent label.
The window does not run this command. A private runtime copy survives moving or
closing the GUI app; explicit reinstall is the upgrade action.

Use one fixed plist, private ownership/permissions, no symlink traversal,
atomic replacement, a small manifest binding installed executable/hash and
expected plist, and a serialized install operation. Reject preexisting unowned
files/registration instead of adopting them. No arbitrary shell execution or
secret arguments. launchd receives an explicit argv and private existing TMPDIR;
it directly runs the foreground onefile backend. RunAtLoad/KeepAlive and a finite
ThrottleInterval supply login startup and crash recovery. Use finite ExitTimeOut.
Log through bounded rotation, with stdout/stderr configured deliberately.

The service run route reuses Hub startup with stale-port reclamation disabled.
It keeps existing config defaults/legacy DB refusal and the DB lease. Stop and
uninstall first unload the fixed owned job, then use bounded observation of this
private runtime's processes and release state. Never kill a PID obtained by
parsing launchctl text. On timeout/unknown cleanup leave runtime/config/history
intact and return an explicit incomplete result. Only remove owned registration
and runtime files after confirmed stop; never remove user YAML/DB/history.

Install stages new verified bytes before stopping the old managed job. Keep the
old runtime/manifest until the new registration starts successfully. Failure
restores the prior runtime where safe, reports the result, and does not roll back
data. The first install must reject a label already registered without this
installation's ownership record. Idempotent reinstall does not restart matching
running bytes. No new general service supervisor is introduced.

## Verified platform facts and remaining assumptions

Local macOS 27.0.1 manuals establish that `gui/UID` is the login domain;
`launchctl print` text is not a stable API; KeepAlive has throttling; nonzero
ExitTimeOut bounds the SIGTERM-to-SIGKILL interval; and process-group cleanup does
not prove separately-sessioned helpers have ended. `bootout --wait` can block
indefinitely and is not assumed available on the native macOS 15 builders.
Use ordinary bootstrap/bootout with external command deadlines and independent
resource checks. Login-session service lifetime is also documented by
[Apple](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html).

The lockfile pins PyInstaller 6.21.0. Its
[onefile lifecycle](https://pyinstaller.org/en/v6.21.0/advanced-topics.html)
waits for the child before extraction cleanup. A private persistent TMPDIR avoids
[temporary-directory cleanup](https://pyinstaller.org/en/v6.21.0/usage.html#defining-the-extraction-location)
affecting a long-running process. launchd starts the fresh copied binary; the
installer must not launch a lasting child inheriting its PyInstaller extraction
environment. The existing upstream helper exits on parent stdin EOF, in addition
to normal owned transport cleanup. Actual frozen crash/stop tests remain required.

Hosted runner GUI-domain availability, native desktop close automation and
Gatekeeper acceptance of copied ad-hoc binaries are not assumed. Probe the
qualified disposable hosted environment and record actual results. A missing
native prerequisite is explicit, not a unit-test pass. No local production
service is stopped to make this shared Mac qualify as a disposable test host.

## Files and execution surface

| Paths | Allowed changes |
| --- | --- |
| `taskpaw_v3/src-tauri/src/hub_connection.rs`, narrow `main.rs` integration, local waiting-page asset | Connect-only ownership and credential reconnect |
| `taskpaw_v3/hub/macos_service.py`, `packaging/backend_main.py`, narrow `hub/server/service.py` and `app.py` | Explicit installer/manager and non-reclaiming service entry |
| New Hub health/transport modules, `ui/src/views/HubDashboard.tsx`, `ui/src/components/HubSettings.tsx`, necessary conditional `ui/src/api.ts` integration | Freshness/notification evidence, correct background-service restart guidance and bounded local requests |
| `scripts/build.py`, `scripts/macos_release.py`, release workflow, Hub command template | Separate Hub artifacts and genuine native Hub smoke |
| Focused Python/Rust/UI tests, deployment/signing guides | RED-to-GREEN contracts and operational evidence |

Read existing #52, design section 7.1, current credential/ownership tests and
Windows PR #324/#326 file lists. Do not edit their branches or unrelated files.
Generate UI with existing npm build, sidecar and DMG with `scripts/build.py` on
fresh native hosted workers only. Retain outputs in ignored build/run storage;
new artifacts have role/architecture/commit in evidence, never overwrite old
Desktop installers. No tag/GitHub Release or live deployment is implied.

## Verification and risks

Tests first: explicit Hub role never resolves/spawns an owned child; absent,
invalid/wrong-role/unsafe descriptors; changed boot/token; stable failures;
reader cancellation; Agent lifecycle regression. UI fixtures cover stale/sleep,
disabled agents, auth/network errors, recovery, missing OpenClaw token, backlog
and dead-letter states, no redirect, deadline/body bounds and unchanged media
read-only behavior.
HD-DES-01 requires a HubSettings regression: pending settings in a connect-only
desktop show the background-service instruction, no misleading reopen message,
and no GUI restart request. Existing Agent restart tests remain unchanged.

Temporary-directory/fake-launchctl tests cover install twice, occupied/unowned
label/files, symlinks, partial writes/copy/signature failure, upgrade rollback,
timeout, uncertain stop, start after stop, uninstall preserving all data, and
serialization. Controlled Hub port/DB conflicts must not call reclaim or kill.

Native hosted fixtures use isolated HOME/config/DB/ports and fake Agent/OpenClaw.
Verify actual frozen Hub process identity, continuous samples/outbox around
client lifecycle, service restart, slow delivery stop, helper/port/DB-lease release
and installer hash/signature/role. Where real window automation is unavailable,
record it as field-unverified and retain a concrete manual check, not a fabricated
close acceptance. Production Hub installation, native GUI visual acceptance and
real notification delivery remain separately unperformed.

Main risks are incorrectly owning an external Hub, stale credentials/data after
restart, destructive installer recovery, and falsely labelling Agent smoke as Hub
evidence. The dedicated connect-only branch, protected descriptor, explicit
managed-path checks, fail-closed non-reclamation and role-specific native fixtures
are the corresponding controls. No additional platform or scheduling framework
is needed.

After the design critic closes supported blockers, implement the contract, run
targeted and existing regressions, self-review, independent internal review,
the operator-selected Kimi K3 outer gate, then final full Python/UI/static/native
checks and current PR CI. Leave the PR open; report actual artifact and field
boundaries separately.
