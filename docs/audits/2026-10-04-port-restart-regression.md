# macOS 3.9.10 port restart regression

## Confirmed cause

The installed Agent 3.9.10 reports `port_in_use` after a quick restart even
when no service is listening. The release source at `5cf9ef0` does not contain
the socket restart repair from `41ba56b`, previously documented in
[the September startup repair](2026-09-13-startup-repair.md).

Both `claim_port()` and its advisory probe bind without `SO_REUSEADDR` in
3.9.10. An accepted connection that the backend actively closes can leave the
local API port in `TIME_WAIT`. The next bind fails with `EADDRINUSE`. The
reclaim warning incorrectly calls this an unidentified foreign process.

## Installed-app evidence

Observed on macOS on 2026-10-04, using the existing installed 3.9.10 app:

- Before launch, neither configured API port had a listener and no TaskPaw
  process was running. Both addresses could bind and listen.
- Launch succeeded. The installed TaskPaw backend owned network port 5678 and
  control port 5681; `/ping` returned version 3.9.10 and the desktop connected.
- After an HTTP connection with `Connection: close` and graceful backend
  termination, `lsof` found no listeners. A bind followed by listen failed
  with errno 48 without address reuse, but succeeded with address reuse on
  both configured addresses.
- Immediately reopening the installed app reproduced the operator's error.
  `netstat` showed local control port 5681 in `TIME_WAIT`, with no listener.
- Once the old TCP state expired, reopening the same app succeeded again.
  The desktop was left showing its connected local API and loaded monitors.

This establishes a reproducible TCP-state failure, not evidence of a foreign
backend. The existing application, config, credentials and event lineage were
not replaced or reset. Normal monitoring continued when the app was running.

During diagnosis, Cmd-Q also left the identified bundled backend running after
the desktop exited. It was explicitly stopped gracefully before reproducing the
TCP-state failure. This is a separate desktop lifecycle observation; the socket
repair does not change the shell's shutdown handling.

## Repair

- Restore POSIX `SO_REUSEADDR` and require both bind and listen to succeed.
  Do not enable `SO_REUSEPORT`; preserve Windows socket options.
- Make the advisory probe use the same socket path as actual startup.
- Report an unavailable port without claiming a foreign process exists.
- Distinguish address/permission failures from real port conflicts, retaining
  the original OS error for the desktop startup protocol.
- Preserve 3.9.10's control credentials, authenticated readiness handshake,
  event state admission and state-lease cleanup.

Regression tests actively close a real accepted TCP connection and immediately
rebind the listening address. Separate tests prove that real listeners still
block startup (with or without address reuse), remain reachable after failed
probes, and that failed control binds release the network socket and event-state
lease. The restart regression failed against the unmodified release source.

The repair is included in a locally rebuilt 3.9.11 macOS arm64 package. The
installed 3.9.10 application has not been replaced.

## Windows startup recovery

The operator also reported Windows 3.9.10 opening without a window. Inspection
confirmed that fatal startup errors only reached stderr in the windowed Windows
binary, while the desktop migration/initialization entry was macOS-only. The
operator's Windows investigation independently confirmed old-format or absent
event state as the trigger on that machine.

3.9.11 adds native Windows error dialogs and explicitly confirmed migration or
initialization for the owned bundled Agent. Migration retains the configured
identity and event counter; it does not take the new-pairing initialization path.
Confirmation defaults to No, dialog failure cannot count as consent, and the
existing one-attempt and damaged-state refusal rules remain in force. Failed
Windows backend jobs are closed before the offline helper, and both that helper
and the restarted backend receive owned kill-on-close jobs.

The shared recovery tests and Windows dialog policy tests pass on macOS. The
actual Windows-only dialog, helper and Job Object modules also type-check for
the Windows GNU target. This is not a Windows executable build or native runtime
acceptance; the Windows child-tree regression is prepared for a native runner.

## Local package verification

- Application, Python backend and UI all report source version 3.9.11.
- macOS arm64 app and all 77 native archive entries passed ad-hoc signature and
  architecture verification.
