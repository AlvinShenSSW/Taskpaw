# Complete Mac Hub package

## Scope and frozen contract

The operator installed the monitoring Hub on another Apple-silicon Mac and saw
the connection waiting page, then requested one complete frontend/backend
package. The existing app already embeds both the React/Tauri interface and the
frozen Hub backend. The missing usability step is installing its independent
service without typing a manager action. Preserve the original desktop/service
contract in `2026-10-26-mac-hub-desktop.md`, amended as follows.

1. Deliver one Apple-silicon DMG containing the full Hub app, an explicit
   `Install Hub.command` entry, the existing service manager, and short Chinese
   installation instructions. No Python, Node, repository checkout or separate
   backend download is required on the destination Mac.
2. Use the normal two-step DMG flow: drag the app to Applications, then double
   click the installation entry. The new entry selects the same installed app
   locations as the existing manager and invokes its fixed `hub-service install`
   command without interactive action input. Only on success does it open that
   exact installed Hub app. Missing app or failed service installation produces
   a visible, actionable failure and does not open the dashboard as success.
3. Repeated installation delegates to the existing idempotent service manager.
   Keep its signature, ownership, conflict, rollback, configuration/database
   preservation and stop/resource bounds. No new app updater/copy transaction,
   privileged installer, daemon, framework or dependencies. Existing drag-to-
   Applications replacement remains the app update operation.
4. The GUI stays connect-only. Merely opening or closing it cannot install,
   start, stop or restart the service. Update its waiting-page instruction to
   name the explicit installation entry; retain status/log guidance for a
   service that was already installed.
5. Package verification checks the exact executable installation-entry bytes
   and instruction file as well as the app and existing manager. Controlled
   command tests exercise success, missing app, failed installation, paths with
   spaces and repeated invocation. Native hosted qualification proves the
   shipped entry drives the real frozen service with isolated state and fake
   peers; it must not open a real GUI in unattended tests or modify this Mac.
6. Integrate the current main into this branch only, resolving the HubDashboard
   overlap while preserving main's independent-version/protocol compatibility policy and this branch's
   freshness/notification UI. Inherit main's current version unchanged; no new
   version allocation and no edits to another session's branch. The previously
   observed Windows CI failure requires separate evidence-based triage.

Publication stays in PR #327 with a new verified Desktop DMG. No merge, local Hub
deployment, live Agent events/acks, real notifications, Agent replacement, media
execution, or notarization claim. Prior artifacts remain intact. The other Mac
is not remotely accessible here, so its actual installation/GUI/login behavior
remains field-unverified until reported or separately observed.

## Approach and verification

Add a short standalone shell entry next to the existing manager. It takes no
operator action arguments, quotes paths, checks the installed executable,
invokes it synchronously, and uses macOS `open` only after success. Keep terminal
failure output readable; no shell eval or token arguments. Its CLI dependencies
are existing macOS shell/open plus the bundled backend; executing it never
downloads code. Tests use fake executables and open-command fixtures in temporary
directories, never the production Applications tree.

The generator remains `uv run python scripts/build.py` with Hub role on the
existing fresh hosted Mac builder. Extend the existing staging/verification and
native Hub smoke rather than adding a packaging pipeline. The native smoke may
inject a harmless `open` executable through its controlled PATH so installation
can be qualified without claiming a real window opened. This substitution is
test-only and must not bypass the actual frozen service manager.

| Files | Allowed changes |
| --- | --- |
| `scripts/Install Hub.command`, concise package instructions | Fixed installation entry and user steps |
| `scripts/macos_release.py`, `scripts/macos_hub_smoke.py` | Stage/verify shipped files and exercise entry in isolated native qualification |
| Hub waiting asset, deployment/signing guides | Name the complete package's explicit entry |
| Focused packaging tests | Behavioral RED/GREEN coverage and correct platform permission assertions |
| `ui/src/views/HubDashboard.tsx` under `taskpaw_v3` | Only resolve current main integration preserving both features |

External assumptions: target Gatekeeper acceptance of an ad-hoc downloaded
command/app is not established by local tests; retain the existing ad-hoc caveat.
Current-user GUI login and app installation location remain the manager's
documented prerequisites. No automatic quarantine removal, permission widening
or service-state deletion is part of this change.
