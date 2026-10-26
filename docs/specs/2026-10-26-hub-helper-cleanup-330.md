# #330 — Hub upstream helper: a cleanup overrun must not wedge polling or stop delivery

Date: 2026-10-26 · Base: `cb9aca4` (main, 3.10.6) · Branch: `afk/hub-helper-cleanup-330`
Planner: Claude (driver). Implementer: Claude Opus 5.5. Gate: Codex astra xhigh.

## Spec review

Issue #330 reports two one-way failures in the Hub's child-process HTTP transport
(`taskpaw_v3/hub/server/upstream_worker.py`, `Transport`) and its OpenClaw sender
(`taskpaw_v3/hub/server/delivery_worker.py`, `DeliveryWorker`):

1. `Transport.request` ends with a `finally` block that **returns**. When
   `_cleanup` reports `False` (threads not joined within its 1 s grace, native
   handles pending, or an `OSError`), that `return` replaces whatever the `try`
   body returned, including a successful decoded reply, with
   `{"ok": False, "reason": "helper_cleanup_failed"}`. The record stays in
   `_owned`, and the admission check at the top of `request` then answers
   `helper_cleanup_failed` to **every** later request. Nothing on the poll path
   calls `retry_cleanup` (`poller.py` only does so in `stopped()`), so the Hub
   shows every agent offline until the process restarts.
2. `DeliveryWorker.send_payload` calls `request_stop()` whenever the transport is
   not clean after a request. `_run` then exits for good; nothing restarts it.
   OpenClaw notifications stop silently (only `notification_health.state ==
   "worker_stopped"` hints).

Today this is the documented behaviour (`docs/guides/upstream-isolation.md`
lines 45-50: an incomplete cleanup "forbids replacement helpers and prevents
successful service stop; later bounded cleanup can retry") and it is pinned by
`test_cancel_result_after_owned_cleanup[True-True]` in
`taskpaw_v3/tests/test_upstream_isolation.py`. The issue, confirmed by the
operator, changes that contract: a reply that arrived is a reply; cleanup is
retried on the next request; a dirty transport does not retire the delivery
worker.

Core need: polling and delivery self-heal without a Hub restart. Single-operator
LAN; no new mechanism.

## Acceptance criteria

- AC1 A request whose helper replied successfully returns that reply even when
  the post-reply cleanup reports failure; the retained record stays in `_owned`
  (the transport is not clean). A request that already failed in the body
  (cancel, deadline, invalid reply, helper exit code) and whose cleanup also
  failed keeps today's `helper_cleanup_failed` reason (design v2, critic C1).
- AC2 The next `request()` on a transport with a retained record first retries
  that record's cleanup; on success it proceeds normally (new helper, normal
  result). On failure it returns `helper_cleanup_failed` **without** starting a
  second helper. At most one helper is ever owned.
- AC3 A stop signalled while a reply is already in hand does not rewrite that
  reply: `request` returns what the helper produced. (Admission after stop is
  unchanged: a request issued after `cancel()` still returns `helper_cancelled`.)
- AC4 `DeliveryWorker.send_payload` no longer stops the worker when the transport
  is dirty after a request; the drain loop keeps running and the next send
  heals through AC2. `start()`, `stop()` and `_run`'s final `retry_cleanup` keep
  their current semantics. If a record can never be cleaned, every send returns
  `helper_cleanup_failed`, which the outbox already treats as a counted failure
  (`poller.py` `_drain_outbox`: attempts, 30·2^(n-1) s backoff capped at 1 h,
  dead-letter plus a local alert after 10 attempts). That replaces today's
  silent `worker_stopped` with a visible dead-letter after roughly 3 h; accepted
  (critic C4). The admission retry (≤1 s) runs before the send's deadline is
  computed, so one dirty send may overrun the drain's reserve by up to 1 s;
  accepted as minor.
- AC5 Existing tests that pinned the old behaviour are updated; new tests cover
  AC1, AC2 (both outcomes) and AC4. Full suite, lint, mypy green.
- AC6 `docs/guides/upstream-isolation.md` describes the new behaviour; CHANGELOG
  gets an entry under the current heading, no version bump (Hub-only change).

## Frozen issue contract

- Invariants kept: single owned helper at a time; cleanup budget stays 1 s per
  attempt; no replacement helper while a record is retained; `retry_cleanup`
  remains the single cleanup owner; `admit_result` / `clean()` / `cancel()` /
  `stop()` semantics unchanged; no new threads, state, timers or config.
- Allowed user-visible change: after a transient cleanup overrun the Hub keeps
  polling and delivering instead of reporting every agent offline until restart;
  `notification_health` no longer flips to `worker_stopped` for that cause.
