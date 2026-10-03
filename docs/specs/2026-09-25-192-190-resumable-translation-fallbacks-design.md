# #192 + #190 — resumable translation (断点续翻) with two fallback models; version 3.8.0

> **Evidence reconciliation — 2026-10-02:** This is a historical design, delivered in [PR #195](https://github.com/AlvinShenSSW/Taskpaw/pull/195). The original acceptance text is preserved. A checked item records the source/test contract described in the table below; it does not certify an installed app, external service or every native platform. Unchecked items retain superseded, partial or unverified clauses. Current audit work is indexed in [the follow-up](../audits/2026-10-02-audit-follow-up.md).

Date: 2026-09-25 (design v4, FROZEN — debate rounds 1–4: G1–G13, H1–H8, I1; round 4 CLEAN)
Issues: #192 (per-cue checkpoint, resume, retry without loss, per-provider breaker, failover) and #190
(two fallback models in Settings, probe, bisection, keep Japanese). One design, one PR.
Driver: `/afk` (Opus 5.5 leads; Opus pilots; Codex 外门 gpt-6-astra high; Kimi 终审). Release: the owner
authorised ONE release (v3.8.0) of #189 + #191 + this PR once it is merge-ready.

## Spec review

Owner run 2026-09-25 (standalone AV 翻译, ~200 grok batches): ABC-3620789 lost its whole translation to
one `auth` batch (HTTP 401/403 in 0.8 s after 11 good batches — a content refusal; the key kept working)
and LMNO-005 to one `refusal: empty reply`. Stop also loses the in-flight film. Owner decisions: two
optional fallback models in Settings (DeepSeek first, then MiMo Token Plan); when the primary and both
fallbacks refuse a line, keep its Japanese text; translation must resume and never lose a finished line.

## Acceptance criteria

- [ ] AC1 **Provider chain.** Settings gain 备用模型 1 / 备用模型 2 (API base, model, key). The chain =
  the *usable* providers among primary, fallback 1, fallback 2, in that order — usable = base and model
  non-empty and a key unless loopback (`needs_llm_key`), G11. Duplicates by label are dropped (first
  wins, one log warning, G9). Identity for persistence/display = `model_label(model, base)`; in memory a
  provider is keyed by its fingerprint (base, model, sha256(key)[:16] — never the raw key, H2) so a
  Settings fix resets its state (G8). The chain is re-read at every routing decision and every wake-up
  (G8); every wait is sliced to ≤ 60 s so a Settings change is acted on within a minute, and the worker
  of a fingerprint that left the chain is torn down at the next re-read (H2).
- [x] AC2 **Per-cue state.** `zh` (or none), `by` (label), `refused_by` (labels). done = has `zh`;
  **exhausted (H1)** = the current chain is non-empty AND `refused_by` contains every label of
  `seen` — the union of the chain's labels observed since the film was dequeued (so a provider removed
  mid-film can never make a line "exhausted"; such a film defers and, if the provider never returns,
  pauses at AC8's cap and is re-planned next Start); open = otherwise. **Empty chain (H1):** the current
  film and every deferred film return the no-key result (`skipped no_llm_key`, checkpoint kept) — never
  a publish. A cue whose Japanese text is blank is done at load with its own (blank) text — no request
  (G10).
- [ ] AC3 **Checkpoint.** One JSON per film under `<agent data dir>/subs-checkpoints/`, key =
  sha256(`srt.serialize(cues)`) (verified equal for WhisperJAV cues and the reloaded `.ja.srt`: critic
  experiment, 20 000 fuzz cases). Atomic write after every successful request and every recorded
  refusal. Loaded when the translator dequeues the film (C9). Deleted only after the plugin's zh
  publish returned `ok` (`translator.discard_checkpoint(key)`). Files untouched 30 days pruned at
  translator start. Corrupt / unknown version / cue-count mismatch → renamed `.bad` (or ignored),
  start over, warning. Holds labels only — never a key, userinfo or port. A write failure never fails
  the film: log + one notice per run; translation goes on in memory.
- [x] AC4 **Resume.** Only open cues are sent; a provider in a cue's `refused_by` is never asked again
  for it, incl. after resume; a new label is asked.
- [x] AC5 **Transient schedule (top-level requests only).** network / timeout / 5xx / 408 / 429 / 503 /
  `finish_reason=length` / an empty reply / other `bad_response`: retry the same batch after 10 s, 30 s,
  90 s (cancellable; 429/503 use `Retry-After` delta-seconds when given, capped 300 s). Still failing →
  **probe** (AC6). Invalid output (`content`: bad JSON / id mismatch / empty value) → bisect at once.
- [ ] AC6 **Probe, bisection, refusal (G1/G3/G4/G5/G6).** Triggers: the AC5 schedule exhausted; a
  `refusal` (content_filter / model refused); HTTP 401 / 402 / 403 / 404; any other 4xx after the
  json_mode retry (400 → once without json_mode; success → json_mode off for that provider for the run).
  The **probe** = one request with the real translation prompt and one cue `こんにちは` (json_mode as the
  provider currently uses, with the same 400 → no-json retry); **probe OK = the reply passes
  `_validate` for `{"1": …}`** (H4) — an envelope-OK but invalid reply is a probe failure of kind
  `invalid`. A probe result is cached 60 s for top-level decisions only.
  - Probe OK → the failure is content-specific → **bisect** the batch on that provider: each node is ONE
    request (no schedule); ok → done; failure → split; a single cue whose failure is transient (network /
    timeout / 5xx / 429) gets ONE retry after 10 s (H5); **a leaf still failing transiently after that
    retry gets a fresh probe at once (I1): probe fails → provider-level, the bisection is abandoned (no
    leaf recorded, its cues untouched); probe OK → continue.** After all leaves of the bisection, ONE
    fresh **confirming probe** (never the cache, G6/H8): OK → every failed leaf gets `refused_by +=
    provider` (reason refused / invalid persisted; reason `failed` — a transient leaf — kept in memory
    only, so a resume asks again, H5); the confirming probe fails → provider-level, no leaf recorded.
  - **Retired mid-attempt (H2):** before every retry of the schedule and every bisection node the
    provider's fingerprint is checked against the chain; a retired one ends the attempt with its cues
    untouched (no respawn of the retired worker).
  - **Adaptive batch size (H8):** when a bisection was triggered by a timeout or `finish_reason=length`
    and its halves succeeded, that provider's batch size halves for the run (floor 5).
  - Probe fails → **provider-level**: the breaker opens (AC7) and one notice per provider per run whose
    text follows the probe's failure (H4): 401 / 403 / 402 → "key or credit"; 404 → "model or URL not
    found"; 429 → "rate limit or quota"; other 4xx → "rejected the request (HTTP n)"; refusal → "content
    policy rejected the translation prompt" (G5); `invalid` → "returns unusable output"; network / 5xx →
    "unreachable".
  - **Termination (G1/H6):** every top-level attempt ends with its cues done, refused by that provider
    (≤ 4 schedule tries + 1 probe + 2n−1 bisection requests + ≤ n leaf retries + ≤ n I1 leaf probes +
    1 confirming probe), or
    the provider open with the cues untouched. An open provider is retried only after a cool-down AND a
    successful probe (≥ 5 min apart). So each (cue, provider) pair ends done or refused, or its provider
    stays open — and then the film ends through AC8's cap. Every film ends translated (AC9), paused
    (AC8) or no-key (AC2).
- [x] AC7 **Breaker + failover.** A provider opens on a failed probe: level 1 = 5 min, then 15, then 30
  (a failed probe at a cool-down end escalates); auth / 402 / content-policy start at 30 min. At a
  cool-down end the next routing that needs it probes it; OK closes it (level reset). **Failover**
  (default on; Settings switch 「主模型不可用时改用备用模型」, #192 §4): while a provider is open, a cue that
  would go to it goes to the next provider it has not refused; with the switch off, a cue not refused by
  `chain[0]` (the first usable provider — the primary when usable, H7) waits for it (the film is deferred,
  AC8). Each cue records its `by`.
- [ ] AC8 **Defer, never block, pause after 2 h (G2).** A film whose remaining open cues have no
  available provider is **deferred**: the translator moves on to the next queued film; a deferred film
  is resumed as soon as a provider it needs closes again (probe OK at a cool-down end). With only
  deferred films left, the translator waits (cancellable) for the earliest cool-down end or deferral
  deadline. A film whose **accumulated** deferred time reaches 2 h (H6: the sum of its deferred
  intervals) returns outcome `paused` (checkpoint kept)
  → plugin settles `skipped` reason `translation_paused` (「翻译服务不可用，可续」), streak neutral; one
  alert per run (「N 部片翻译服务不可用，已暂停，下次 Start 继续」) and the run's `done` text gains
  `; N paused` (G7). The plugin's `done` waits for deferred films — bounded by 2 h. **While any film is
  queued, worked or deferred (H3):** `in_flight()` is true and stands for exactly one film — the worked
  one, or, when only deferred films remain, the deferred film with the earliest deadline; `queued()`
  counts every OTHER queued or deferred film (so `queued() + in_flight()` = the unsettled films);
  `progress()` reports that same film (`paused: true`, `deferred: n` when it is a deferred one).
- [x] AC9 **Outcomes.** Every cue done or exhausted → `translated` (exhausted cues carry their Japanese
  text) with counts `resumed`, `fallback`, `kept_ja` and `checkpoint_key`. `failed` only for an I/O or
  internal error. Plugins: `translated` → publish as today; kept-ja → one info log per film and the
  `done` text gains `; N lines kept in Japanese` only when N > 0; `paused` → AC8.
- [x] AC10 **Key check chain-aware; one rule in both plugins (G11).** A film is `skipped no_llm_key`
  only when the chain is empty. The translator's own "no LLM key" result settles `skipped no_llm_key` in
  BOTH plugins (Jasna today: failed + alert + streak — unified with avsubs).
- [x] AC11 **Settings UI + API.** Two fallback sections (base, model, write-only key with Save / Clear /
  Test) under the primary + the failover switch (`llm_failover`, default true); env overrides
  `TASKPAW_LLM_FALLBACK1_API_KEY` / `TASKPAW_LLM_FALLBACK2_API_KEY`; `GET /control/config` masks both new
  keys `***` + `…_source` (by name; a test asserts no stored key value is returned anywhere); keep on
  blank / `***`, `null` clears; `llm_test(candidate, slot)` sends **the real probe** (G5) for any slot,
  with the same 400 → no-json retry and the same OK rule (H4);
  live apply after Save; zh + en.
- [x] AC12 **Progress.** `Translator.progress()` adds `cues_resumed`, `cues_fallback`, `cues_kept_ja`,
  `paused` (bool: the current film is deferred/waiting), `deferred` (count); `model` = the provider in
  use right now. The translate panel shows 已续翻 / 备用模型 / 保留日文 tiles when > 0 and a 等待翻译服务
  chip while paused. No new step state (C8).
- [ ] AC13 Version 3.8.0; CHANGELOG; README; openclaw guide (metrics, `translation_paused`, alerts,
  settings); tests per the plan; `uv run pytest`, ruff, mypy, UI lint, vitest, tsc green.

## Frozen issue contract

**In scope:** AC1–AC13 (#192 and #190). **User-visible changes allowed:** Settings sections + failover
switch; films no longer fail on a refusal, an outage or a Stop; deferred/paused films; kept-Japanese
lines; new progress tiles; new alerts; `done` text suffixes; Jasna "no LLM key" → skipped; the Settings
Test uses the translation probe; version 3.8.0.

**Invariants:** constitution §2 (atomic writes; no secret in argv/logs/files/metrics/notices; control
API loopback), §4 (no silent except; monotonic scheduling; clean shutdown within the 5 s stop budget —
cancel with 3 workers measured 0.7–0.8 s), §5 (tests). The translator thread never writes a subtitle
and never touches plugin counters (it writes only its checkpoint files). One client thread per
Translator; requests sequential. GPU lease, ASR, restore and the #191 publish/recognition rules
unchanged. The #187 `.ja.srt` rule unchanged. Tests never use the network, real keys, real exes or the
real `%APPDATA%`.

**Corrections from repository evidence:**

| # | Issue text | Evidence | Correction |
|---|---|---|---|
| C1 | "honour Retry-After" | `llm._send` drops response headers | `LLMError.retry_after` (delta-seconds) → worker reply `retry_after` → translator |
| C2 | "401/403 → probe" | the translator drops the worker's `status` | carry `status` into `_Fail` |
| C3 | "credit problem → breaker" | DeepSeek insufficient balance = 402 (docs), today `bad_response` | 402 (and 404, other 4xx) trigger the probe (AC6) |
| C4 | key over "the `.ja.srt` cue list" | fresh ASR cues keep WhisperJAV indices; resume renumbers | key = sha256(`srt.serialize(cues)`), verified |
| C5 | "TaskPaw's data directory" | plugins have no path; tests would write the real `%APPDATA%` (#68) | `core/datadir.py` holder set ONLY from `run_agent`'s `config_path` folder (never `default_config_path()`); None = no persistence; tests' autouse fixture resets it; an unwritable folder (e.g. Linux `/etc/taskpaw`) → write failure notice |
| C6 | "one lazy worker per provider" | cancel ≈ 1 s per worker; 5 s budget | close every worker's stdin, one shared ≤ 1 s wait, `terminate_tree` survivors (measured 0.7–0.8 s for 3) |
| C7 | fallback keys in env | `worker_env` copies all of `os.environ` | each worker env drops every `TASKPAW_LLM_*` then sets only its own key |
| C8 | "paused" | no such tracker/UI state; UI drops unknown states | `paused` is a translate-step number; the step stays `active` |
| C9 | checkpoint read at submit | translate-only films queued in bulk | read at dequeue |
| C10 | "5 consecutive failed batches open the breaker"; "split in halves once"; "2 h run end" | G1/G2/G3: as written they loop, block the queue or mass-skip | the probe decides after the schedule (AC5/AC6); defer instead of block, per-film 2 h cap (AC8) |

**#201 update:** the recovery rule in `2026-09-26-201-thinking-off-design.md` supersedes the following batch-size residual: three clean full batches double the size (cap 40), except into a size that shrank twice in the run.

**Accepted residual (superseded by #201):** a provider's halved batch size (H8) never grows back within a run (cost
only). **Non-goals (OUT-OF-SCOPE):** re-translating published films or their kept-Japanese lines; parallel
requests; streaming; sharing one film's checkpoint across instances (overlapping Jasna + AV 翻译 on the
same film is unsupported — worst case lost progress, never corruption); env keys inherited by jasna.exe /
lada / custom commands (same as today's primary key, G12); the unreadable-`.ja.srt` asymmetry.

**Causal boundary:** `core/llm.py`, `core/llm_worker.py`, `core/config.py`, `core/datadir.py` (new),
`agent/server/admin.py`, `agent/server/app.py`, `launcher.py`, `monitors/subs/translate.py`,
`monitors/subs/checkpoint.py` (new), `monitors/subs/util.py`/`progress.py` (numbers only),
`plugins/jasna.py`, `plugins/avsubs.py`, `ui/src/views/Settings.tsx`, `ui/src/api.ts`, `ui/src/i18n.ts`,
`ui/src/components/PipelineProgress.tsx` (+ helpers), `tests/conftest.py`, tests,
`examples/agent.example.yaml`, README, guide, CHANGELOG, version files, this doc.

## Assumptions

| # | Claim | Basis | Risk if wrong |
|---|---|---|---|
| A1 | DeepSeek reports insufficient balance as HTTP 402 | public docs (unverified: no network) | any other shape → schedule → probe fails → provider-level anyway (AC6); no loss |
| A2 | DeepSeek and the MiMo Token Plan speak OpenAI-compatible `/v1/chat/completions` | DeepSeek docs; owner enters them | a misconfigured fallback → schedule → probe fails → provider-level + notice (AC6) |
| A3 | the probe (real prompt + `こんにちは`) is accepted by each provider | unverified per provider | a provider rejecting the prompt → "content policy" notice, 30 min breaker, failover; the owner sees it at Settings Test (AC11) |
| A4 | `serialize(parse(serialize(x))) == serialize(x)` | VERIFIED (critic, 20 000 fuzz + 31 tricky texts) | — |
| A5 | xAI content refusals arrive as 401/403 or an empty reply | owner's log | other shapes → schedule → probe OK → bisect → refused (AC6); no loss, no loop |

## Module design

### Settings and holders (AC1, AC11)
- `AgentConfig`: `llm_fallback1_api_base/model/api_key`, `llm_fallback2_…` (default ""), `llm_failover:
  bool = True`; validators as the primary. `resolve_llm_settings(config, slot, environ)`.
- `LLMChain`: holder `set_llm_chain/get_llm_chain` (usable providers, deduped) published at boot and
  after Save; `get_llm_settings()` stays the primary; `get_llm_failover()`.
- `admin`: editable + live lists, keep/clear for both keys, `llm_test(candidate, slot)` = the probe;
  `app`: mask + sources by name. UI `Settings.tsx`: a `LlmSection` component with a `slot` prop reused
  three times + the failover switch; i18n `settings.llmFallback*`, `settings.llmFailover*`.

### LLM core (C1–C3)
`LLMError(kind, message, status, retry_after=None)`; `_http_error(code, headers)`; worker error line adds
`retry_after`; translator `_Fail(kind, message, status=None, retry_after=None)`.

### Checkpoint store (`subs/checkpoint.py`, AC3)
`CheckpointStore(directory: Optional[Path])` with `key(cues)`, `load(key, n)`, `save(key, film, states)`
(tmp + `os.replace`, never raises, returns ok), `delete(key)`, `prune(max_age_s)`. File `{"v": 1, "key",
"film", "cues": [{"zh"?, "by"?, "refused_by": [...]}]}`.

### Translation engine (`subs/translate.py`, AC2–AC10)
- Injected: `chain_fn`, `failover_fn`, `checkpoint_dir`, `clock` (monotonic), `wait_fn(seconds) ->
  cancelled` (default `self._cancel.wait`).
- `ProviderState` per fingerprint: worker (lazy, own env, C7), `json_mode`, `open_until`, `level`,
  `probe_ok_at`, `alerted`, `label`.
- Loop: (1) re-read the chain; (2) choose work — a deferred film that now has a routable cue (probing a
  provider whose cool-down ended), else the next queued film (load checkpoint, blank cues done), else
  wait (cancellable) for the earliest cool-down end / deferral deadline / new request; (3) work the
  film: route each open cue to the first chain provider it has not refused that is closed (or passes a
  probe after its cool-down); failover off → only `chain[0]` for cues it has not refused (H7); group
  consecutive cues with the same route into batches ≤ 40; `_attempt` per AC5/AC6; (4) no open cue →
  `translated`; open cues but none routable → defer (its deferred time accumulates while deferred,
  H6); a film whose accumulated deferred time reaches 2 h → `paused`.
- Notices (`drain_notices()`): provider-level (once per provider per run), checkpoint write failure
  (once per run). The paused-films alert and the `done` suffixes are the plugins' (they count settles).

### Plugins (AC8–AC10)
`Translator(run, name=…, checkpoint_dir=get_data_dir()/"subs-checkpoints" if set else None)`; chain-aware
key check; results: `translated` → publish → `ok` → settle completed → `discard_checkpoint`; `paused` →
skipped `translation_paused` + per-run alert; no-key → skipped (both); notices → alerts (dedupe keys);
`done` text suffixes. Stop unchanged (the checkpoint holds all but the in-flight request).

## Test plan
- `test_subs_checkpoint.py`: key equality (ASR cues vs reloaded file); round trip; corrupt / version /
  n mismatch; atomic (no partial file); prune; None = memory; no secret; write failure → ok False.
- `test_subs_translate.py` (fake workers; fake clock + wait): AC5 schedule + Retry-After cap; empty
  reply retried at top level only; AC6 probe OK → bisect → exactly the refused cue fails over, the rest
  stay on the primary; leaf re-probe (a key failure mid-bisection is provider-level, not refused); probe
  failed → breaker + one notice with the right text; 400 → json_mode off; 402/404 → probe; **G1: a cue
  failing transiently on every provider ends kept-Japanese within a bounded request count**; breaker
  5/15/30 escalation and close on probe OK; failover on/off; **G2: grok healthy with both fallbacks down
  → the next film translates at once, the film needing a fallback is deferred, resumes when a fallback
  closes, or pauses after 2 h**; all providers down → every film deferred → paused at 2 h; Stop mid-film →
  a new Translator resumes only open cues; a crash between a request and its checkpoint write repeats at
  most that batch; checkpoint write failure → translation continues + one notice; chain re-read + reset
  on fingerprint change; duplicate labels; blank cues; workers per provider with their own env only;
  cancel with 3 workers within budget; progress keys (update `PROGRESS_KEYS`); **round 2:** the chain
  becomes empty mid-film and while films are deferred → no publish, no-key result, checkpoint kept (H1);
  a label removed mid-film → the film defers, never kept-Japanese from the removal (H1); a Settings
  change wakes a deferred-only wait within 60 s and retires the old worker (H2); in_flight / queued /
  progress with only deferred films (H3); probe OK needs valid content; notice text per failure kind;
  the Test's no-json retry (H4); a transient leaf gets one retry and a `failed` refusal is not persisted
  (H5); accumulated deferral (H6); failover off with an unusable primary uses chain[0] (H7); one
  confirming probe per bisection; adaptive batch size (H8); **round 3:** an outage beginning mid-
  bisection → the provider opens within ~log2(n) node requests and the next film fails over (I1); a
  fingerprint retired mid-attempt ends it without respawn (H2); `queued() + in_flight()` equals the
  unsettled films (H3).
- `test_llm.py`: Retry-After parsing; 402/404 status carried; worker `retry_after`.
- Settings: `test_admin.py` / `test_agent.py` (both keys masked + sources, keep/clear, env overrides,
  slot probe test, failover switch live, no stored key value anywhere in `GET /control/config`);
  `settings.test.tsx`.
- Plugins: chain-aware key check; Jasna no-key → skipped; `paused` → skipped neutral + one alert +
  suffix; kept-ja log + suffix; notices → alerts deduped; checkpoint discarded after publish ok only;
  Stop → next Start resumes (checkpoint dir = tmp); data dir None in all other tests.
- `conftest.py`: strip every `TASKPAW_LLM_*` by prefix; reset the chain and data-dir holders.

## Handoff notes
Pilot split: **A** core (llm/worker status + Retry-After, config fields + chain/failover/datadir holders,
admin/app/launcher, conftest) → **B** engine + checkpoint store (after A's API) → **D** plugins + docs
(after B); **C** UI (Settings sections + switch, progress tiles) in parallel with A. Never touch the
owner's live agent/config/library; no network; no real keys; no WhisperJAV/GPU.

## Acceptance evidence — 2026-10-02

Baseline source: [91f7745](https://github.com/AlvinShenSSW/Taskpaw/commit/91f7745564f17a0039f81fab8293a9c150232236). Historical delivery: [PR #195](https://github.com/AlvinShenSSW/Taskpaw/pull/195), merge [44d5042](https://github.com/AlvinShenSSW/Taskpaw/commit/44d50422cbb15b893b8c69e4b47f998de3906907). [Recorded CI](https://github.com/AlvinShenSSW/Taskpaw/actions/runs/36150320520/job/108121649259) applies to that historical head, not every prose clause or installed machine. Source/test links below describe the baseline; historical version/default statements remain unchanged. The later source version 3.9.8 is recorded in [PR #214](https://github.com/AlvinShenSSW/Taskpaw/pull/214); it supersedes earlier version clauses without proving installation.

| AC | Disposition | Source / automated evidence | Qualification |
|---|---|---|---|
| AC1 | Unchecked; see qualification | [translate.py: llm_chain_from_config](../../taskpaw_v3/monitors/subs/translate.py); [translate.py: _refresh_chain](../../taskpaw_v3/monitors/subs/translate.py); [translate.py: _fingerprint](../../taskpaw_v3/monitors/subs/translate.py); tests: [test_subs_translate.py: test_settings_change_wakes_a_deferred_wait_within_60s_and_retires](../../taskpaw_v3/tests/test_subs_translate.py); [test_subs_translate.py: test_duplicate_labels_in_the_chain_first_wins](../../taskpaw_v3/tests/test_subs_translate.py) | Usable ordered chain, safe fingerprints and deferred-wait wakeup/retirement are tested with fake providers/clock. Active response waits do not re-read the chain and may re-arm their deadline; the original one-minute Settings guarantee and every departed worker teardown are not established. Native worker ownership remains R08 (#225). |
| AC2 | Implemented; automated evidence | [translate.py: _CueState](../../taskpaw_v3/monitors/subs/translate.py); [translate.py: _exhausted](../../taskpaw_v3/monitors/subs/translate.py); [translate.py: _refresh_chain](../../taskpaw_v3/monitors/subs/translate.py); tests: [test_subs_translate.py: test_a_provider_removed_mid_film_defers_never_kept_japanese](../../taskpaw_v3/tests/test_subs_translate.py); [test_subs_translate.py: test_chain_emptied_mid_film_gives_no_key_and_keeps_the_checkpoint](../../taskpaw_v3/tests/test_subs_translate.py); [test_subs_translate.py: test_blank_cues_are_done_at_load_and_never_sent](../../taskpaw_v3/tests/test_subs_translate.py) | Cue-state/exhaustion, seen-label, empty-chain and blank-cue contracts have direct tests. |
| AC3 | Unchecked; see qualification | [checkpoint.py: CheckpointStore](../../taskpaw_v3/monitors/subs/checkpoint.py); [translate.py: _save](../../taskpaw_v3/monitors/subs/translate.py); tests: [test_subs_checkpoint.py: test_key_equal_for_asr_cues_and_the_reloaded_ja_srt](../../taskpaw_v3/tests/test_subs_checkpoint.py); [test_subs_checkpoint.py: test_atomic_no_partial_file_on_replace_failure](../../taskpaw_v3/tests/test_subs_checkpoint.py); [test_subs_translate.py: test_checkpoint_write_failure_continues_with_one_notice](../../taskpaw_v3/tests/test_subs_translate.py) | Atomic/load/prune/privacy/retry behaviors are tested. Original 20,000-case critic experiment has no independent published reproduction here; whole compound item stays unchecked. |
| AC4 | Implemented; automated evidence | [translate.py: _open_cues](../../taskpaw_v3/monitors/subs/translate.py); [translate.py: _dequeue](../../taskpaw_v3/monitors/subs/translate.py); tests: [test_subs_translate.py: test_resume_never_asks_a_provider_that_refused_the_cue](../../taskpaw_v3/tests/test_subs_translate.py); [test_subs_translate.py: test_stop_mid_film_then_a_new_translator_resumes_only_open_cues](../../taskpaw_v3/tests/test_subs_translate.py) | Resume tests use real checkpoint files and controlled workers; only open/unrefused cues are routed. |
| AC5 | Implemented; automated evidence | [translate.py: _attempt](../../taskpaw_v3/monitors/subs/translate.py); [translate.py: _retry_delay](../../taskpaw_v3/monitors/subs/translate.py); tests: [test_subs_translate.py: test_transient_schedule_is_10_30_90_on_the_same_batch](../../taskpaw_v3/tests/test_subs_translate.py); [test_subs_translate.py: test_retry_after_for_429_and_503_capped](../../taskpaw_v3/tests/test_subs_translate.py); [test_subs_translate.py: test_retryable_error_kinds_follow_the_schedule](../../taskpaw_v3/tests/test_subs_translate.py) | Top-level schedules, caps, cancellation and immediate invalid-content split are automated with fake clocks/responses. |
| AC6 | Unchecked; see qualification | [translate.py: _probe](../../taskpaw_v3/monitors/subs/translate.py); [translate.py: _bisect](../../taskpaw_v3/monitors/subs/translate.py); [translate.py: _attempt](../../taskpaw_v3/monitors/subs/translate.py); tests: [test_subs_translate.py: test_h8_one_confirming_probe_for_all_failed_leaves](../../taskpaw_v3/tests/test_subs_translate.py); [test_subs_translate.py: test_i1_outage_mid_bisection_opens_within_log2n_nodes_and_fails_over](../../taskpaw_v3/tests/test_subs_translate.py); [test_subs_translate.py: test_h2_a_provider_retired_mid_attempt_ends_it_without_respawn](../../taskpaw_v3/tests/test_subs_translate.py) | Routing/probe/bisection cases are extensive; universal termination also depends on worker containment and bounded teardown, still R08 ([#225](https://github.com/AlvinShenSSW/Taskpaw/issues/225)). Later parameter retry behavior in [#202](https://github.com/AlvinShenSSW/Taskpaw/pull/202) is additive. |
| AC7 | Implemented; automated evidence | [translate.py: _open](../../taskpaw_v3/monitors/subs/translate.py); [translate.py: _half_open](../../taskpaw_v3/monitors/subs/translate.py); [translate.py: _route](../../taskpaw_v3/monitors/subs/translate.py); tests: [test_subs_translate.py: test_breaker_escalates_5_15_30_and_closes_on_probe_ok](../../taskpaw_v3/tests/test_subs_translate.py); [test_subs_translate.py: test_failover_switch](../../taskpaw_v3/tests/test_subs_translate.py); [test_subs_translate.py: test_h7_failover_off_with_an_unusable_primary_uses_chain0](../../taskpaw_v3/tests/test_subs_translate.py) | Breaker/escalation/failover semantics are directly covered by simulated failures and time. |
| AC8 | Unchecked; see qualification | [translate.py: _defer](../../taskpaw_v3/monitors/subs/translate.py); [translate.py: _idle](../../taskpaw_v3/monitors/subs/translate.py); [translate.py: in_flight](../../taskpaw_v3/monitors/subs/translate.py); tests: [test_subs_translate.py: test_all_providers_down_every_film_deferred_then_paused_at_2h](../../taskpaw_v3/tests/test_subs_translate.py); [test_subs_translate.py: test_h6_deferral_accumulates_across_intervals](../../taskpaw_v3/tests/test_subs_translate.py); [test_subs_translate.py: test_h3_only_deferred_films_in_flight_queued_and_progress](../../taskpaw_v3/tests/test_subs_translate.py) | Fake-clock tests prove cap/count semantics; unconditional never-block/bounded-done also depends on native worker teardown (R08). No two-hour production soak was run. |
| AC9 | Implemented; automated evidence | [translate.py: _finish](../../taskpaw_v3/monitors/subs/translate.py); [avsubs.py: _settle_results](../../taskpaw_v3/monitors/plugins/avsubs.py); [jasna.py: _settle_results](../../taskpaw_v3/monitors/plugins/jasna.py); tests: [test_subs_translate.py: test_refused_by_every_provider_keeps_japanese](../../taskpaw_v3/tests/test_subs_translate.py); [test_avsubs.py: test_kept_japanese_lines_are_logged_per_film_and_suffix_the_done_text](../../taskpaw_v3/tests/test_avsubs.py) | Translated/kept-Japanese counts and plugin suffix/publish paths are tested; real translation quality is outside this proof. |
| AC10 | Implemented; automated evidence | [avsubs.py: _alert_no_key](../../taskpaw_v3/monitors/plugins/avsubs.py); [jasna.py: _alert_no_key](../../taskpaw_v3/monitors/plugins/jasna.py); tests: [test_avsubs.py: test_a_no_llm_key_translator_result_is_a_skip](../../taskpaw_v3/tests/test_avsubs.py); [test_jasna_subs.py: test_a_no_key_result_is_a_skip_with_one_alert_never_a_failure](../../taskpaw_v3/tests/test_jasna_subs.py) | Both plugins use chain-aware no-key skips with shared translator outcomes and direct cases. |
| AC11 | Implemented; automated evidence | [admin.py: update_config](../../taskpaw_v3/agent/server/admin.py); [admin.py: llm_test](../../taskpaw_v3/agent/server/admin.py); [Settings.tsx: Settings](../../taskpaw_v3/ui/src/views/Settings.tsx); tests: [test_admin.py: test_update_config_fallbacks_and_failover_are_live_and_publish_the_chain](../../taskpaw_v3/tests/test_admin.py); [test_agent.py: test_control_config_never_returns_a_stored_or_env_key_value](../../taskpaw_v3/tests/test_agent.py); [test_admin.py: test_llm_test_ok_only_when_the_reply_is_a_valid_translation](../../taskpaw_v3/tests/test_admin.py); [settings.test.tsx](../../taskpaw_v3/ui/src/test/settings.test.tsx) | Fallback fields, mask/keep/clear/env, live holders and candidate probe validation are implemented and covered. Local-origin authorization remains separate R01; this row only asserts this AC's field/probe contract. |
| AC12 | Implemented; automated evidence | [translate.py: progress](../../taskpaw_v3/monitors/subs/translate.py); [PipelineProgress.tsx: PipelineProgress](../../taskpaw_v3/ui/src/components/PipelineProgress.tsx); tests: [test_subs_translate.py: test_progress_reports_resumed_fallback_kept_and_the_model_in_use](../../taskpaw_v3/tests/test_subs_translate.py); [pipelineprogress.test.tsx](../../taskpaw_v3/ui/src/test/pipelineprogress.test.tsx) | Resumed/fallback/kept/paused/deferred/model fields and zh/en conditional tiles are covered by translator/component tests. |
| AC13 | Unchecked; see qualification | [test_version.py: test_version_matches_python_source_of_truth](../../taskpaw_v3/tests/test_version.py); tests: [test_version.py: test_version_matches_python_source_of_truth](../../taskpaw_v3/tests/test_version.py) | Historical 3.8.0/docs and recorded checks delivered by [#195](https://github.com/AlvinShenSSW/Taskpaw/pull/195); version later superseded. No complete original plan certification or installed/provider claim. |
