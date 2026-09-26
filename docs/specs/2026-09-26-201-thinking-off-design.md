# #201 — Translation speed: turn thinking off for DeepSeek / MiMo, log why replies are unusable, let the batch size recover; version 3.9.2

Date: 2026-09-26 (design v3, FROZEN — debate rounds 1–3: T201-1…T201-10, N1–N6; round 3 CLEAN; wording V3-1…V3-3 applied)
Issue: #201. Owner: "需要 现在开issue然后afk来跑 排在200前面发一版 版本号增加0.0.1".
Driver: `/afk` — Claude leads; implementation Codex gpt-6-astra (high); outer gate Claude; final gate DeepSeek flash.
Merge when AFK merge-ready, then release 3.9.2.

## Spec review

A live AV 翻译 run with `deepseek-flash` as the main model crawls. What the agent log shows, 2026-09-26:

- 307 of the last 400 requests came back `bad_response`, at 1–3 s latency each.
- The batch size sits at its floor of 5.

Why this is likely:

- DeepSeek's and MiMo's new models think by default. Both turn it off with `"thinking": {"type": "disabled"}` ([DeepSeek thinking-mode guide](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode), [MiMo deep-thinking guide](https://mimo.mi.com/docs/en-US/quick-start/usage-guide/text-generation/deep-thinking)).
- Reasoning tokens count toward the reply budget. The translator's per-batch budget is only `min(4096, 64 + 8 × chars)`.

Measured with the translator's own prompt:

- **deepseek-flash:** about 1.5 s per 40 bland lines, with or without thinking.
- **mimo-v2.6-flash:** 26–34 s per 40 lines with thinking, 6–11 s without.

Two gaps in the current code:

- The log names only the failure kind, not the reason.
- The batch size halves on a timeout or a length cut-off (H8, #192) and never grows back within a run.

## Frozen issue contract

### AC1 Settings

- `AgentConfig` gains `llm_thinking_off`, `llm_fallback1_thinking_off` and `llm_fallback2_thinking_off`, each `Optional[bool] = None`. `None` means **automatic**.
- A `mode="before"` validator maps a string that is blank or `auto` after `strip().lower()` to `None`. Any other value is a bool, as pydantic parses it.
- `taskpaw_v3/examples/agent.example.yaml` documents exactly three values: `true`, `false`, and blank-or-`auto` (T201-7).
- An existing `agent.yaml` without the fields loads as automatic.
- All three fields are editable (`_EDITABLE_CONFIG`) and live (`_LIVE_CONFIG`). Saving republishes the settings and the chain, as today.

### AC2 Effective value (core)

- `core/llm.py` gains `thinking_off_default(api_base) -> bool`.
  - It is True when the URL's hostname (lower-case, via `urlsplit`) is `api.deepseek.com`, ends with `.deepseek.com`, is `xiaomimimo.com` or ends with `.xiaomimimo.com`. Otherwise it is False.
  - It never raises.
- `LLMSettings` gains a trailing field `thinking_off: bool = False`, so existing positional constructions keep working.
- `llm_settings_from_config(cfg, slot)` resolves it: the explicit bool, else `thinking_off_default(base)`.
- `model_label` ignores it.

### AC3 Requests

- `chat()` adds `"thinking": {"type": "disabled"}` to the body when `settings.thinking_off` is True, and nothing otherwise.
- **Worker:** the llm-worker request line gains an optional strict-bool `thinking_off`, default False.
  - A non-bool value is an invalid request.
  - It is applied via `dataclasses.replace` on the effective settings, like the `api_base` / `model` overrides. The key stays env-only.
- The translator's `_request` sends `"thinking_off": p.settings.thinking_off`, for batches and for probes.
- The Settings **Test** carries it, because it builds its settings with `llm_settings_from_config`.
- **400 fallback (T201-4, N1, N4, N6)** mirrors the json_mode fallback and is CUMULATIVE within one `_send`. J = json_mode, T = thinking; "rejected" = HTTP 400, or 422 for the thinking step only.
  1. Send (J, T) as the provider's current flags say.
  2. If rejected and T was sent → send (J, ¬T).
  3. If still rejected and J was sent → send (¬J, T′), where T′ = ¬T if step 2 dropped it (a dropped `thinking` is never re-sent), else T.
  4. A success at step 3 turns json_mode off for the provider (today's rule). The thinking flag is NOT learned from it. A step-2 success counts as one "occurrence" of a thinking rejection.
  - The provider stops sending `thinking` for the rest of the run after **2 occurrences** in the run, so one non-deterministic 400 does not silently put DeepSeek/MiMo back on thinking (N4). This is logged once: `… thinking parameter rejected; sending without it (<label>)`. Until then each rejected batch pays one extra request. A success of a request that CARRIED `thinking` resets the occurrence count (V3-2), so two one-off 400s far apart in a long run do not flip it.
  - The probe shares `_send`, so it follows the same steps.
  - A service that rejects BOTH parameters converges: (J,T) → (J,¬T) → (¬J,¬T) succeeds (critic Exp R2-c: 6 requests).
  - **Settings Test** uses four steps, so its note blames the right parameter: (J,T) → (J,¬T) → (¬J,T) → (¬J,¬T). The first one that succeeds decides the message:
    - (J,¬T): ok, plus 「该服务不支持关闭思考参数，已按默认发送」;
    - (¬J,T): ok, with no note (today has none; V3-1);
    - (¬J,¬T): ok, plus the thinking note. The note travels in a new OPTIONAL result field `note: "thinking_unsupported"`, present only when a thinking-less step succeeded after the thinking step was rejected (so today's pinned `{ok, model, latency_ms}` set holds otherwise); the UI maps it to the zh/en string.
    
    A slot with T = False keeps today's two steps.
- **Budget floor (T201-3):** a batch's `max_tokens` becomes `min(MAX_TOKENS, max(1024, 64 + 8 × chars))`.
  - This protects a reasoning model that keeps thinking: a manual 「不发送」, another reasoning provider, or a proxy host. It is no longer cut off on a tiny budget.
  - The ceiling costs nothing unless it is used. The probe keeps `PROBE_MAX_TOKENS` (4096).

### AC4 Live change

- The flag becomes part of the translator's provider fingerprint (`_fingerprint`).
- Toggling it in Settings takes effect at the next chain refresh: before the next film, batch, retry or wait.
- That provider state starts fresh (breaker, batch size, json_mode, the thinking fallback), consistent with "a Settings change resets its state".
- Persisted checkpoint labels (`model_label`) are unaffected.
- Two slots that differ ONLY in this setting have the same label. The existing label dedupe keeps the first and logs a warning. The Settings hint and the README document this (T201-10).

### AC5 Why a reply was unusable

- **One layout for every line (T201-8):**
  `subs-translate <task>: <probe|batch> kind=<k> status=<s> latency_ms=<n> reason=<text> (<label>)`
  - `reason=` appears only on failures, always before the label.
  - The text is the `_Fail.message`, bounded to 80 chars. Examples: `finish_reason=length`, `reply without content`, `invalid response`, `empty reply`, `HTTP 400`, `timeout`.
- **Content failures found after a reply arrived** are logged as:
  - `subs-translate <task>: batch unusable reason=<text> (<label>)`, for the `_validate` failures such as `invalid JSON` and `id set mismatch`.
  - `… probe unusable reason=unusable probe reply (<label>)`.
- The log never contains reply content, cue text, keys or URLs with userinfo. The reason texts form a closed set: fixed messages, a sanitised `finish_reason` token, or `spawn: <Type>`.

### AC6 Batch size recovers, without flapping (T201-1)

Per provider:

- **Clean successes.** The provider counts consecutive **clean** top-level successes. A clean success is a batch that:
  - was answered on its first send (the json_mode / thinking 400 resend inside `_send` still counts as the first send, T201-9);
  - needed no scheduled retry and no bisection;
  - had a size ≥ the provider's current `batch_size`.
  
  Batches cut short by the film's end or by routing never count.
- **Growing.** After 3 in a row, `batch_size` doubles, capped at `BATCH_SIZE` (40). The exception is a target size that has already had to shrink **twice** in this run for this provider: that is a proven ceiling, and the size stays. The counter resets after a grow, on any failed top-level batch, and on any shrink.
- **Tracking shrinks.** A shrink records the size it shrank FROM, as a per-provider count.
- **Critic simulation** (1200 cues, a 20-cue ceiling):
  - Today: 65 requests, 130 s of retry sleep.
  - This rule: 70 requests, 260 s.
  - The v1 rule flapped: 120 requests, 1560 s.
  - A transient bad stretch recovers to 40 with 42 requests, against 69 today.
- **Logging.** A grow is logged like a shrink: `… batch size now N`.
- **Unchanged:** the floor (5), the halving rule (H8) and today's pinned request sequences. The progress view's `batches_total` estimate may drop mid-film after a grow; that is fine.
- The #192 design doc's "never grows back within a run" residual gets a note that #201 changes it.

### AC6b A cut-off is not a refusal (T201-2, N3)

- A single-cue request whose reply ends with `finish_reason=length` is recorded in `_bisect`'s transient `failed` list, i.e. `failed_by`, memory only.
- `_leaf_transient` is unchanged: there is no extra leaf retry or fresh probe for a cut-off, since retrying the same cue on the same budget rarely helps.
- `_exhausted` uses `refused_by ∪ failed_by`, so a cue never loops within a film. Deferral and the 2 h pause behave as before.
- It is NOT a persisted refusal (`refused_by`). So the line is asked again later, and of other models, instead of being written off for that model in the checkpoint.
- **Residual, documented in the CHANGELOG:** lines ALREADY recorded as refused in an existing checkpoint stay refused for that model. An example is a film interrupted under 3.9.1 while thinking ate the budget. That film then uses the fallbacks or keeps those lines in Japanese. Deleting the film's checkpoint re-asks everything.

### AC7 UI (Settings)

- Each LLM card (主模型, 备用模型 1, 备用模型 2) gets a 「思考」 select after the key field.
  - Choices: 「自动」 → `null`, 「关闭思考」 → `true`, 「不发送（服务默认）」 → `false`.
  - `false` sends nothing, i.e. the service's own default; it does not force thinking on (T201-5).
- The select saves with the card's **Save**, like the base, model and key; it does not save on change.
- **The 「自动」 label** shows the automatic result for the SAVED API base, e.g. 「自动（当前：关闭思考）」 or 「自动（当前：不发送）」.
  - `GET /control/config` reports it per slot as `{prefix}thinking_off_auto: bool`, computed from the stored base, next to the key `_source` fields.
  - While the base field is being edited, the label drops the 「当前…」 part (T201-6). "Edited" means it differs from the saved one after `trim()` and removing trailing `/` on both sides, like the server's normalisation (N5). There is no client-side copy of the host rule.
- **Save** and **Test** both carry the slot's `{prefix}thinking_off` value.
  - The Test candidate treats the thinking field by presence: `null` means automatic, not "keep the stored value".
  - The Test shows the thinking-400 note from AC3 when it applies.
- **Hint:** 「DeepSeek、MiMo 默认关闭思考，翻译更快；其它服务可能不支持此参数。两个模型槽只差这一项时视为同一模型。」, with an English equivalent.
- zh + en strings throughout.

### AC8 Unchanged

Keys and their masking and env overrides, failover, refusal handling (except AC6b), checkpoints, publishing, status payloads, the Hub.

### AC9 Docs and version

- 3.9.2 in the six version files.
- CHANGELOG 3.9.2 (Chinese). It must say:
  - after the upgrade, DeepSeek and MiMo slots switch to thinking-off automatically;
  - which models were verified;
  - the escape hatch (「不发送」);
  - the AC6b residual.
- README and OpenClaw guide LLM sections: one line on the new setting.
- `taskpaw_v3/examples/agent.example.yaml`.

### AC10 Tests

No network, no real keys.

- **Config:** defaults; `" Auto "` / `""` → None; bool values; PATCH persistence; the live / non-live partition.
- **`thinking_off_default`:** DeepSeek and MiMo hosts; subdomains; ports; uppercase; userinfo; look-alikes such as `deepseek.com.evil.test`, `evildeepseek.com` and `api.deepseek.com@evil.test`; malformed URLs.
- **Settings resolution:** `llm_settings_from_config` for each slot.
- **`chat()`:** the body with and without `thinking`.
- **Worker:** pass-through, and the invalid non-bool case.
- **Translator:**
  - the request line carries the flag, for batches and probes;
  - a toggle takes effect at the next refresh, with fresh provider state;
  - the thinking 400/422 fallback is cumulative: a service rejecting BOTH converges and the json step never re-sends a dropped `thinking`; the flag is learned only after 2 step-2 occurrences, never from a step-3 success; the probe follows it;
  - the budget floor (1024);
  - the reason is logged for transport and content failures, in the one layout (`probe unusable` pinned);
  - no content, key or cue text appears in the log;
  - regrow after 3 clean successes, but not after a retry win, a bisection win or a short batch;
  - the cap is 40;
  - the counter resets on failure, shrink and grow;
  - a proven ceiling (a size that shrank twice) is never regrown into; add a "ceiling" test next to the H8 tests;
  - a one-cue length cut-off is `failed_by` (via `_bisect`'s transient list), not `refused_by`, and is not persisted; no extra leaf retry for it;
  - the existing H8 sequences are unchanged.
- **Admin Test:** carries the flag; null means automatic; the four-step sequence with correctly attributed notes.
- **GET:** reports `…_thinking_off_auto`.
- **UI:**
  - the select's three states;
  - the automatic label from GET, and dropped while the base is edited;
  - Save and Test payloads;
  - zh / en.

## Invariants

- A request carries `thinking` only when the slot's effective value is True, and only until that provider has rejected it (400/422) twice with no successful thinking request in between (V3-3).
- Nothing about keys changes. The new logging adds only bounded, non-secret reason text.
- Failover and refusal semantics are unchanged, except AC6b.
- Behaviour changes only in: the request parameter, the 400 fallback, the budget floor, the batch-size recovery, and the one-cue length cut-off.

## Assumptions

- **A1** Other OpenAI-compatible services may reject the unknown `thinking` parameter with HTTP 400, or 422 on FastAPI-style servers (handled for the thinking step only, N6).
  - Automatic is on only for DeepSeek and MiMo hosts. Wherever it is sent, the 400 fallback (AC3) drops it and carries on.
  - Verified models: deepseek-flash and mimo-v2.6-flash (2026-09-26). Other models on those hosts rely on the fallback.
- **A2** Whether thinking caused the live `bad_response`s is not confirmed; the new reason logging will show it. Turning thinking off removes reasoning tokens entirely. In measurement it was faster for MiMo and equal for DeepSeek.
- **A3** Regrow uses 3 clean successes, doubles (5 → 10 → 20 → 40), and never grows into a size that shrank twice in the run. It recovers from a transient bad stretch without flapping against a real ceiling (critic simulation, T201-1). A ceiling that later lifts (load) stays proven for the rest of the run: the same final size as 3.9.1, at a bounded extra cost (critic R2-a).

## Test plan

Tests first, per AC10, in:

- `tests/test_core.py`, `tests/test_llm.py`, `tests/test_admin.py`, `tests/test_agent.py`, `tests/test_subs_translate.py`
- `ui/src/test/settings.test.tsx`

Existing assertions that must change are listed in the pilot report, each with its reason:

- the exact `values()` payloads in the Settings tests gain `…thinking_off`;
- the `_LIVE_CONFIG` partition gains three fields;
- `test_llm.py:233`'s DeepSeek fallback settings gain `thinking_off=True`;
- `test_batch_shaping_and_success` follows the 1024 floor (its last 10-cue batch asserts 384); `test_max_tokens_is_bounded` is unaffected (N2).

Then run the full `uv run pytest`, ruff, ruff format and mypy; and for the UI, lint, vitest and tsc.
