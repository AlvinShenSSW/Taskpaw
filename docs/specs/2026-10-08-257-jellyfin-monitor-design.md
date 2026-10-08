# #257 — V3 Jellyfin service monitor plugin

Status: design v2 (2026-10-08; v1 revised after debate round 1 — D1–D7; round 2 revalidated them and added wording corrections D8–D10, no decision change). Author: Claude (driver). Issue: #257.

## Spec review

The operator wants a monitor that answers "is this machine's Jellyfin media
server actually running properly?". The generic `tcp_check` (port accepts a
connection) and `process` (a process matches) cannot tell a healthy Jellyfin
from an open port owned by something else, or from a Jellyfin that is up but
unhealthy. The only target today is one Mac running the Jellyfin app on its
default port 8096; acceptance is on the macOS agent only.

Core need: one passive, self-describing `jellyfin` plugin that probes
Jellyfin's unauthenticated HTTP endpoints, maps the answer to
`ok` / `degraded` / `error`, and emits transition events — plus the existing
"add a plugin" UI touchpoints (labels, icon), docs and a version bump.

## Evidence (target Mac, 2026-10-08, Jellyfin server reporting version 12.2.0)

Verified by running the requests against the live server:

- `GET /health` → `200`, `text/plain`, body `Healthy`, no auth.
- `GET /System/Info/Public` → `200` JSON object with `ProductName:
  "Jellyfin Server"`, `Version`, `ServerName`, `Id`,
  `StartupWizardCompleted: true`, no auth.
- The server listens on TCP `*:8096`.

## Corrections to the issue text (repository evidence)

- **Version.** The issue says "bump to 3.9.9". `origin/main` is already at
  3.9.10, so this change ships as **3.9.11**.
- **`dedupe_key`.** The issue asks for a stable `dedupe_key`. The supervisor
  records every delivered `dedupe_key` in a per-monitor seen-set and drops any
  later event carrying the same key (`taskpaw_v3/monitors/supervisor.py`,
  `seen_dedupe`). A stable key would therefore swallow the alert for a second
  outage. The plugin passes `dedupe_key=None`, exactly like `tcp_check`, and
  relies on its own previous-state tracking to emit once per transition.

## Frozen issue contract

Acceptance criteria:

1. `jellyfin` is registered in `default_registry()` and listed by
   `plugin_catalog()` with a renderable form schema. Unknown config keys are
   rejected. `base_url` rejects: a scheme other than `http`/`https`, a missing
   host, embedded credentials (`user:pass@`), a query or a fragment.
2. State mapping per the decision table below, covered by unit tests against a
   local throwaway HTTP server (no real Jellyfin, no network).
3. Event transitions per the table below, covered by unit tests; no event while
   the state is unchanged.
4. `check()` never polls or sleeps, never raises on any network / HTTP / decode
   / shape failure. Every socket operation is bounded by `timeout`, and each
   request's body read is additionally bounded by one `timeout`-long monotonic
   deadline (see "Time bound"). Redirects are not followed; no proxy is used.
5. UI: `monitorType.jellyfin`, `services.jellyfin` and the `base_url` field
   label exist in en + zh-CN; the i18n test list includes `jellyfin`;
   `ServiceIcon` has a `jellyfin` glyph distinct from the fallback.
6. README "What it monitors" table has a `jellyfin` row; CHANGELOG has a 3.9.11
   entry; the version is 3.9.11 everywhere the repo carries it.
7. All repo gates green (`uv lock --check`, pytest, ruff check, ruff format
   check, mypy, UI test / lint / build).
8. One manual run of the plugin's `check()` against the real Jellyfin on the
   target Mac returns `ok` with the right version and server name (recorded in
   the PR).

Invariants (from `docs/constitution.md` and the plugin contract):

- Standard library only; no new runtime dependency.
- No credentials: the plugin never sends an API key, cookie or auth header.
- Passive: it never starts, stops or reconfigures Jellyfin; `manual_start`
  stays the default `False`.
- No change to the monitor framework, the agent↔Hub protocol, V2, or any
  generic UI form code.

Allowed user-visible changes: a new "Jellyfin" entry in the add-monitor wizard
(with icon, description and a `base_url` field), its status/events once an
operator adds it, a README row, a CHANGELOG entry, version 3.9.11.

