# #124 Hub dashboard: manage agents (add/edit/remove + polling token) — design (2026-07-01)

Let a Hub operator add / edit / remove the agents the Hub polls, and set the
polling token, **from the dashboard** — no `python -m taskpaw_v3.hub add-server`
CLI needed. Issue: [#124](https://github.com/AlvinShenSSW/Taskpaw/issues/124).

## Problem

The Hub dashboard is read-only (Fleet / Events / Settings). Agents are registered
only via the CLI (`hub add-server`) and `polling_token` only via `hub.yaml`. A
user with just the packaged Hub app (esp. macOS) can't add a device or fix a port.

`HubStore` already has `add_server` / `list_servers` / `set_server_enabled` /
`remove_server` and `get_config` / `set_config`; `polling_token` is read from the
store (`get_config("polling_token", …)`). The Hub API (`create_hub_app`, port
5690) is read-only and Bearer-gated (#106). There is no Hub control API.

## Design

### Backend — mutation endpoints on the Hub API, Bearer-gated (reuse #106)

Add write endpoints to `create_hub_app`, each behind the existing `_auth`
(the #106 gate): empty `api_token` = open (loopback default, local-only); a
non-loopback bind already requires a token (#114), so on a LAN Hub only a
token-holder can mutate.

- `POST /servers` `{name, ip, port}` → `store.add_server`; 400 on blank name /
  bad port (1–65535) / blank ip / duplicate name (UNIQUE).
- `PATCH /servers/{id}` `{name?, ip?, port?, enabled?}` → `store.update_server` /
  `set_server_enabled`; 404 if the id doesn't exist, 400 on bad values / dup name.
- `DELETE /servers/{id}` → `store.remove_server`; 404 if absent.
- `PATCH /config` `{polling_token}` → `store.set_config("polling_token", …)`. The
  poller reads it live (`get_polling_token`), so no restart needed.
- `store.update_server(id, name?, ip?, port?)` — new: partial update of the
  non-enabled columns (enabled stays on `set_server_enabled`), UNIQUE-name safe.

**Why on 5690 and not a new loopback control port (like the agent's #57):** the
Hub GUI already talks to 5690, and #106 already Bearer-gates it; #114 forbids an
unauthenticated non-loopback Hub. So mutations are local-only by default and
token-gated on the LAN — no new port/config/handshake, and the dashboard needs no
rewiring. (The agent keeps a separate loopback control port because its LAN API is
*unauthenticated-by-default* read; the Hub's is already gated, so the split buys
nothing here.)

### Frontend — a "Manage agents" section in the Hub dashboard

In the Fleet tab (below the machine cards): a compact manager listing each server
(name / ip:port / enabled) with **edit** (name/ip/port inline), **enable-toggle**,
**delete** (confirm), an **add** form (name + ip + port, default 5680), and a
**polling token** field (saved to Hub config, with a hint to match each agent's
`api_token`). Calls via `api.ts` hub `send()` (POST/DELETE/PATCH); the 5s
`hubStatus` poll refreshes the list. Errors render inline; delete confirms first.
All strings via i18n (中/EN).

## Test plan

- `uv run pytest`: POST adds (and 400 on blank/bad-port/dup); PATCH edits + toggles
  (404 on missing id, 400 on dup name); DELETE removes (404 absent); PATCH /config
  persists `polling_token`; all gated (401 without token when `api_token` set);
  `store.update_server` partial-update + UNIQUE-name conflict.
- `npm test`: manager lists servers, add/edit/delete calls fire the right requests
  (mock), delete confirms, polling-token save.

## Out of scope

Hub-as-connect-only-client (#52); per-agent `api_token` (that's agent-side config).
Auth is unchanged (reuses #106); no new schema beyond `update_server`.
