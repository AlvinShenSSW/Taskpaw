"""`Translator`: ja→zh subtitle translation through `llm-worker`s (#177),
resumable and with fallback providers (#192/#190).

One daemon thread per monitor run. The thread is only a CLIENT of terminable
`llm-worker` child processes (#178): it writes JSON request lines and waits on
the worker's own response queue with a deadline. It never writes a subtitle
and never touches plugin counters — it writes only its checkpoint files, and
every film yields exactly one `TranslateResult` on `results`, which the
monitor's worker thread settles. Requests are sequential.

The engine (#192 design v4, AC2–AC10/AC12):

- **Provider chain** (`chain_fn`, default `core.llm.get_llm_chain`): the usable
  providers, re-read at every routing decision, before every retry and every
  bisection node, and after every wait (each wait is sliced to ≤ 60 s). A
  provider is identified by its label (`model_label`: model + host) for
  persistence and display, and in memory by its fingerprint (base, model,
  sha256(key)[:16], thinking_off) — a changed fingerprint resets its state;
  republishing the same fingerprint preserves learned request settings. The
  worker of a fingerprint that left the chain is retired at once (H2).
- **Per-cue state**: `zh`, `by` (label), `refused_by` (labels, persisted),
  `failed_by` (a transient leaf, memory only — H5). A cue is exhausted when
  the chain is non-empty and its refusals or memory-only failures cover every
  label SEEN since the film was dequeued (H1); open otherwise. Blank cues are
  done at load (G10).
- **Checkpoint** (`checkpoint.CheckpointStore`, AC3): loaded at dequeue, saved
  atomically after every successful request and every recorded refusal; a
  write failure raises one notice per run, translation goes on in memory.
- **Routing** (AC7): an open cue goes to the first chain provider it has not
  refused that is closed (an open one is probed when its cool-down ended);
  with failover off a cue not refused by `chain[0]` waits for it (H7).
  Consecutive cues with the same route form batches of ≤ the provider's batch
  size (initially 40, halved after a timeout/length bisection succeeds, floor
  5 — H8). After 3 consecutive clean full batches it doubles, up to 40, but
  never regrows into a size that has shrunk twice (#201).
- **An attempt** (AC5/AC6): a transient failure (network, timeout, 5xx, 408,
  429, `finish_reason=length`, an empty reply, other bad responses) is retried
  after 10/30/90 s (429/503: `Retry-After`, ≤ 300 s); still failing, or a
  refusal / 401–404 / other 4xx, → the probe (the Settings Test's request;
  cached 60 s for these top-level decisions). Requests and probes first use
  cumulative fallback: on 400/422 omit the thinking-disable parameter, then
  on 400 omit json_mode too (#201). Probe failed → the provider opens
  (breaker 5/15/30 min; key, credit and content-policy failures start at 30)
  with one notice per
  provider per run. Probe OK, or invalid output → bisect on that provider: one
  request per node; a single cue failing transiently gets one retry after
  10 s and, still failing, a fresh probe at once (I1); after the leaves ONE
  fresh confirming probe decides whether failed leaves are recorded (G6/H8)
  or the provider opens instead. Transient failures and one-cue length
  cut-offs enter memory-only `failed_by`; other leaves enter persisted
  `refused_by`.
- **Defer, never block** (AC8): a film whose open cues have no available
  provider is deferred; the next film goes first; a deferred film resumes
  when a provider it needs closes again, returns `paused` after 2 h of
  ACCUMULATED deferral (H6) and `no_key` when the chain becomes empty (H1).

Cancel (D6/D24, C6): set the flag; under `_spawn_lock` detach EVERY worker and
put `CANCELLED` on each one's response queue (a waiting thread wakes at once);
then, outside the lock, close every worker's stdin, wait ONE shared ≤ 1 s for
them, tree-kill the survivors, join their readers and close the job keepers.
Every worker gets its own response queue (D4): a stale `Eof` from a killed
worker can never reach a later request.

Secrets: each provider's key travels only in ITS worker's environment
(`worker_env`, C7) — never argv, logs, notices, checkpoints, exception text or
`detail` strings. Requests log kind/status/latency, a bounded failure reason and label.

`progress()` (#189/#192 AC12) is a read-only view of the film `in_flight()`
stands for; None when idle and as soon as `cancel()` was called.

`llm_chain_from_config` builds the provider chain the agent publishes
(`core.llm.set_llm_chain`), and the provider probe (`probe_messages` /
`probe_ok`: the real prompt with one cue, OK only when the reply passes
`_validate`) is shared by the Settings Test and the translator.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import math
import queue
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Union,
)

from taskpaw_v3.core.llm import (
    EMPTY_REPLY_MESSAGE,
    LLM_SLOTS,
    LLMSettings,
    get_llm_chain,
    get_llm_failover,
    llm_settings_from_config,
)
from taskpaw_v3.core.llm_worker import (
    JobKeeper,
    assign_kill_on_close_job,
    worker_argv,
    worker_env,
)
from taskpaw_v3.core.tasklog import get_task_log
from taskpaw_v3.monitors.subs.checkpoint import CheckpointStore, SavedCue
from taskpaw_v3.monitors.subs.child import ChildProcess, Eof
from taskpaw_v3.monitors.subs.srt import Cue
from taskpaw_v3.monitors.subs.util import bounded

if TYPE_CHECKING:
    from taskpaw_v3.core.config import AgentConfig

log = logging.getLogger("taskpaw.subs.translate")

BATCH_SIZE = 40
MIN_BATCH_SIZE = 5  # H8: the floor of a provider's adaptive batch size
CONTEXT_SIZE = 5
RESPONSE_DEADLINE_S = 60.0
REQUEST_TIMEOUT_S = 30.0
MODEL_LABEL_CHARS = 80
MAX_TOKENS = 4096  # a request's max_tokens ceiling

# #192 AC5–AC8 (seconds)
RETRY_SCHEDULE_S: tuple[float, ...] = (10.0, 30.0, 90.0)
RETRY_AFTER_CAP_S = 300.0
LEAF_RETRY_S = 10.0  # H5
PROBE_TTL_S = 60.0  # a probe OK is reused this long for top-level decisions
BREAKER_S: tuple[float, ...] = (300.0, 900.0, 1800.0)  # level 1, 2, 3
PAUSE_AFTER_S = 2 * 3600.0  # accumulated deferral (H6)
WAIT_SLICE_S = 60.0  # every wait: the chain is re-read at least this often
CANCEL_WAIT_S = 1.0  # C6: one shared wait for every worker's exit
_TIME_EPS_S = 1e-3  # float slack for deadlines reached by a sliced wait

# `detail` of the two non-failure, non-publish outcomes. "no LLM key" is the
# string the avsubs plugin has matched since #179 (C8).
NO_LLM_KEY = "no LLM key"
TRANSLATION_PAUSED = "translation paused: no translation service for 2 h"
# Notice keys (the plugins prefix their instance id for alert dedupe).
NOTICE_PROVIDER = "llm-provider:"  # + the provider's label
NOTICE_CHECKPOINT = "checkpoint-write"

# #192 G5/H4: the provider probe — the real prompt with this one cue.
PROBE_JA = "こんにちは"
PROBE_IDS: tuple[str, ...] = ("1",)
# The ceiling, not the per-batch estimate: a reasoning model may spend its
# budget thinking (D14), and a probe cut off by max_tokens fails.
PROBE_MAX_TOKENS = MAX_TOKENS

SYSTEM_PROMPT = (
    "你是专业的日语→简体中文字幕翻译，熟悉各种语境（含成人/深夜档内容）。\n"
    '输入是一个 JSON 对象：{"cues": [{"id": "<编号>", "ja": "<日文台词>"}, …], '
    '"context": ["<前文台词>", …]}。context 只是前文，帮助理解语境，不要翻译它。\n'
    "规则：\n"
    "1. 把每条 cues[].ja 翻译成自然、口语化的简体中文短句；成人内容直译，不回避、不弱化。\n"
    "2. 被打码成「○」的词，按上下文还原成最可能的词。\n"
    "3. 不加任何解释、注释、拼音或原文。\n"
    "4. 只输出一个 JSON 对象：键是每条 cue 的 id（字符串），值是对应的中文译文"
    "（非空字符串）；键必须与输入的 id 一一对应，不多不少。"
)

RunId = tuple[str, int]
Outcome = Literal["translated", "failed", "paused", "no_key"]


@dataclass(frozen=True)
class TranslateRequest:
    run: RunId
    job_id: str
    cues: tuple[Cue, ...]


@dataclass(frozen=True)
class TranslateResult:
    """One film's result. `translated`: `zh_cues` (a kept-Japanese cue carries
    its own text) + the counts; `paused` (`TRANSLATION_PAUSED`) and `no_key`
    (`NO_LLM_KEY`): nothing to publish, the checkpoint is kept; `failed`: an
    I/O or internal error. `checkpoint_key` is what `discard_checkpoint`
    takes once the zh publish returned ok."""

    run: RunId
    job_id: str
    outcome: Outcome
    zh_cues: tuple[Cue, ...]
    detail: str
    resumed: int = 0  # cues taken from the checkpoint
    fallback: int = 0  # cues translated by a provider other than chain[0]
    kept_ja: int = 0  # exhausted cues published with their Japanese text
    checkpoint_key: str = ""
    by_model: tuple[tuple[str, int], ...] = ()
    duration_s: Optional[float] = None


@dataclass(frozen=True)
class Notice:
    """Something the plugin raises as an alert (`drain_notices`): a provider
    opened (once per provider per run) or the checkpoint could not be
    written (once per run). Labels only — never a key or a userinfo."""

    key: str
    title: str
    message: str


class _Sentinel:
    def __repr__(self) -> str:
        return "CANCELLED"


# Put on `results` by cancel() so a draining worker thread can observe it; also
# the wake-up sentinel on each worker's response queue (D6).
CANCELLED: Any = _Sentinel()

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_TIMEOUT_MESSAGES = frozenset({"timeout", "response timeout"})
_LENGTH_MESSAGE = "finish_reason=length"

# (child, its own response queue, job keeper)
_Worker = tuple[ChildProcess, queue.Queue[object], Optional[JobKeeper]]
_Fingerprint = tuple[str, str, str, bool]


class _Cancelled(Exception):
    pass


class _WorkerError(Exception):
    """The worker could not be spawned (network kind for the request). The
    message is a fixed `spawn: <TypeName>` — never exception text."""


class _DuplicateKey(ValueError):
    pass


@dataclass(frozen=True)
class _Fail:
    kind: str
    message: str
    status: Optional[int] = None  # C2: the HTTP status, when there was one
    retry_after: Optional[int] = None  # C1: Retry-After delta-seconds


def _transient(f: _Fail) -> bool:
    """AC5 (top-level requests): retried on the schedule — network, timeout,
    5xx, 408, 429, 503, `finish_reason=length`, an empty reply and every other
    bad response (no status, 3xx). Not: content, refusals, auth, other 4xx."""
    if f.kind in ("network", "rate_limit"):
        return True
    if f.kind == "refusal":
        return f.message == EMPTY_REPLY_MESSAGE
    if f.kind == "bad_response":
        s = f.status
        return s is None or s in (408, 429) or not 400 <= s < 500
    return False


def _leaf_transient(f: _Fail) -> bool:
    """H5: a single cue's failure worth one more try — network / timeout /
    5xx / 429 (408 counts as a timeout). Empty replies and length cut-offs
    get no extra leaf retry; #201 records cut-offs as memory-only failures."""
    if f.kind in ("network", "rate_limit"):
        return True
    s = f.status
    return f.kind == "bad_response" and s is not None and (s >= 500 or s in (408, 429))