- The socket module extracted from the actual frozen backend passed three
  immediate-restart cycles and still refused a real live listener.
- The app ZIP and read-only DMG were extracted/copied and their signatures,
  executable modes and backend hashes checked again. The DMG checksum passed.
- The package has not been notarized, installed or given full app-runtime /
  clean-machine acceptance. The active production Agent was left unchanged;
  no disposable-runner isolation assertion was fabricated on this shared Mac.

## Validation

- `uv run pytest`: 4179 passed, 85 skipped. One existing dependency deprecation
  warning from FastAPI/Starlette's HTTPX test client.
- Post-change focused socket/reclaim/agent/launcher tests: 222 passed.
- Desktop startup and recovery protocol tests: 40 passed.
- After the Windows changes/version bump: focused Python checks 169 passed,
  1 skipped; desktop Rust tests 31 passed, 1 ignored; Windows startup module
  cross-target type-check passed; frontend production build passed.
- `uv lock --check`, Ruff lint/format, mypy, V2 syntax checks and
  `git diff --check`: passed.
- Work is prepared on `fix/desktop-port-restart`, based on release source
  `5cf9ef0`. Native remote package builds require submitting the repaired source;
  installation and public release are separate actions.

## Hub package and live connectivity follow-up

On 2026-10-05 (Asia/Tokyo), the operator requested a matching Hub package. The
macOS arm64 Hub app was compiled with the Hub role and bundle identifier at
3.9.11, then packaged as an app ZIP and DMG. The embedded backend hash matches
the Agent build. Both containers passed signature, architecture, extraction and
checksum verification. This remains an ad-hoc local build without notarization
or full app-runtime acceptance; it has not replaced the running Hub.

The Hub, Hub CLI, film aggregation, event cursor compatibility, bootstrap and
version suites passed: 236 tests. The frontend suite passed all 640 tests across
25 files.

Live inspection of SunnyPig confirmed its Hub backend is 3.9.8. BlackGoldPig was
running Agent 3.9.10 on its existing configured port 5678, while Hub polling
received HTTP 401. After the operator restored the matching Agent token, a
request from SunnyPig using the Hub's stored polling credential returned HTTP
200 for both ping and status. The Hub dashboard then displayed BlackGoldPig as
online with current metrics. No registration, event-state reset or Hub credential
change was required. The older-Hub version warning is separate from that
authentication failure.

An existing remote 3.9.11 build at `306e0c7` was inspected. It does not contain
the Windows native error/recovery dialogs added in this repair tree, so its
successful Windows packaging is not evidence that this repair has passed native
Windows acceptance. A new Windows build of the repaired source is still needed.

The operator subsequently installed the local Hub package on SunnyPig. Both
installed executable hashes matched the verified package, and the installed
app passed deep/strict signature verification. Gatekeeper initially blocked the
unnotarized app. After the operator explicitly authorized its app-specific
exception, the 3.9.11 Hub dashboard opened and connected to its local API. Live
polling then showed all seven registered machines online, including ThunderPig
and BlackGoldPig, with the older-Hub version warning absent. This verifies the
observed upgrade launch and polling; it is not clean-machine or notification
delivery certification.

## Native Windows build and delivery

The operator authorized committing and pushing the repair branch. Commit
`f5d56374e9991cac4a4aa8b17b10fe4a93e91852` was built by release run
`37215547511`. The Windows job completed successfully, producing both NSIS and
MSI installers for 3.9.11. Its migration/first-install/version checks passed all
47 tests. Native Rust tests passed 26 tests, including recovery policy, Unicode
dialog handling and helper descendant cleanup after success or timeout. One
pre-existing Python credential interoperability test was ignored because it
requires dedicated production-writer fixtures.

The Windows installers were downloaded and copied alongside the Hub DMG into
the operator-requested desktop delivery folder. Copy hashes matched and a
checksum manifest and separate validation records were supplied. No Windows
signing certificate was configured, so the installers are unsigned. Target
Windows upgrade installation remains an operator acceptance step; the native
build and tests do not establish that installation result.