- Non-goals: replacing the helper process model, changing `_cleanup`, touching
  `_WindowsProcess`, changing the poller's event-cursor logic (#332), any
  timeout tuning.
- Causal boundary: `Transport.request` (result handling and admission),
  `DeliveryWorker.send_payload` (the `request_stop` call), their tests, the guide
  paragraph, CHANGELOG.

## Assumptions

- A1 Python semantics: a `return` inside `finally` overrides a `return` from the
  `try` body. (Language fact; the critic may verify with a two-line snippet.)
- A2 `retry_cleanup` already handles the retained record correctly when called
  from a request path (it is today called from `stop()` paths only). Verified by
  reading: it takes the lock, sets `_creating`, calls `_cleanup(rec, True)`, and
  clears `_owned` on success.
- A3 No consumer depends on the `stopped → helper_cancelled` override after a
  successful reply: `poller.py` guards every use with `admit_result()` or
  `_stopped`; `delivery_worker.send_payload` only checks `result["ok"]`. Read, not
  executed.
- A4 Full-suite runtime on this machine is unknown; it runs in the background.

## Approach

Keep the structure of `request`; stop returning from `finally`.

```python
result: dict | None = None
try:
    ...                                   # as today, but assign `result = ...`
                                          # where the body currently `return`s
except Exception:
    result = {"ok": False, "reason": "helper_failed"}
finally:
    cleaned = rec is None
    if rec is not None:
        try:
            cleaned = self._cleanup(rec, rec.cancel.is_set() or rec.proc.poll() is None)
        except OSError:
            cleaned = False
    with self._lock:
        self._creating = False
        if cleaned:
            self._owned = None
if result is None:
    result = {"ok": False, "reason": "helper_failed"}
if not cleaned and not result.get("ok"):
    # A failed request whose record is also retained keeps the documented
    # reason; only a reply that actually arrived survives a cleanup overrun.
    result = {"ok": False, "reason": "helper_cleanup_failed"}
return result
```

This keeps `test_retained_cleanup_denies_replacement_and_can_retry`
(`test_upstream_isolation.py` ~1143, deadline + cleanup failure →
`helper_cleanup_failed`, second request denied while cleanup still fails) and
`test_windows_preread_backpressure[*-fail]` (~2040-2105, cancel before reply +
cleanup failure → `helper_cleanup_failed`, `_owned is rec`) green without edits.

Admission at the top of `request` becomes:

```python
with self._lock:
    if self._stopped.is_set():
        return {"ok": False, "reason": "helper_cancelled"}
    retained = self._owned is not None and not self._creating
if retained and not self.retry_cleanup():
    return {"ok": False, "reason": "helper_cleanup_failed"}
with self._lock:
    if self._stopped.is_set():
        return {"ok": False, "reason": "helper_cancelled"}
    if self._owned or self._creating:
        return {"ok": False, "reason": "helper_cleanup_failed"}
    self._creating = True
```

`retry_cleanup` already serialises on `_creating`, so two concurrent callers
cannot both clean or both create. The `stopped → helper_cancelled` override
after the `finally` is deleted (a stop that lands while the loop has not yet
observed completion still yields `helper_cancelled` through the in-loop
`_stopped` check, as today); the cleanup override is narrowed as shown above.

`DeliveryWorker.send_payload`: delete the two lines
`if not self._transport.clean(): self.request_stop()`. Nothing else changes.

Alternatives rejected: resetting `_owned` on failure (would allow a second helper
while the first may still hold handles, violating the single-owner invariant);
a background cleanup retry thread (new mechanism for a single-operator LAN).

## Files to change

| path | change | reason |
|---|---|---|
| `taskpaw_v3/hub/server/upstream_worker.py` | edit `Transport.request` | AC1-AC3 |
| `taskpaw_v3/hub/server/delivery_worker.py` | remove `request_stop` on dirty transport | AC4 |
| `taskpaw_v3/tests/test_upstream_isolation.py` | update `test_cancel_result_after_owned_cleanup`; add retry tests | AC1-AC3, AC5 |
| `taskpaw_v3/tests/test_hub_delivery_transport.py` | add drain-continues test | AC4, AC5 |
| `docs/guides/upstream-isolation.md` | reword the cleanup paragraph (lines 45-50) | AC6 |
| `CHANGELOG.md` | one entry under the current heading | AC6 |

## Execution surface

- Writes: the six files above, inside the worktree
  `D:/WORKSPACE/Taskpaw/.afk/worktrees/hub-helper-cleanup-330` only.