def _shrinks_batch(f: _Fail) -> bool:
    """H8: a bisection triggered by this failure halves the batch size when
    its halves succeed — a timeout (incl. HTTP 408) or `finish_reason=length`."""
    timed_out = f.message in _TIMEOUT_MESSAGES or f.status == 408
    return timed_out or f.message == _LENGTH_MESSAGE


def _empty(f: _Fail) -> bool:
    """An empty reply: unusable output (level 1, AC7), not a content policy."""
    return f.kind == "refusal" and f.message == EMPTY_REPLY_MESSAGE


def _severe(f: _Fail) -> bool:
    """AC7: key, credit and content-policy failures open for 30 min at once."""
    policy = f.kind == "refusal" and not _empty(f)
    return f.kind == "auth" or policy or f.status in (401, 402, 403)


def _reason(f: _Fail) -> str:
    """H4: the notice text follows the failed probe."""
    if f.kind == "auth" or f.status in (401, 402, 403):
        return "key or credit problem"
    if f.status == 404:
        return "model or URL not found"
    if f.kind == "rate_limit" or f.status == 429:
        return "rate limit or quota reached"
    if f.status is not None and 400 <= f.status < 500:
        return f"rejected the request (HTTP {f.status})"
    if f.kind == "refusal" and not _empty(f):
        return "content policy rejected the translation prompt"
    if f.kind in ("invalid", "content") or _empty(f):
        return "returns unusable output"
    return "unreachable"