Non-goals: Windows/Linux acceptance or platform branches; authenticated
endpoints (sessions, transcodes, library scans, scheduled tasks); controlling
Jellyfin; adding the monitor to any machine's config, releasing or deploying;
a moomoo-style preset.

Causal boundary: one new plugin module, its registration, its tests, the
per-plugin UI label/icon maps and their tests, README, CHANGELOG, version files.

## Assumptions (not verified here)

- **Unhealthy responses.** Jellyfin's `/health` is the ASP.NET Core health
  endpoint; its documented defaults are `Healthy`/`Degraded` → 200 and
  `Unhealthy` → 503. Only `Healthy` was observed. Risk: none for correctness —
  the rule is "anything other than 200 + `Healthy` is not healthy", so it does
  not depend on the exact unhealthy shape.
- **Startup window.** While Jellyfin is still starting it may answer every path
  with a non-JSON 5xx page. Not observed. The decision table classifies "an
  HTTP answer we cannot identify because it is 5xx" as `degraded`, not as
  "not a Jellyfin server", so a restart shows as degraded → ok rather than a
  false "wrong service" alert.
- **`ProductName` across versions.** Observed `"Jellyfin Server"` on one
  version. The identity test is a case-insensitive "contains `jellyfin`" on
  `ProductName` to tolerate wording drift. Risk: a fork that renames the
  product reads as "not a Jellyfin server" (error); acceptable, visible in
  `detail`.
- **https with a self-signed certificate** fails verification and reads as a
  transport failure (row 1). No "skip verification" switch is offered.
- **Windows / Linux.** The probe is plain HTTP with no platform branch, but it
  is exercised only on macOS (CI macOS/Windows runners still run the unit
  tests, which use a loopback server).

## Approach

A single module `taskpaw_v3/monitors/plugins/jellyfin.py`, modelled on
`tcp_check.py` (transition events) and `comfyui.py` (stdlib HTTP, `_NET_ERRORS`).

### Config

`JellyfinConfig(BaseMonitorConfig)` adds one field:

- `base_url: str = "http://127.0.0.1:8096"` — validated with
  `urllib.parse.urlsplit`: scheme ∈ {`http`, `https`}; non-empty hostname;
  a parseable port; no username/password; no query; no fragment. A trailing
  `/` is stripped so a path prefix (`http://host/jellyfin`) composes cleanly.
  `urlsplit` silently tolerates leading whitespace and strips newlines, so the
  validator first rejects any value containing whitespace or an ASCII control
  character (rejected, not rewritten). The field is declared with
  `Field(title="Base URL", description=…)` so the English form label is not
  Pydantic's auto-generated "Base Url".

The request deadline is the shared `timeout` (one knob, as in `tcp_check`).

### Probe

`_fetch(url, timeout) -> _Reply` performs one `GET` through a module-level
opener built with `urllib.request.ProxyHandler({})` and
`taskpaw_v3.core.http.NoRedirectHandler`, reads at most 64 KiB, and returns
`(status, body)`. The empty `ProxyHandler` matters: a default opener honours
`http_proxy`/`https_proxy` from the environment, which would send a loopback
probe to the proxy and report a healthy local server as unreachable (debate
D2, reproduced). The monitor always talks to the configured host directly; an HTTP error status (4xx/5xx, or a
refused redirect) is returned as a reply with that status, never raised. Any
transport failure (`_NET_ERRORS`: `OSError` incl. timeouts/`URLError`,
`ValueError`, `http.client.HTTPException`) returns `None`.

`probe(base_url, timeout) -> JellyfinProbe` (a small dataclass, pure data, so
the mapping is unit-testable without HTTP):

1. `GET <base>/health`. Transport failure → `reachable=False`; stop.
2. `GET <base>/System/Info/Public`. Parse JSON; keep it only if it is an object.

The info request is skipped when the health request had no HTTP answer, so an
unreachable server costs one request.

### Time bound

urllib's `timeout` is a per-socket-operation timeout, not a total: a peer that
drips one byte at a time keeps a plain `read(65536)` alive far beyond it
(debate D1, reproduced: `timeout=1.0` returned after 12 s). `_fetch` therefore
reads the body with `read1()` in a loop against a single
`time.monotonic()` deadline of `timeout` seconds per request, started before
the request is opened, and treats an overrun as a transport failure. The
resulting bound per request is: connect ≤ `timeout` per resolved address
(name resolution itself is not bounded by it), response head ≤ `timeout`
per socket operation, body ≤ `timeout` plus at most one more socket operation.
A check makes at most two requests. What is **not** bounded to a single
`timeout`: a peer that drips the status line / headers a few bytes at a time
(that read happens inside `http.client`, one bounded socket operation at a
time, capped by its own header-size limits). That is a hostile-peer shape, not
a Jellyfin or reverse-proxy failure mode, and the target is operator-configured
on the LAN; the residual is accepted and listed under Risks.

