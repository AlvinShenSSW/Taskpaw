# #106 Hub /status & /events Bearer auth — design (2026-06-29)

Gate the Hub's read API (`/status`, `/events`) behind a Bearer token, mirroring
the agent's already-gated network API. Issue:
[#106](https://github.com/AlvinShenSSW/Taskpaw/issues/106) (raised by the #105
Kimi 终审). Backend-only.

## Problem

`taskpaw_v3/hub/server/app.py` exposes `/status` and `/events` with **no auth**.
After #96, `/status` returns each agent's full snapshot (monitors, metrics, host
state). `HubConfig` has only `polling_token` (the Bearer the Hub *sends* to
agents) — there is no inbound token for the Hub's own API. The arch diagram in
`AGENTS.md` says the Hub REST is "Bearer-gated", but the code never implemented it.

The agent already solves this exact problem: `agent/server/app.py`'s network app
gates `/status` + `/events` via `token_ok(config.api_token, Authorization)` and
returns 401, while `/ping` stays open. `token_ok` (`core/auth.py`) treats an empty
configured token as "auth disabled" (V2 parity).

## Design (mirror the agent)

1. **`HubConfig.api_token: str = ""`** — the inbound Bearer for the Hub's read
   API. Empty = auth disabled (V2 parity), same semantics as `AgentConfig.api_token`.
   Distinct from `polling_token` (outbound, Hub→agents).
2. **Gate `/status` and `/events`** in `create_hub_app` with
   `token_ok(config.api_token, request.headers.get("Authorization"))`; on failure
   return a 401 (`{"error":"unauthorized"}`, `WWW-Authenticate: Bearer`). `/ping`
   stays open (trivial reachability probe, no sensitive data) — identical to the
   agent.
3. **No frontend change.** `ui/src/api.ts` `get("hub", …)` already attaches
   `Authorization: Bearer <apiKey>` when an apiKey is present; the shell injects
   it from `TASKPAW_UI_TOKEN` (main.rs `init_script`). With the default empty
   `api_token`, the local dashboard keeps working unauthenticated; when an operator
   binds the Hub to a LAN address they set `api_token` **and** `TASKPAW_UI_TOKEN`,
   exactly as they already do for an agent.

### Why a local `_unauthorized()` in the hub app (not shared)

`token_ok` is shared (`core/auth.py`, framework-agnostic — pure hmac). The 401
JSONResponse helper stays in the web layer: the agent defines its own
`_unauthorized()` in `agent/server/app.py`. The hub app gets the same 5-line
helper rather than importing FastAPI types into `core/auth.py` (keeps core free of
a web-framework dependency). Minor duplication, deliberate — same call the
reviewers already accepted for the agent.

## Out of scope

- A startup "non-loopback bind requires a token" guard like the agent's
  `admin.update_config` exposure check. The agent enforces that on its **UI
  config-edit path**; the Hub has no config-edit API (its app exposes only
  `/ping`/`/status`/`/events`), so there's no equivalent UI surface to guard.
  Loading a LAN bind with an empty token from `hub.yaml` is the operator's
  explicit choice (same as the agent at startup). Could be a follow-up.
- Authenticating `/events` differently from `/status` — both get the same gate.

## Test plan (`uv run pytest`)

1. `api_token` set → `/status` and `/events`: no header → 401; wrong token → 401;
   `Bearer <token>` → 200.
2. `/ping` → 200 even with a token configured and no header (stays open).
3. Empty `api_token` (default) → `/status` 200 without a header (V2 parity; the
   existing `/status`/`/events` tests already exercise this path).