- Reads/executes: `uv run pytest taskpaw_v3/tests/test_upstream_isolation.py
  taskpaw_v3/tests/test_hub_delivery_transport.py` (targeted), then the full
  `uv run pytest`, `uv run ruff check . && uv run ruff format --check . && uv run mypy`.
  Always pass `--basetemp <LOCALAPPDATA>/taskpaw-pytest/afk-330`
  (create the parent first; delete it when the issue is done). Never touch
  `%APPDATA%\TaskPaw`.
- No generated outputs.

## Key implementation notes

- In the `try` body, every current `return {...}` becomes `result = {...}`
  followed by nothing else (the body already ends after each). Keep the order of
  checks (cancel → returncode → decode).
- `test_cancel_result_after_owned_cleanup` currently expects, for
  `(stop=True, failed=False)`, `helper_cancelled`, and for `(True, True)`,
  `helper_cleanup_failed`. Under the new contract all three parameter sets expect
  `{"ok": True}`; the `failed` case additionally asserts `transport._owned is
  records[0]` and `not transport.clean()`. The fixture's `finally` already
  restores `_cleanup` and asserts `retry_cleanup()`.
- New test (AC2 success): monkeypatch `_cleanup` to fail once, request → ok and
  dirty; restore `_cleanup`; second request → ok, `transport.clean()`, and the
  records list shows the retained record was cleaned before the second helper
  started (e.g. count `worker_argv` calls, or record `_cleanup` call order).
- New test (AC2 failure): `_cleanup` keeps failing → second request returns
  `helper_cleanup_failed` and `worker_argv` was called exactly once.
- New delivery test (AC4): a fake transport whose `request` returns
  `{"ok": True}` and whose `clean()` returns False for the first call; drive
  `send_payload` twice through a drain callback and assert the worker is neither
  cancelled nor stopped and the second send happened. Use the existing
  `endpoint` fixture style in `test_hub_delivery_transport.py` where simpler.
- Windows: the real helper path spawns `taskpaw-backend upstream-http`; tests in
  this area already run real child processes via `worker_argv` monkeypatching to
  `python -c`; reuse that pattern.

## Risk assessment

| risk | likelihood | impact | mitigation |
|---|---|---|---|
| A retained native record whose cleanup never succeeds keeps every request failing | low | same as today, but now with a retry per request | AC2 keeps the failure visible as `helper_cleanup_failed`; `stop()` unchanged |
| A reply applied after stop | low | poller already guards with `admit_result()` | AC3 test; A3 recorded |
| Full-suite regressions in isolation tests that assert `helper_cancelled` ordering | medium | test churn | run the two targeted files first, then the full suite |

## Out of scope

Everything listed under non-goals; the poller's paused event channel (#332);
`_cleanup` internals; the `notification_health` model.

## Test plan

- Targeted RED first: the updated parametrised test and the three new tests fail
  on `cb9aca4`.
- GREEN: targeted files pass; then full `uv run pytest`, ruff, ruff format, mypy.
- Manual smoke: none needed; behaviour is covered by the child-process tests.

## Debate record

- v1 → v2 (2026-10-26): critic C1 (supported, P1 for the plan: the unconditional
  removal of the cleanup override would fail two existing tests) fixed by
  narrowing the override to failed bodies; C4 (supported, P2) fixed by stating
  the permanently-dirty outcome in AC4; C7 (minor) line numbers corrected.
  C2, C3, C5, C6: verified, no finding.
- Round 2 (v2): C1 and C4 revalidated by name, resolved. C8 (minor, accepted):
  under a permanently dirty transport each request spends ≤1 s in
  `retry_cleanup` before `helper_cleanup_failed`, so a poll cycle grows by up to
  1 s per enabled server; stop latency ≤1 s more. C9 (nit): no `result is None`
  fallback is needed, every body path assigns `result`. Clean round; debate
  closed.

## Handoff notes

- Implementation note from C9: do not add a `result is None` fallback; every
  `try` path and the `except` assign `result`, so initialise it without a
  default-reason branch (e.g. `result: dict` assigned on each path, or
  `result = {"ok": False, "reason": "helper_failed"}` as the initial value and
  overwrite it).

Smallest diff: about 25 changed lines in `upstream_worker.py`, 2 deleted lines
in `delivery_worker.py`, tests, one doc paragraph, one CHANGELOG line. Do not
refactor `request`, do not touch `_WindowsProcess`, do not add logging beyond one
`log.warning` when a request heals a retained record (optional, only if a
logger already exists in the module; it does: `log`). Commit message prefix
`fix(hub):`, with `[skip ci]` and the trailer from the driver.