Why two endpoints instead of one: `/health` alone cannot distinguish Jellyfin
from any other service that happens to answer 200 on `/health`; the public info
alone says nothing about health. Why not reuse `tcp_check` + `custom_cmd`: no
identity, no health semantics, no version in the snapshot.

### State decision table

`healthy` := health status 200 and stripped body == `Healthy`.
`identified` := info status 200, JSON object, `ProductName` contains `jellyfin`
(case-insensitive).

| # | health request | info request | state | detail |
|---|---|---|---|---|
| 1 | transport failure | (not sent) | `error` | `unreachable` |
| 2 | healthy | identified, wizard completed | `ok` | `healthy — <ServerName> <Version>` |
| 3 | healthy | identified, `StartupWizardCompleted` is `false` | `degraded` | `setup wizard not completed` |
| 4 | not healthy | identified | `degraded` | `unhealthy: <health status> <body, capped>` |
| 5 | any HTTP answer | HTTP answer that is neither identified nor 5xx | `error` | `not a Jellyfin server (info HTTP <status>)` |
| 6 | any HTTP answer | 5xx, or transport failure | `degraded` | `server info unavailable` (+ health summary) |

Row 4 takes precedence over row 3: an unhealthy server whose wizard is also
incomplete reports the `unhealthy` detail. Fields read from the info object are
type-checked, never trusted: a non-string `ProductName` is "not identified"
(row 5); non-string `Version`/`ServerName` become empty strings.

`StartupWizardCompleted` absent or non-boolean is treated as completed (only an
explicit `false` degrades), so an older server lacking the field is not
permanently degraded.

`metrics`: `reachable`, `healthy`, `health_status`, `version`, `server_name`,
`response_ms` (health request wall time, rounded). Strings taken from the
server are capped (80 chars) before entering `detail`/`metrics`.

### Events

The instance keeps `_prev_state` (`None` before the first check).
`dedupe_key=None` (see corrections).

| previous → current | event |
|---|---|
| `None`/`ok`/`degraded` → `error` | `alert` "`<name>` down" |
| `None`/`ok`/`error` → `degraded` | `warn` "`<name>` degraded" |
| `error`/`degraded` → `ok` | `done` "`<name>` healthy" |
| `None` → `ok`, or unchanged | none |

The message carries the target URL and the status detail.

### Plugin

`JellyfinPlugin`: `type_id="jellyfin"`, `display_name="Jellyfin"`,
`category="service"`, `config_version=1`, default `ui_schema`, registered in
`default_registry()` after `TcpCheckPlugin`.

## Files to change

| Path | Change | Reason |
|---|---|---|
| `taskpaw_v3/monitors/plugins/jellyfin.py` | new | the plugin |
| `taskpaw_v3/monitors/registry.py` | edit | register it |
| `taskpaw_v3/tests/test_jellyfin.py` | new | config, probe, state table, events |
| `taskpaw_v3/tests/test_catalog.py` | edit | catalog lists `jellyfin` |
| `taskpaw_v3/ui/src/i18n.ts` | edit | `services.jellyfin`, `monitorType.jellyfin` (en + zh-CN) |
| `taskpaw_v3/ui/src/schemaI18n.ts` | edit | zh label/help for `base_url` |
| `taskpaw_v3/ui/src/components/ServiceIcon.tsx` | edit | `jellyfin` glyph |
| `taskpaw_v3/ui/src/test/i18n.test.ts` | edit | type list includes `jellyfin`; `services.jellyfin` resolves |
| `taskpaw_v3/ui/src/test/schemai18n.test.tsx` | edit | `base_url` zh label; icon differs from fallback |
| `README.md`, `CHANGELOG.md` | edit | monitor table row; 3.9.11 entry |
| `taskpaw_v3/__init__.py`, `taskpaw_v3/ui/package.json`, `taskpaw_v3/ui/package-lock.json`, `taskpaw_v3/src-tauri/Cargo.toml`, `taskpaw_v3/src-tauri/Cargo.lock`, `taskpaw_v3/src-tauri/tauri.conf.json` | edit | version 3.9.11 |

