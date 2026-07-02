# Design: startup port takeover from a stale same-app instance

Date: 2026-07-02 · Branch: `feat/port-takeover-stale-instance`

## Problem
Every time the operator installs a new version, launch fails with
`PortInUseError` ("backend did not start within 30s / port in use") because the
**previous version's backend is still running** and holds the agent/hub ports. The
operator has to hunt down and kill the old process by hand each update.

## Fix (what the operator asked for: "clear it on startup")
On startup, before claiming its ports, the backend **reclaims a port that is held by
THIS app's own stale backend** — terminates that old instance and waits for the
socket to free, then binds. Applies to the agent (network + control ports) and the
Hub (API port).

## Safety — the critical constraint
It must **never** kill a foreign process that merely happens to sit on the port
(that would be dangerous). So the takeover only fires when the holder is
**positively identified as this app's own backend of the same role**:

- `net._listener_pids(port)` — psutil enumerates LISTEN sockets on the port.
- `net._is_our_backend(proc, role)` — true only if the process name is the
  PyInstaller sidecar (`taskpaw-backend[.exe]`) **or** a from-source
  `backend_main.py`, **AND** the `role` (`agent`/`hub`) is in its argv. A different
  role (agent must not kill a hub) or any other process → not ours → **left alone**.
- If the holder is foreign/unidentifiable, `reclaim_*` is a no-op and the existing
  `claim_port` still **fails loudly** — the "refuse to start if a real conflict
  exists" contract (constitution §3) is preserved for everything that isn't us.

Termination is graceful: `terminate()` → wait → `kill()` only if it won't exit;
then poll `port_available` until the socket frees (bounded). psutil errors /
missing psutil / access-denied all degrade to "did nothing" (never crash startup).
All actions are logged (§4).

## Where
- `taskpaw_v3/core/net.py`: `reclaim_port_from_stale_instance(host, port, *, role,
  what)` + helpers `_listener_pids` / `_is_our_backend`. psutil is a module-level
  optional import (mockable in tests; psutil is already a base dependency).
- `agent/server/launcher.py run_agent`: reclaim the network + control ports
  (role=agent) after the exposure guard, before `claim_port`.
- `hub/server/app.py run_hub`: reclaim the API port (role=hub) before `claim_port`.

## Tests (`test_net_reclaim.py`, fake psutil — no real kills)
- Reclaims a stale same-role backend (terminate called, returns True).
- **Leaves a foreign process (nginx) untouched** (no terminate, returns False).
- Wrong role (agent won't kill a hub backend) → untouched.
- From-source `backend_main.py` matched.
- No psutil → no-op.
- `_is_our_backend` name+role matching.

## Constitution gate
- §1 Scope: V3 only; V2 frozen/untouched.
- §3 Reliability/ports: still fails loudly on a genuine (foreign) conflict; only a
  self-owned stale instance is reclaimed. Clean termination, no zombies.
- §5 Testing: behaviour covered; ruff/mypy/pytest green.
