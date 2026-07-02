# Design: #152 Hub time authority (host receipt clock)

Date: 2026-07-02 · Issue: #152 · Branch: `fix/152-hub-time-authority`

**Authority = the Hub *host machine's* system clock at receipt** — never a hardcoded
timezone, never an agent-supplied local time. Agents may sit in UTC+8/+9/+10; the
Hub judges freshness/online purely by when *it* received data.

## Audit (what the code already does right)
A full audit of `datetime` use in `taskpaw_v3/{hub,core,agent}` shows the design is
**already Hub-receipt-authoritative**, with two internally-consistent time bases:

- **Outbox + events → UTC (aware ISO-8601)** via `store._dt()` / `protocol`. These
  are absolute instants, compared machine-independently (drain retry, dead-letter
  age, `next_attempt_at`, `due_deliveries`). ✔
- **status_log + status.md → Hub *host* local time** (`datetime('now','localtime')`
  in SQLite; `datetime.now()` for display). This is the deliberate V2/OpenClaw
  contract — `idle-detector-v2.py` / `daily-report.py` parse `status.md`'s
  `Last updated:` / `last seen HH:MM:SS`. localtime **follows the host machine**, so
  it already satisfies "authority = whatever server the Hub runs on". ✔
- **`last_seen` = the Hub's own clock at the poll** (not the agent's). Agent tz has
  no bearing on Hub freshness. ✔ (No place compares an agent-supplied timestamp
  against Hub time.)

## The one genuine defect — `store.prune_dead_letters` (store.py:584)
```python
cutoff = _dt(datetime.now() - timedelta(days=days))   # naive LOCAL → ISO w/o offset
... "DELETE ... WHERE created_at < ?", (cutoff,)       # created_at is UTC-aware ISO
```
`created_at` is written by `_dt()` as **UTC-aware** (`...+00:00`); the cutoff is
built from **naive local** `datetime.now()`, so the lexical string comparison is
**off by the Hub's UTC offset** (e.g. ~9 h in JST) and mixes naive/aware ISO. Dead
letters get pruned up to `offset` hours early/late.

**Fix:** build the cutoff from UTC to match `created_at`:
```python
cutoff = _dt(datetime.now(timezone.utc) - timedelta(days=days))
```
One line; `timezone` is already imported. (`prune_status_logs` is unaffected — it
compares the *localtime* status_log column against SQLite `datetime('now','localtime')`,
both host-local, consistent.)

## Hardening (no behaviour change, guards the invariant)
- Clarify in comments that the two time bases are intentional (UTC for absolute
  comparisons; host-localtime only for the V2 status.md display contract).
- The seed-freshness check (`poller.py` `_seed_snapshot`) compares host-local
  `datetime.now()` against the host-local `last_seen` string — consistent; left as
  is, with a comment noting it is intentionally host-local.

## Test plan (TDD)
- **`prune_dead_letters` is UTC-correct under any host tz:** with the process TZ
  forced to `Asia/Tokyo` (UTC+9) and to `UTC`, a dead-letter row aged just over /
  just under `days` is deleted / kept correctly. (RED before the fix — the naive
  cutoff mis-prunes by the offset in a non-UTC tz.)
- **Invariant: agent tz is irrelevant to Hub freshness** — a poll records
  `last_seen` from the Hub's own clock; the agent's reported/local time does not
  move the Hub's online/last_seen judgement.
- **Guard: no hardcoded timezone/offset literal** in `taskpaw_v3` source (grep for
  `Asia/`, `+08:00`, `+09:00`, etc.) so a future change can't pin a tz.

## Constitution gate
- §1 Scope: V3 Hub only; V2 frozen/untouched.
- §3 Timezone: "one authority (Hub local time), convert on ingest, never compare
  timestamps from different machines lexically" — this fixes the one place that
  violated it (naive-vs-UTC lexical compare) and codifies the rest.
- §5 Testing: the behavioural fix ships with a tz-parametrized regression test.