## Execution surface

- Writes: only the files above.
- Read/execute only: `uv` (sync, pytest, ruff, mypy), `npm` in
  `taskpaw_v3/ui` (ci, test, lint, build), `curl`/the plugin itself against the
  local Jellyfin for the manual smoke (read-only GETs).
- Generated outputs: none committed. The two lockfile version lines are edited
  by hand to match the manifest bump (same single-line change previous version
  bumps made); `uv.lock` is untouched (no dependency change) and
  `uv lock --check` confirms it.

## Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| A startup/5xx page read as "wrong service" | medium | false alert on every restart | row 6: unidentifiable 5xx → `degraded` |
| Slow server makes a check take a few ×`timeout` | low | later poll | per-request body deadline (see "Time bound"); supervisor schedules from iteration start |
| Peer drips response headers byte by byte | very low | one check runs long | accepted: hostile-peer shape on an operator-configured LAN target; `http.client` header limits cap it |
| Jellyfin dead behind a reverse proxy that answers 502/503 on both paths | low | reads `degraded` (warn), not `error` (alert) | matches the issue's "HTTP answer but not healthy → degraded"; `detail` shows the status; the direct `127.0.0.1:8096` target is unaffected |
| Oversized/hostile response body | low | memory | 64 KiB read cap, capped strings |
| Redirecting reverse proxy (http→https) | low | reads as not-Jellyfin | no redirect following by design; `detail` carries the info status (`info HTTP 301`); operator sets the https URL |
| Flapping health → event noise | low | notifications | one event per transition only; supervisor's per-minute cap still applies |

## Test plan

`taskpaw_v3/tests/test_jellyfin.py`, using a `ThreadingHTTPServer` on
`127.0.0.1:0` whose per-path replies the test sets:

- Config: default URL; trailing slash stripped; path prefix kept; rejects
  `ftp://`, missing host, credentials, query, fragment, unknown key, leading /
  trailing / embedded whitespace and a newline; the schema title is "Base URL".
- State table rows 1–6, including: closed port (row 1), a server that accepts
  but never answers within `timeout` (row 1), `/health` 503 `Unhealthy` and
  200 `Degraded` (row 4), wizard `false` (row 3), a non-Jellyfin 200 JSON and a
  404 on the info path (row 5), info 503 non-JSON and info non-JSON 200
  (rows 6 and 5), a JSON array instead of an object (row 5), a redirect on
  `/health` not followed, unhealthy + wizard `false` reports the unhealthy
  detail (row 4 precedence), `ProductName` of `123` / `null` (row 5), non-string
  `Version` / `ServerName` (still `ok`, blank strings).
- Time bound: a `/health` body dripped one byte per interval is cut off at the
  deadline and reads as row 1; a dripped info body reads as row 6 (asserted with a generous upper bound on wall
  time, far below the undeadlined duration).
- Proxy: a default `ProxyHandler` reads the environment when the opener is
  **built**, so the test patches `http_proxy` to a closed port first and then
  builds a fresh opener through the module's own factory (`_build_opener()`);
  a healthy loopback server must still read `ok`. Patching the environment
  after import and using the module-level opener would pass with or without
  the fix (debate D8, reproduced).
- Path prefix: requests hit `<prefix>/health` and `<prefix>/System/Info/Public`.
- Events: down at startup alerts once; staying down is silent; recovery emits
  `done`; `ok → degraded` warns; `degraded → error` alerts; first-check `ok` is
  silent.
- Registry/catalog: `jellyfin` present with `base_url` in the schema.

UI: `i18n.test.ts` list + `services.jellyfin`; `schemai18n.test.tsx`
`fieldLabel("base_url", "jellyfin", "zh-CN")`; icon markup differs from the
fallback.

Manual smoke (acceptance 8): instantiate the plugin with the default config on
the target Mac and print one `check()` result.

## Handoff notes

- Keep `dedupe_key=None`; do not "fix" it to a stable key.
- Update the stale plugin list in the `default_registry()` docstring only as far
  as naming the new plugin; no other docstring churn.
- `NoRedirectHandler` raises `HTTPError` with the redirect status; `_fetch`
  must treat `HTTPError` as a reply (it is also an `OSError`, so catch it
  before `_NET_ERRORS`).
