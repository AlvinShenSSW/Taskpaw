# Design: #145 Harden the auth-disabled default (V3)

Date: 2026-07-02 · Issue: #145 · Branch: `sec/145-auth-hardening`

Empty bearer token = auth disabled (V2 parity: `token_ok` returns True on an empty
token). The issue asks for better defaults on a network API with no token: a **loud
startup warning + a UI banner**, keeping `token_ok` V2-compatible.

## What already exists (do not duplicate/weaken)
`core.net.guard_bind_exposure` (#114) already **hard-refuses** at startup:
- a wildcard bind (`0.0.0.0` / `::`),
- a public/WAN bind (even with a token),
- a **non-loopback bind with an empty token**.

So the *dangerous* "reachable off-host, unauthenticated" state cannot run — it
raises before the socket is claimed. The remaining gap #145 fills is **visibility**:
when auth is disabled at all (necessarily a loopback bind, given the guard), nothing
tells the operator. This change surfaces it — additive, and it does **not** relax the
#114 refusal.

## Changes (all additive; default-safe)
1. **`core/auth.py`** — add `auth_disabled(configured_token) -> bool` (empty/
   whitespace → True). `token_ok` is unchanged (V2-compatible).
2. **Startup warning** — the agent launcher (`agent/server/launcher.py`) and the Hub
   startup (`hub/server/app.py`) `log.warning(...)` once when auth is disabled,
   stating the posture ("no api_token set → /status and /events are unauthenticated;
   the bind guard keeps this loopback-only — set a token to bind a LAN address").
   Emitted *after* the exposure guard, so it only ever describes the allowed
   loopback-disabled case.
3. **Expose state** — the agent console reads `/control/config`; add an explicit
   `auth_disabled: bool` field (derived from the config token before it is masked).
   No secret is leaked (the token itself stays masked `***`).
4. **UI banner** — `ui/src/views/AgentConsole.tsx` renders a dismissible-less MUI
   `<Alert severity="warning">` when `config.auth_disabled` is true, with i18n
   strings (en/zh). Informational, not alarming — the API is loopback-only.

## Why not "generate a token on first run" (option 1)
That changes the runtime contract (a token suddenly required by pollers/Hub) and can
break existing loopback/dev setups silently — not safe-direction for an AFK change.
Option 2 (warn + banner) is additive and reversible, and pairs with the existing
#114 refusal. Recorded as the deliberate choice.

## Test plan (TDD)
- `auth_disabled("")`/`"  "` → True; `auth_disabled("t")` → False; `token_ok`
  behavior unchanged (regression test kept).
- `/control/config` includes `auth_disabled` true when token empty, false when set,
  and never leaks the token (still `***`).
- Startup warning: capture logs — warns when token empty, silent when set. (Guard
  still refuses non-loopback+empty, so that path raises, unchanged.)
- UI: a vitest asserting the banner shows when `auth_disabled` and hides otherwise.

## Constitution gate
- §1 Scope: V3 only; V2 untouched; #114 guard untouched.
- §2 Security: strictly additive visibility; `token_ok`/guard unchanged — no
  weakening of "network-facing HTTP requires auth". Token never logged/exposed.
- §5 Testing: behavioural changes each covered by a test.