def _no_dupes(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise _DuplicateKey("duplicate key")
        out[k] = v
    return out


def _api_host(api_base: object) -> Optional[str]:
    """`urlsplit(api_base).hostname` (lower-case; never the userinfo, port or
    path), or None — no host, not a string, or unparseable. Never raises."""
    if not isinstance(api_base, str):
        return None
    try:
        return urllib.parse.urlsplit(api_base).hostname
    except Exception:  # N7: a malformed base is "no host", never an error
        return None


def needs_llm_key(api_base: str) -> bool:
    """Whether a request to `api_base` needs an API key: a keyless request is
    only allowed against a loopback base (a local OpenAI-compatible server).
    An unparseable base needs a key. Shared with the plugins (one rule)."""
    host = _api_host(api_base)
    if host is None:
        return True
    return not (host in _LOOPBACK_HOSTS or host.startswith("127."))


def model_label(model: object, api_base: object) -> str:
    """`<model> · <api host>` for the progress view (#189, D14/N7). The host
    is `urlsplit(api_base).hostname` — never the userinfo (a key can sit
    there), the port or the path; the key itself is never an input. Computed
    defensively: no parseable host, or any error → the model name alone.
    At most `MODEL_LABEL_CHARS` (`bounded` keeps the host at the end)."""
    name = model.strip() if isinstance(model, str) else ""
    host = _api_host(api_base)
    return bounded(" · ".join(p for p in (name, host) if p), MODEL_LABEL_CHARS)


def _usable(s: LLMSettings) -> bool:
    """G11: base and model set, and a key unless the base is loopback."""
    return bool(s.api_base and s.model) and bool(
        s.api_key or not needs_llm_key(s.api_base)
    )


def llm_chain_from_config(
    config: "AgentConfig", *, environ: Optional[Mapping[str, str]] = None
) -> tuple[LLMSettings, ...]:
    """#192 AC1: the provider chain — the USABLE providers among primary,
    fallback 1 and fallback 2, in that order, each key resolved env-first for
    its own slot. Usable (G11) = base and model non-empty and a key unless the
    base is loopback (`needs_llm_key`). A provider whose `model_label` repeats
    an earlier one's is dropped — first wins — with one warning naming the
    slot and the label only (G9): the label is a provider's identity.

    Lives here, beside `needs_llm_key` and `model_label` (core cannot import
    the subs engine); the launcher (boot) and `MonitorAdmin` (after a save)
    publish it with `core.llm.set_llm_chain`."""
    chain: list[LLMSettings] = []
    labels: set[str] = set()
    for slot in LLM_SLOTS:
        s = llm_settings_from_config(config, slot=slot, environ=environ)
        if not _usable(s):
            continue
        label = model_label(s.model, s.api_base)
        if label in labels:
            log.warning(
                "LLM %s ignored: the same model as an earlier provider (%s)",
                slot,
                label,
            )
            continue
        labels.add(label)
        chain.append(s)
    return tuple(chain)


def _messages(user: dict[str, Any]) -> list[dict[str, str]]:
    """A translation request's messages: the system prompt + the JSON user turn
    (`{"cues": [...], "context": [...]}`) — every batch and the probe."""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def probe_messages() -> list[dict[str, str]]:
    """#192 G5/H4: the provider probe — the REAL translation prompt with one
    cue (`PROBE_JA`, id "1", no context). Shared by the Settings Test and the
    translator so both ask exactly the same question."""
    return _messages(
        {"cues": [{"id": i, "ja": PROBE_JA} for i in PROBE_IDS], "context": []}
    )


def _validate(content: str, ids: list[str]) -> Union[dict[str, str], _Fail]:
    """Content-layer validation (spec review §4.2)."""
    try:
        obj = json.loads(content, object_pairs_hook=_no_dupes)
    except _DuplicateKey:
        return _Fail("content", "duplicate key")
    except (ValueError, RecursionError):
        return _Fail("content", "invalid JSON")
    if not isinstance(obj, dict):
        return _Fail("content", "not an object")
    if set(obj) != set(ids):
        return _Fail("content", "id set mismatch")
    out: dict[str, str] = {}
    for k in ids:
        v = obj[k]
        if not isinstance(v, str):
            return _Fail("content", "empty or non-string value")
        # F1: blank/whitespace-only lines are dropped — a blank line inside a
        # cue would end the cue early and corrupt the published .srt.
        text = "\n".join(ln.strip() for ln in v.splitlines() if ln.strip())
        if not text:
            return _Fail("content", "empty or non-string value")
        out[k] = text
    return out


def probe_ok(content: str) -> bool:
    """H4: a probe reply is OK only when it passes the translator's own
    `_validate` for `PROBE_IDS` — an envelope-OK but unusable reply fails."""
    return not isinstance(_validate(content, list(PROBE_IDS)), _Fail)


def _fingerprint(s: LLMSettings) -> _Fingerprint:
    """H2: a provider's in-memory identity — never the raw key."""
    digest = hashlib.sha256(s.api_key.encode("utf-8")).hexdigest()[:16]
    return (s.api_base, s.model, digest, s.thinking_off)


@dataclass(eq=False)
class _Provider:
    """One fingerprint's state for this run (translator thread only)."""

    settings: LLMSettings
    fingerprint: _Fingerprint
    label: str
    json_mode: bool = True  # off for the run after a 400 → no-json success
    batch_size: int = BATCH_SIZE  # H8
    clean_successes: int = 0
    shrinks: dict[int, int] = field(default_factory=dict)
    thinking_rejections: int = 0
    open_until: Optional[float] = None  # breaker: None = closed
    level: int = 0
    probe_ok_at: Optional[float] = None
    alerted: bool = False
    last_fail_kind: Optional[str] = None
    retired: bool = False  # left the chain (H2): never asked again


@dataclass(eq=False)
class _CueState:
    zh: Optional[str] = None
    by: Optional[str] = None
    refused_by: set[str] = field(default_factory=set)  # persisted
    failed_by: set[str] = field(default_factory=set)  # H5: memory only
    blank: bool = False


@dataclass(eq=False)
class _Film:
    """A dequeued film. The counters, `seen`, `model` and the deferral fields
    are guarded by `_count_lock`; `states` belongs to the translator thread."""

    req: TranslateRequest
    key: str
    states: list[_CueState]
    seen: set[str]
    started_at: float
    model: str
    n_done: int = 0  # has zh (translated, resumed or blank)
    n_kept: int = 0  # exhausted
    n_resumed: int = 0
    n_fallback: int = 0
    n_new: int = 0  # translated since dequeue (the ETA's rate)
    batches_done: int = 0
    batches_total: int = 0
    deferred_total: float = 0.0
    deferred_since: Optional[float] = None
    log_started: bool = False
    last_provider: Optional[_Provider] = None
    left_open: set[str] = field(default_factory=set)
    finished: bool = False  # its result is (about to be) on `results`

    def deferred_for(self, now: float) -> float:
        since = self.deferred_since
        return self.deferred_total + (now - since if since is not None else 0.0)

    def deadline(self) -> float:
        """While deferred: when its accumulated deferral reaches 2 h."""
        since = self.deferred_since if self.deferred_since is not None else 0.0
        return since + PAUSE_AFTER_S - self.deferred_total


def _exhausted(film: _Film, st: _CueState, chain: Sequence[_Provider]) -> bool:
    """AC2/H1: refused by every label seen since dequeue — never with an
    empty chain (then the film is `no_key`)."""
    return bool(chain) and film.seen <= (st.refused_by | st.failed_by)


class Translator:
    def __init__(
        self,
        run: RunId,
        *,
        name: str,
        task_type: str = "avsubs",
        spawn: Callable[..., ChildProcess] = ChildProcess,
        chain_fn: Callable[[], Sequence[LLMSettings]] = get_llm_chain,
        failover_fn: Callable[[], bool] = get_llm_failover,
        checkpoint_dir: Optional[Path] = None,
        clock: Callable[[], float] = time.monotonic,
        wait_fn: Optional[Callable[[float], bool]] = None,
        worker_argv_fn: Callable[[], list[str]] = worker_argv,
        job_fn: Callable[[subprocess.Popen], Optional[JobKeeper]] = (
            assign_kill_on_close_job
        ),
        deadline_s: float = RESPONSE_DEADLINE_S,
    ) -> None:
        """`checkpoint_dir`: the `subs-checkpoints` folder, or None (memory
        only). `clock` is monotonic; `wait_fn(seconds)` waits and returns
        True when cancelled — the default wakes on cancel() and submit()."""
        self._run = run
        self._name = name
        self._task_type = task_type
        self._spawn = spawn
        self._chain_fn = chain_fn
        self._failover_fn = failover_fn
        self._store = CheckpointStore(checkpoint_dir)
        self._clock = clock
        self._wait_fn = wait_fn if wait_fn is not None else self._wait_wake
        self._worker_argv_fn = worker_argv_fn
        self._job_fn = job_fn
        self._deadline_s = deadline_s
        self.results: "queue.Queue[TranslateResult | object]" = queue.Queue()
        self._requests: "queue.Queue[Optional[TranslateRequest]]" = queue.Queue()
        self._cancel = threading.Event()
        self._wake = threading.Event()  # set by submit() and cancel()
        self._cancel_once = threading.Lock()
        self._cancel_started = False
        self._spawn_lock = threading.Lock()
        self._workers: dict[_Fingerprint, _Worker] = {}
        self._count_lock = threading.Lock()
        self._queued = 0
        self._working: Optional[_Film] = None
        self._deferred: list[_Film] = []
        self._notices: list[Notice] = []
        # translator thread only
        self._providers: dict[_Fingerprint, _Provider] = {}
        self._chain: list[_Provider] = []
        self._rids = itertools.count()
        self._checkpoint_alerted = False
        self._thread: Optional[threading.Thread] = None

    # ── public API ───────────────────────────────────────────────────────
    def _record(
        self, event_kind: str, film: Optional[_Film] = None, **data: Any
    ) -> None:
        get_task_log().record(
            self._name,
            event_kind,
            task_type=self._task_type,
            film=film.req.job_id if film is not None else None,
            severity="warn"
            if event_kind in {"translate.provider_down", "translate.paused"}
            else "info",
            data=data,
        )

    def _log_start(self, film: _Film, model: Optional[str]) -> None:
        if not film.log_started:
            film.log_started = True
            self._record(
                "translate.started",
                film,
                model=model,
                lines=len(film.states),
                resumed=film.n_resumed,
            )

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name=f"subs-translate-{self._name}", daemon=True
        )
        self._thread.start()

    def submit(self, req: TranslateRequest) -> None:
        with self._count_lock:
            if self._cancel.is_set():
                log.debug("subs-translate %s: submit after cancel ignored", self._name)
                return
            self._queued += 1
        self._requests.put(req)
        self._wake.set()

    def queued(self) -> int:
        """H3: every unsettled film but the one `in_flight()` stands for —
        the queued requests and the deferred films; 0 after cancel (D7)."""
        if self._cancel.is_set():
            return 0
        with self._count_lock:
            n = self._queued + len(self._deferred)
            if self._working is None and self._deferred:
                n -= 1
            return n

    def in_flight(self) -> bool:
        """H3: a film is worked or deferred and its result is not yet on
        `results` — exactly one film; False after cancel (D7)."""
        if self._cancel.is_set():
            return False
        with self._count_lock:
            return self._working is not None or bool(self._deferred)

    def progress(self, now: Optional[float] = None) -> Optional[dict[str, Any]]:
        """#189/#192 AC12: a fresh dict of the film `in_flight()` stands for
        (the worked one, else the deferred one with the earliest deadline):
        `job_id`, `model` (the label of the provider in use), `batches_done`/
        `batches_total` (a top-level batch counts once it concluded),
        `cues_done` (translated, resumed, blank or kept) / `cues_total`,
        `started_at` (monotonic, at dequeue), `elapsed_s`, `percent`, `eta_s`
        (≥ 1 cue translated and ≥ 10 s passed; None while paused),
        `cues_resumed`, `cues_fallback`, `cues_kept_ja`, `paused` (the film is
        deferred) and `deferred` (how many films are). None when idle and
        whenever cancel is set (D5). `now` defaults to the clock."""
        with self._count_lock:
            if self._cancel.is_set():
                return None
            film = self._working
            paused = False
            if film is None or film.finished:
                waiting = [f for f in self._deferred if not f.finished]
                film = min(waiting, key=_Film.deadline) if waiting else None
                paused = film is not None
            if film is None or film.finished:
                return None
            total = len(film.states)
            done, kept = film.n_done, film.n_kept
            new, started = film.n_new, film.started_at
            snap = {
                "job_id": film.req.job_id,
                "model": film.model,
                "batches_done": film.batches_done,
                "batches_total": max(film.batches_total, film.batches_done),
                "cues_resumed": film.n_resumed,
                "cues_fallback": film.n_fallback,
                "cues_kept_ja": kept,
            }
            deferred = len(self._deferred)
            t = self._clock() if now is None else now
            waited = film.deferred_for(t)
        elapsed = max(0.0, t - started)
        settled = done + kept
        eta: Optional[int] = None
        if not paused and new >= 1 and elapsed >= 10:
            active = max(0.0, elapsed - waited)
            eta = math.ceil(active / new * max(0, total - settled))
        return {
            **snap,
            "cues_done": settled,
            "cues_total": total,
            "started_at": started,
            "elapsed_s": int(elapsed),
            "percent": 100 * settled // total if total else 0,
            "eta_s": eta,
            "paused": paused,
            "deferred": deferred,
        }

    def drain_notices(self) -> list[Notice]:
        """The notices raised since the last call (then forgotten)."""
        with self._count_lock:
            out, self._notices = self._notices, []
        return out

    def discard_checkpoint(self, key: str) -> None:
        """Delete a film's checkpoint — call it only after its zh publish
        returned ok. Never raises."""
        self._store.delete(key)

    def cancel(self) -> None:
        """Idempotent and bounded (D24, C6); never respawns. Every worker's
        stdin is closed, then ONE shared ≤ 1 s wait, then the survivors are
        tree-killed — about 1 s for three live workers."""
        with self._cancel_once:
            if self._cancel_started:
                return
            self._cancel_started = True
        self._cancel.set()
        self._wake.set()
        with self._spawn_lock:
            workers = list(self._workers.values())
            self._workers.clear()
            for w in workers:
                w[1].put(CANCELLED)  # wake a thread waiting on THIS worker (D6)
        self._teardown(workers, wait_s=CANCEL_WAIT_S)
        # A worker that _ensure_worker spawned at this moment is torn down by
        # its own path, which then sees the flag and never spawns (D24).
        self._requests.put(None)
        self.results.put(CANCELLED)

    def join(self, timeout: float) -> None:
        t = self._thread
        if t is not None:
            t.join(max(0.0, timeout))

    def is_alive(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    # ── thread ───────────────────────────────────────────────────────────
    def _loop(self) -> None:
        try:
            self._store.prune()
            while self._step():
                pass
        except _Cancelled:
            pass
        finally:
            with self._count_lock:
                self._working = None
                self._deferred = []
            self._shutdown_workers()

    def _step(self) -> bool:
        """One film's turn (or one idle decision). False: the thread ends."""
        try:
            film = self._next_film()
        except _Cancelled:
            raise
        except Exception as e:  # a bug must not kill the thread silently
            self._internal_error(e, None)
            return True
        if film is None:
            return False
        try:
            outcome = self._work(film)
        except _Cancelled:
            raise
        except Exception as e:
            self._internal_error(e, film)
            return True
        if outcome == "defer":
            self._defer(film)
        else:
            self._finish(film, self._result(film, outcome))
        return True

    def _internal_error(self, e: Exception, film: Optional[_Film]) -> None:
        log.error(
            "subs-translate %s: internal error (%s)", self._name, type(e).__name__
        )
        detail = f"internal error: {type(e).__name__}"
        if film is not None:
            self._finish(film, self._failed(film.req, detail))
            return
        with self._count_lock:
            deferred = list(self._deferred)
        for f in deferred:
            self._finish(f, self._failed(f.req, detail))
        self._sleep(1.0)  # never a hot loop on a persistent bug

    def _shutdown_workers(self) -> None:
        """Thread exit: no worker may outlive the thread."""
        with self._spawn_lock:
            workers = list(self._workers.values())
            self._workers.clear()
        self._teardown(workers, wait_s=CANCEL_WAIT_S)

    @staticmethod
    def _failed(req: TranslateRequest, detail: str) -> TranslateResult:
        return TranslateResult(req.run, req.job_id, "failed", (), detail)

    def _result(self, film: _Film, outcome: str) -> TranslateResult:
        req = film.req
        if outcome == "translated":
            zh_cues = tuple(
                Cue(
                    c.index,
                    c.start_ms,
                    c.end_ms,
                    st.zh if st.zh is not None else c.text,
                )
                for c, st in zip(req.cues, film.states)
            )
            kept = sum(1 for st in film.states if st.zh is None)
            log.info(
                "subs-translate %s: film translated (%d lines: %d resumed, "
                "%d by a fallback model, %d kept in Japanese)",
                self._name,
                len(zh_cues),
                film.n_resumed,
                film.n_fallback,
                kept,
            )
            return TranslateResult(
                req.run,
                req.job_id,
                "translated",
                zh_cues,
                "",
                film.n_resumed,
                film.n_fallback,
                kept,
                film.key,
            )
        kind: Outcome = "paused" if outcome == "paused" else "no_key"
        if kind == "paused":
            log.warning(
                "subs-translate %s: film paused — no translation service for 2 h "
                "(its checkpoint is kept)",
                self._name,
            )
        return TranslateResult(
            req.run,
            req.job_id,
            kind,
            (),
            TRANSLATION_PAUSED if kind == "paused" else NO_LLM_KEY,
            film.n_resumed,
            film.n_fallback,
            0,
            film.key,
        )

    def _finish(self, film: _Film, result: TranslateResult) -> None:
        """Publish a film's result, THEN forget it: `queued()`/`in_flight()`
        never report nothing while its result is not yet on `results`."""
        if self._cancel.is_set():
            raise _Cancelled
        self._log_start(film, None)
        by_model: dict[str, int] = {}
        if result.outcome == "translated":
            for st in film.states:
                if st.by is not None and st.zh is not None and not st.blank:
                    label = bounded(st.by, MODEL_LABEL_CHARS)
                    by_model[label] = by_model.get(label, 0) + 1
        result = replace(
            result,
            by_model=tuple(by_model.items()),
            duration_s=max(0.0, self._clock() - film.started_at),
        )
        if result.outcome == "translated":
            self._record(
                "translate.finished",
                film,
                lines=len(result.zh_cues),
                by_model=by_model,
                kept_ja=result.kept_ja,
                duration=result.duration_s,
            )
        elif result.outcome == "paused":
            self._record("translate.paused", film, minutes=120)
        with self._count_lock:
            film.finished = True  # progress() stops reporting it now
        self.results.put(result)
        with self._count_lock:
            if self._working is film:
                self._working = None
            if film in self._deferred:
                self._deferred.remove(film)

    def _defer(self, film: _Film) -> None:
        self._log_start(film, None)
        with self._count_lock:
            film.deferred_since = self._clock()
            self._deferred.append(film)
            if self._working is film:
                self._working = None
            left = len(film.states) - film.n_done - film.n_kept
        self._record("translate.deferred", film, lines=left)
        log.info(
            "subs-translate %s: film deferred — no translation service for its "
            "%d open line(s)",
            self._name,
            left,
        )

    def _resume(self, film: _Film) -> None:
        with self._count_lock:
            film.deferred_total = film.deferred_for(self._clock())
            film.deferred_since = None
            self._deferred.remove(film)
            self._working = film
        self._record("translate.resumed", film)

    def _next_film(self) -> Optional[_Film]:
        """The next film to work: a deferred one that a provider can take
        again, else the next queued request, else a wait (≤ 60 s, until a
        cool-down end, a deferral deadline or a new request). None: cancel."""
        while True:
            if self._cancel.is_set():
                raise _Cancelled
            chain = self._refresh_chain()
            with self._count_lock:
                deferred = list(self._deferred)
            if deferred and not chain:  # H1: the empty chain → no_key, all
                for f in deferred:
                    self._finish(f, self._result(f, "no_key"))
                deferred = []
            now = self._clock()
            for f in list(deferred):
                if f.deferred_for(now) >= PAUSE_AFTER_S - _TIME_EPS_S:
                    self._finish(f, self._result(f, "paused"))
                    deferred.remove(f)
            failover = self._failover_fn()
            for f in deferred:
                if self._routable(f, chain, failover):
                    self._resume(f)
                    return f
            try:
                req = self._requests.get_nowait()
            except queue.Empty:
                if deferred:
                    self._idle(self._idle_delay(chain, deferred))
                    continue
                req = self._requests.get()
            if req is None:
                return None
            return self._dequeue(req)

    def _idle_delay(self, chain: Sequence[_Provider], deferred: list[_Film]) -> float:
        now = self._clock()
        ends = [
            p.open_until - now
            for p in chain
            if p.open_until is not None and p.open_until > now
        ]
        deadlines = [f.deadline() - now for f in deferred]
        return max(_TIME_EPS_S, min([WAIT_SLICE_S, *ends, *deadlines]))

    def _idle(self, seconds: float) -> None:
        """Only deferred films are left: wait, but return early for a new
        request (the default wait wakes on submit)."""
        self._wake.clear()
        if self._cancel.is_set():
            raise _Cancelled
        if not self._requests.empty():
            return
        if self._wait_fn(seconds):
            raise _Cancelled

    def _wait_wake(self, seconds: float) -> bool:
        self._wake.wait(seconds)
        return self._cancel.is_set()

    def _sleep(self, seconds: float, p: Optional[_Provider] = None) -> bool:
        """Wait `seconds` in slices of ≤ 60 s, re-reading the chain after
        each (H2). False when `p` was retired meanwhile. Raises on cancel."""
        end = self._clock() + max(0.0, seconds)
        while True:
            if p is not None and self._retired(p):
                return False
            left = end - self._clock()
            if left <= 0:
                return True
            self._wake.clear()
            if self._cancel.is_set():
                raise _Cancelled
            if self._wait_fn(min(left, WAIT_SLICE_S)):
                raise _Cancelled

    def _dequeue(self, req: TranslateRequest) -> _Film:
        """C9: the checkpoint is read when the film is dequeued."""
        cues = req.cues
        key = self._store.key(cues)
        saved = self._store.load(key, len(cues))
        states: list[_CueState] = []
        resumed = 0
        for i, c in enumerate(cues):
            st = _CueState()
            if not c.text.strip():
                st.zh, st.blank = c.text, True  # G10: done with its own text
            elif saved is not None:
                s = saved[i]
                st.refused_by = set(s.refused_by)
                if s.zh is not None:
                    st.zh, st.by = s.zh, s.by
                    resumed += 1
            states.append(st)
        chain = self._refresh_chain()
        film = _Film(
            req=req,
            key=key,
            states=states,
            seen={p.label for p in chain},
            started_at=self._clock(),
            model=chain[0].label if chain else "",
            n_done=sum(1 for st in states if st.zh is not None),
            n_resumed=resumed,
        )
        self._recount(film, chain)
        with self._count_lock:
            self._queued -= 1
            self._working = film
        if resumed:
            log.info(
                "subs-translate %s: %d of %d lines resumed from the checkpoint",
                self._name,
                resumed,
                len(cues),
            )
        return film

    def _recount(self, film: _Film, chain: Sequence[_Provider]) -> None:
        """The kept count and the batch estimate (after each routing pass
        and each concluded batch)."""
        kept = sum(
            1 for st in film.states if st.zh is None and _exhausted(film, st, chain)
        )
        size = chain[0].batch_size if chain else BATCH_SIZE
        with self._count_lock:
            film.n_kept = kept
            left = len(film.states) - film.n_done - kept
            film.batches_total = film.batches_done + math.ceil(left / size)

    # ── chain + routing ──────────────────────────────────────────────────
    def _refresh_chain(self) -> list[_Provider]:
        """AC1/H2: re-read the chain; a new fingerprint gets a fresh state,
        a departed one is retired (its worker torn down). Every label is
        added to `seen` of the worked and the deferred films (H1)."""
        chain: list[_Provider] = []
        labels: set[str] = set()
        for s in self._chain_fn():
            if not _usable(s):
                continue
            label = model_label(s.model, s.api_base)
            if label in labels:
                continue  # G9: first wins (the publisher already warned)
            labels.add(label)
            fp = _fingerprint(s)
            p = self._providers.get(fp)
            if p is None:
                p = _Provider(settings=s, fingerprint=fp, label=label)
                self._providers[fp] = p
            chain.append(p)
        current = {p.fingerprint for p in chain}
        for fp in [fp for fp in self._providers if fp not in current]:
            gone = self._providers.pop(fp)
            gone.retired = True
            self._retire_worker(gone)
        self._chain = chain
        with self._count_lock:
            films = list(self._deferred)
            if self._working is not None:
                films.append(self._working)
            for f in films:
                f.seen |= labels
        return chain

    def _retired(self, p: _Provider) -> bool:
        self._refresh_chain()
        return p.retired

    def _routable(
        self, film: _Film, chain: Sequence[_Provider], failover: bool
    ) -> bool:
        """A deferred film can go on: no open cue is left, or one has a route."""
        open_cues = self._open_cues(film, chain)
        if not open_cues:
            return True
        return any(self._route(film, i, chain, failover) is not None for i in open_cues)

    @staticmethod
    def _open_cues(film: _Film, chain: Sequence[_Provider]) -> list[int]:
        return [
            i
            for i, st in enumerate(film.states)
            if st.zh is None and not _exhausted(film, st, chain)
        ]

    def _route(
        self, film: _Film, i: int, chain: Sequence[_Provider], failover: bool
    ) -> Optional[_Provider]:
        """AC7: the first provider the cue has not refused that is closed —
        an open one whose cool-down ended is probed first. Failover off: a
        cue not refused by `chain[0]` waits for it (H7)."""
        st = film.states[i]
        for k, p in enumerate(chain):
            if p.label in st.refused_by or p.label in st.failed_by:
                continue
            self._half_open(p)
            if p.open_until is None:
                return p
            if not failover and k == 0:
                return None
        return None

    def _half_open(self, p: _Provider) -> None:
        if p.open_until is None or self._clock() < p.open_until:
            return
        fail = self._probe(p, cached=False)
        if fail is None:
            p.open_until, p.level = None, 0
            self._record("translate.provider_up", model=p.label)
            log.info("subs-translate %s: %s is available again", self._name, p.label)
        else:
            self._open(p, fail)

    def _open(self, p: _Provider, fail: _Fail) -> None:
        """AC7: open the breaker (5 → 15 → 30 min; key/credit/content policy
        at 30 at once) + one notice per provider per run (H4)."""
        p.level = len(BREAKER_S) if _severe(fail) else min(p.level + 1, len(BREAKER_S))
        cool = BREAKER_S[p.level - 1]
        p.open_until = self._clock() + cool
        p.probe_ok_at = None
        p.last_fail_kind = fail.kind
        self._record(
            "translate.provider_down",
            model=p.label,
            reason=fail.kind,
            minutes=int(cool // 60),
        )
        log.warning(
            "subs-translate %s: %s unavailable (probe kind=%s status=%s); "
            "tried again in %d min",
            self._name,
            p.label,
            fail.kind,
            fail.status,
            int(cool // 60),
        )
        if p.alerted:
            return
        p.alerted = True
        self._notify(
            Notice(
                f"{NOTICE_PROVIDER}{p.label}",
                f"translation model unavailable: {p.label}",
                f"{p.label}: {_reason(fail)}. Its lines go to the next translation "
                "model when one is set up (else they wait); it is tried again in "
                f"{int(cool // 60)} min.",
            )
        )

    def _notify(self, notice: Notice) -> None:
        with self._count_lock:
            self._notices.append(notice)

    # ── working a film ───────────────────────────────────────────────────
    def _work(self, film: _Film) -> str:
        """Translate until no open cue is left ("translated"), none has a
        route ("defer") or the chain is empty ("no_key"). A pass goes through
        the open cues in order; the chain, the failover switch and each
        provider's availability are re-read before EACH batch (AC1/G8)."""
        start = 0  # the pass's position: the cues before it wait for the next
        while True:
            if self._cancel.is_set():
                raise _Cancelled
            chain = self._refresh_chain()
            if not chain:
                return "no_key"
            self._recount(film, chain)
            open_cues = self._open_cues(film, chain)
            if not open_cues:
                return "translated"
            rest = [i for i in open_cues if i >= start]
            got = self._next_batch(film, rest, chain, self._failover_fn())
            if got is None:
                if start == 0:
                    return "defer"
                start = 0  # the next pass
                continue
            p, batch = got
            start = batch[-1] + 1
            if self._attempt(film, p, batch):
                with self._count_lock:
                    film.batches_done += 1
                self._recount(film, self._chain)

    def _next_batch(
        self,
        film: _Film,
        cues: Sequence[int],
        chain: Sequence[_Provider],
        failover: bool,
    ) -> Optional[tuple[_Provider, list[int]]]:
        """The first of `cues` that has a route, with the next cues on the
        same route (a cue without one is skipped) — at most that provider's
        batch size (H8). None: none of them has a route."""
        p: Optional[_Provider] = None
        batch: list[int] = []
        for i in cues:
            q = self._route(film, i, chain, failover)
            if q is None:
                continue
            if p is None:
                p = q
            elif q is not p:
                break
            batch.append(i)
            if len(batch) >= p.batch_size:
                break
        return None if p is None else (p, batch)

    def _attempt(self, film: _Film, p: _Provider, idx: list[int]) -> bool:
        """AC5/AC6: one top-level batch on `p`. True when it concluded (each
        cue done or its failure recorded); False when `p` opened or left the
        chain with the rest of the batch untouched."""
        got = self._call(p, film, idx)
        if not isinstance(got, _Fail):
            self._done(film, p, idx, got)
            if len(idx) >= p.batch_size:
                p.clean_successes += 1
            target = min(BATCH_SIZE, p.batch_size * 2)
            if (
                p.clean_successes >= 3
                and target > p.batch_size
                and p.shrinks.get(target, 0) < 2
            ):
                p.batch_size = target
                p.clean_successes = 0
                log.info(
                    "subs-translate %s: %s batch size now %d",
                    self._name,
                    p.label,
                    p.batch_size,
                )
            return True
        p.clean_successes = 0
        trigger = got  # the first failure: it drives the batch size (H8)
        if trigger.kind != "content":  # invalid output: bisect at once
            if _transient(trigger):
                for scheduled in RETRY_SCHEDULE_S:
                    if not self._sleep(self._retry_delay(got, scheduled), p):
                        return False
                    got = self._call(p, film, idx)
                    if not isinstance(got, _Fail):
                        self._done(film, p, idx, got)
                        return True
                    if not _transient(got):
                        break
            fail = self._probe(p, cached=True)
            if fail is not None:
                self._open(p, fail)
                return False
        return self._bisect(film, p, idx, trigger, got)

    @staticmethod
    def _retry_delay(f: _Fail, scheduled: float) -> float:
        if f.retry_after is not None and f.status in (429, 503):
            return min(float(f.retry_after), RETRY_AFTER_CAP_S)
        return scheduled

    def _bisect(
        self, film: _Film, p: _Provider, idx: list[int], trigger: _Fail, last: _Fail
    ) -> bool:
        """AC6: the failure is content-specific — split on `p`, one request
        per node, down to single cues (H5 leaf retry, I1 leaf probe), then
        ONE fresh confirming probe before any failed leaf is recorded.
        `trigger` (the attempt's first failure) decides the adaptive batch
        size (H8); `last` (its final failure) classifies a one-cue batch."""
        failed: list[tuple[int, bool]] = []  # (cue, transient → memory only)
        abandoned = False

        def leaf(i: int, fail: _Fail, root: bool) -> None:
            nonlocal abandoned
            if not root and _leaf_transient(fail):
                if not self._sleep(LEAF_RETRY_S, p):
                    abandoned = True
                    return
                got = self._call(p, film, [i])
                if not isinstance(got, _Fail):
                    self._done(film, p, [i], got)
                    return
                fail = got
                if _leaf_transient(fail):  # I1: an outage, or this cue?
                    probe = self._probe(p, cached=False)
                    if probe is not None:
                        self._open(p, probe)
                        abandoned = True
                        return
            failed.append((i, _leaf_transient(fail) or fail.message == _LENGTH_MESSAGE))

        def node(ix: list[int], fail: _Fail, root: bool) -> None:
            nonlocal abandoned
            if len(ix) == 1:
                leaf(ix[0], fail, root)
                return
            half = len(ix) // 2
            for part in (ix[:half], ix[half:]):
                if abandoned:
                    return
                if self._retired(p):
                    abandoned = True
                    return
                got = self._call(p, film, part)
                if isinstance(got, _Fail):
                    node(part, got, False)
                else:
                    self._done(film, p, part, got)

        node(idx, last, True)
        if abandoned:
            return False
        if failed:
            fail = self._probe(p, cached=False)
            if fail is not None:
                self._open(p, fail)
                return False
            refused = 0
            for i, transient in failed:
                if transient:
                    film.states[i].failed_by.add(p.label)
                else:
                    film.states[i].refused_by.add(p.label)
                    refused += 1
            if refused:
                log.info(
                    "subs-translate %s: %d line(s) refused by %s",
                    self._name,
                    refused,
                    p.label,
                )
                self._record("translate.refused", film, model=p.label, lines=refused)
                self._save(film)
        elif _shrinks_batch(trigger) and len(idx) > 1:
            p.shrinks[p.batch_size] = p.shrinks.get(p.batch_size, 0) + 1
            p.batch_size = max(MIN_BATCH_SIZE, p.batch_size // 2)
            p.clean_successes = 0
            log.info(
                "subs-translate %s: %s batch size now %d",
                self._name,
                p.label,
                p.batch_size,
            )
        return True

    def _done(
        self, film: _Film, p: _Provider, idx: list[int], got: dict[str, str]
    ) -> None:
        first = self._chain[0].label if self._chain else None
        with self._count_lock:
            for i in idx:
                st = film.states[i]
                st.zh, st.by = got[str(i + 1)], p.label
                film.n_done += 1
                film.n_new += 1
                if p.label != first:
                    film.n_fallback += 1
        self._save(film)

    def _save(self, film: _Film) -> None:
        states = [
            SavedCue()
            if st.blank
            else SavedCue(zh=st.zh, by=st.by, refused_by=tuple(sorted(st.refused_by)))
            for st in film.states
        ]
        if self._store.save(film.key, film.req.job_id, states):
            return
        if not self._checkpoint_alerted:
            self._checkpoint_alerted = True
            self._notify(
                Notice(
                    NOTICE_CHECKPOINT,
                    "translation checkpoint not saved",
                    "The translation checkpoint could not be written; translation "
                    "goes on, but a Stop now loses the lines not yet published.",
                )
            )

    def _probe(self, p: _Provider, *, cached: bool) -> Optional[_Fail]:
        """G5/H4: the Settings Test's request (the real prompt, one cue), with
        the provider's learned settings and the same cumulative fallback:
        omit the thinking-disable parameter on 400/422, then json_mode on 400.
        None = OK — only when the reply passes `_validate`. A cached OK
        (≤ 60 s) serves top-level decisions only."""
        if (
            cached
            and p.probe_ok_at is not None
            and self._clock() - p.probe_ok_at < PROBE_TTL_S
        ):
            return None
        got = self._send(p, probe_messages(), PROBE_MAX_TOKENS, None)
        if isinstance(got, _Fail):
            return got
        if not probe_ok(got):
            log.info(
                "subs-translate %s: probe unusable reason=unusable probe reply (%s)",
                self._name,
                p.label,
            )
            return _Fail("invalid", "unusable probe reply")
        p.probe_ok_at = self._clock()
        return None

    # ── requests ─────────────────────────────────────────────────────────
    def _call(
        self, p: _Provider, film: _Film, idx: list[int]
    ) -> Union[dict[str, str], _Fail]:
        """One translation request for the cues at positions `idx` (ids =
        position + 1; context = the 5 cues before the first)."""
        cues = film.req.cues
        ids = [str(i + 1) for i in idx]
        first = idx[0]
        user = {
            "cues": [{"id": d, "ja": cues[i].text} for d, i in zip(ids, idx)],
            "context": [c.text for c in cues[max(0, first - CONTEXT_SIZE) : first]],
        }
        chars = sum(len(cues[i].text) for i in idx)
        switch: Optional[dict[str, Any]] = None
        with self._count_lock:
            previous = film.last_provider
            if previous is not None and previous.label != p.label:
                reason = None
                if previous.open_until is not None:
                    reason = "unavailable"
                    film.left_open.add(previous.label)
                elif p.label in film.left_open and p.open_until is None:
                    reason = "recovered"
                    film.left_open.remove(p.label)
                elif previous.retired or previous not in self._chain:
                    reason = "changed"
                if reason is not None:
                    switch = {"from": previous.label, "to": p.label, "reason": reason}
                    if reason == "unavailable":
                        switch["kind"] = previous.last_fail_kind
            film.model = p.label
            film.last_provider = p
        # Logging is push-only; file I/O never holds the status/submit lock.
        self._log_start(film, p.label)
        if switch is not None:
            self._record("translate.switched", film, **switch)
        got = self._send(
            p,
            _messages(user),
            min(MAX_TOKENS, max(1024, 64 + 8 * chars)),
            film.req.job_id,
        )
        if isinstance(got, _Fail):
            return got
        validated = _validate(got, ids)
        if isinstance(validated, _Fail):
            log.info(
                "subs-translate %s: batch unusable reason=%s (%s)",
                self._name,
                bounded(validated.message, 80),
                p.label,
            )
        return validated

    def _send(
        self,
        p: _Provider,
        messages: list[dict[str, str]],
        max_tokens: int,
        tag: Optional[str],
    ) -> Union[str, _Fail]:
        """#201: omit the thinking-disable parameter on 400/422, then json_mode
        on 400, cumulatively. `thinking_off=False` omits that parameter; it
        does not explicitly enable thinking. Two successes immediately after
        omitting it teach the provider to omit it for the run; success with
        the parameter present resets the count. A later JSON fallback success
        does not count toward this learning. `tag` is None for probes."""
        json_mode = p.json_mode
        thinking_off = p.settings.thinking_off
        got = self._request(p, messages, max_tokens, json_mode, tag, thinking_off)
        if not isinstance(got, _Fail) and thinking_off:
            p.thinking_rejections = 0
        if thinking_off and isinstance(got, _Fail) and got.status in (400, 422):
            thinking_off = False
            got = self._request(p, messages, max_tokens, json_mode, tag, thinking_off)
            if not isinstance(got, _Fail):
                p.thinking_rejections += 1
                if p.thinking_rejections >= 2:
                    p.settings = replace(p.settings, thinking_off=False)
                    log.info(
                        "subs-translate %s: thinking parameter rejected; sending without it (%s)",
                        self._name,
                        p.label,
                    )
        if json_mode and isinstance(got, _Fail) and got.status == 400:
            got = self._request(p, messages, max_tokens, False, tag, thinking_off)
            if not isinstance(got, _Fail):
                p.json_mode = False
                log.info(
                    "subs-translate %s: %s rejects json_mode — off for this run",
                    self._name,
                    p.label,
                )
        return got

    def _request(
        self,
        p: _Provider,
        messages: list[dict[str, str]],
        max_tokens: int,
        json_mode: bool,
        tag: Optional[str],
        thinking_off: bool,
    ) -> Union[str, _Fail]:
        """One exchange with `p`'s worker → the reply content or a `_Fail`.
        Failure reasons originate in the worker's fixed/sanitised errors."""
        if self._cancel.is_set():
            raise _Cancelled
        rid = f"{'probe' if tag is None else tag}#{next(self._rids)}"
        line = json.dumps(
            {
                "id": rid,
                "messages": messages,
                "temperature": 0.3,
                "max_tokens": max_tokens,
                "json_mode": json_mode,
                "thinking_off": thinking_off,
                "timeout": int(REQUEST_TIMEOUT_S),
                "api_base": p.settings.api_base,
                "model": p.settings.model,
            }
        )
        started = time.monotonic()
        got = self._exchange(p, line, rid)
        log.info(
            "subs-translate %s: %s kind=%s status=%s latency_ms=%d%s (%s)",
            self._name,
            "probe" if tag is None else "batch",
            got.kind if isinstance(got, _Fail) else "ok",
            got.status if isinstance(got, _Fail) else None,
            int((time.monotonic() - started) * 1000),
            f" reason={bounded(got.message, 80)}" if isinstance(got, _Fail) else "",
            p.label,
        )
        return got

    def _exchange(self, p: _Provider, line: str, rid: str) -> Union[str, _Fail]:
        try:
            worker = self._ensure_worker(p)
        except _WorkerError as e:
            return _Fail("network", str(e))
        child = worker[0]
        try:
            child.write_line(line)
        except OSError as e:
            log.info(
                "subs-translate %s: worker gone on write (%s)",
                self._name,
                type(e).__name__,
            )
            self._drop_worker(p, worker)
            return _Fail("network", "worker gone")
        return self._await_reply(p, worker, rid)

    def _await_reply(
        self, p: _Provider, worker: _Worker, rid: str
    ) -> Union[str, _Fail]:
        child, lines = worker[0], worker[1]
        deadline = time.monotonic() + self._deadline_s
        rearmed = False
        while True:
            remaining = deadline - time.monotonic()
            try:
                if remaining <= 0:
                    raise queue.Empty
                msg = lines.get(timeout=remaining)
            except queue.Empty:
                log.info("subs-translate %s: worker response timeout", self._name)
                child.terminate_tree(1.0)
                self._drop_worker(p, worker)
                return _Fail("network", "response timeout")
            if msg is CANCELLED or self._cancel.is_set():
                raise _Cancelled
            if isinstance(msg, Eof):
                if msg.pid != child.pid:
                    continue  # not this worker's EOF (D4)
                self._drop_worker(p, worker)
                return _Fail("network", "worker exited")
            reply = self._parse_reply(msg)
            if reply is None or reply.get("id") != rid:
                if not rearmed:  # a stale/mismatched line: keep waiting, once
                    deadline = time.monotonic() + self._deadline_s
                    rearmed = True
                continue
            if reply.get("ok") is True:
                content = reply.get("content")
                if not isinstance(content, str):
                    return _Fail("bad_response", "reply without content")
                return content
            kind = reply.get("kind")
            message = reply.get("message")
            status = reply.get("status")
            retry_after = reply.get("retry_after")
            return _Fail(
                kind if isinstance(kind, str) and kind else "bad_response",
                message if isinstance(message, str) else "error",
                status if type(status) is int else None,
                retry_after if type(retry_after) is int and retry_after >= 0 else None,
            )

    @staticmethod
    def _parse_reply(msg: object) -> Optional[dict[str, Any]]:
        if not isinstance(msg, str):
            return None
        try:
            obj = json.loads(msg)
        except ValueError:
            return None
        return obj if isinstance(obj, dict) else None

    # ── worker lifecycle (one lazy worker per provider) ──────────────────
    def _ensure_worker(self, p: _Provider) -> _Worker:
        with self._spawn_lock:
            if self._cancel.is_set():
                raise _Cancelled
            if p.retired:  # H2: never respawn a departed fingerprint
                raise _WorkerError("retired")
            current = self._workers.get(p.fingerprint)
            if current is not None:
                return current
            lines: "queue.Queue[object]" = queue.Queue()
            try:
                child = self._spawn(
                    self._worker_argv_fn(),
                    env=worker_env(p.settings),  # C7: this provider's key only
                    stdin_pipe=True,
                    line_sink=lines,
                )
            except Exception as e:  # recorded as a network failure
                log.warning(
                    "subs-translate %s: worker spawn failed (%s)",
                    self._name,
                    type(e).__name__,
                )
                raise _WorkerError(f"spawn: {type(e).__name__}") from None
            keeper = self._job_fn(child.proc)
            worker: _Worker = (child, lines, keeper)
            if not self._cancel.is_set():  # post-spawn re-check (D6)
                self._workers[p.fingerprint] = worker
                return worker
        self._teardown([worker], wait_s=0.5)
        raise _Cancelled

    def _drop_worker(self, p: _Provider, worker: _Worker) -> None:
        with self._spawn_lock:
            if self._workers.get(p.fingerprint) is worker:
                del self._workers[p.fingerprint]
        self._teardown([worker], wait_s=0.5)

    def _retire_worker(self, p: _Provider) -> None:
        with self._spawn_lock:
            worker = self._workers.pop(p.fingerprint, None)
        if worker is not None:
            log.info(
                "subs-translate %s: %s left the provider chain — worker retired",
                self._name,
                p.label,
            )
            self._teardown([worker], wait_s=0.5)

    def _teardown(self, workers: Sequence[_Worker], *, wait_s: float) -> None:
        """C6: close every stdin → ONE shared bounded wait → tree-kill EVERY
        worker (CX4: also one whose launcher already exited — it may leave the
        real interpreter behind; `terminate_tree` reaches tracked orphans and is
        harmless on a gone tree) → join readers → close the job keepers.
        Bounded; never raises. Outside every lock: cancel() must never wait on
        it."""
        if not workers:
            return
        for child, _lines, _keeper in workers:
            child.close_stdin()
        deadline = time.monotonic() + wait_s
        for child, _lines, _keeper in workers:
            try:
                child.proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                continue  # a survivor: logged and tree-killed below
        for child, _lines, _keeper in workers:
            if child.poll() is None:
                log.info("subs-translate %s: worker ignored stdin EOF", self._name)
            child.terminate_tree(1.0)  # CX4: always, exited launcher or not
        for child, _lines, keeper in workers:
            child.join_readers(0.5)
            if keeper is not None:
                try:
                    keeper.close()
                except OSError as e:
                    log.warning(
                        "subs-translate %s: job keeper close failed (%s)",
                        self._name,
                        type(e).__name__,
                    )
