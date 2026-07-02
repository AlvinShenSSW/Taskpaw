# Design: #146 Hygiene batch

Date: 2026-07-02 · Issue: #146 · Branch: `fix/146-hygiene-batch`

Four small, pre-reviewed hygiene items batched into one PR. No feature work; each
change is either a constitution-compliance fix, dead-file removal, test-cleanliness
fix, or a file move.

## Items & decisions

### 1. Bare `except:` → `except OSError:` (`taskpaw.py:2584`)
The local-IP display helper opens a UDP socket to `8.8.8.8:80` purely to learn the
outbound interface address, falling back to `127.0.0.1` on failure. The bare
`except:` violates constitution §4 ("No silent `except: pass`... catch the specific
exception"). The only failures the socket dance can raise are `OSError` subclasses
(`socket.error`, `socket.gaierror`, `socket.timeout` are all `OSError`). Narrowing
to `except OSError:` preserves behaviour exactly while no longer swallowing
programming errors (e.g. `NameError`, `KeyboardInterrupt`).

**Scope note:** V2 is frozen; this is one of the two V2 changes the operator
explicitly sanctioned for this batch (the other is deleting `requirements.txt`).
No other V2 lines touched.

### 2. Delete `requirements.txt`
It duplicates runtime deps already declared in `pyproject.toml` (`psutil`,
`pystray`, `Pillow`) and will drift. `pyproject.toml` + `uv.lock` are the single
source of truth (AGENTS.md §Commands). Removed rather than regenerated from the
lockfile — this repo installs via `uv sync`, not `pip install -r`, so the file has
no consumer. Verified no CI job, script, Dockerfile, or doc invokes
`requirements.txt` (only a historical CHANGELOG entry names it, left as-is).

### 3. Silence `PytestUnhandledThreadExceptionWarning`
`test_supervisor_watchdog_restarts_dead_worker` intentionally raises `SystemExit`
inside a worker thread to prove the watchdog restarts a dead worker. The uncaught
thread exception surfaced as a `PytestUnhandledThreadExceptionWarning`.

Fix: a small `_expect_thread_death()` context manager temporarily installs a
`threading.excepthook` that captures the exception, so the *intentional* death is
handled at its source rather than merely filtered. The test additionally asserts
that exactly one `SystemExit` was swallowed and nothing unexpected — so the cleanup
strengthens the test rather than weakening it.

**Decision — no global `filterwarnings = ["error"]`.** The issue says "consider" it.
After the fix the suite still emits an unrelated third-party
`StarletteDeprecationWarning` (FastAPI TestClient / httpx) and a default-hidden
sqlite `ResourceWarning` in `test_security.py`. Turning all warnings into errors
would fail the suite on those out-of-scope items and force V3 churn beyond this
batch. Deferred as follow-up once those are addressed; the named warning from this
issue is fully resolved.

### 4. Move audit files to `docs/audits/`
`BUG_AUDIT.md`, `CODE_AUDIT_REPORT.md`, `CODEX_AUDIT_FINDINGS.md` moved from repo
root into a new `docs/audits/` dir (v2.7 closed them out; root stays for
README/CHANGELOG/AGENTS). Used `git mv` to preserve history. The AGENTS.md
repo-layout row that names these files is updated to point at the new location (the
only doc reference that goes stale from the move). CHANGELOG.md entries that mention
the files by name are point-in-time historical records and are left untouched.

## Test plan
- `uv run pytest` green (434 passed); `PytestUnhandledThreadExceptionWarning` count
  drops to 0.
- `uv run python -m py_compile taskpaw.py` passes (V2 syntax gate).
- New assertion in the watchdog test guards against silently swallowing an
  unexpected exception type.

## Constitution gate
- §1 Scope: only the two operator-sanctioned V2 edits; all else is docs/tests/moves.
- §4 Reliability: bare-except removed, replaced with specific `OSError`.
- §5 Testing: behavioural test unchanged in intent, strengthened; suite green.
