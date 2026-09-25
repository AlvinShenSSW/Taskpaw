"""Global LLM API settings + a synchronous OpenAI-compatible `chat()` (#178).

One place to configure the LLM for the whole agent (base URL, model, key) so the
Jasna "AV translate" tick box (#177) and the `avsubs` task (#179) never carry
their own credentials.

- **Settings** resolve env-first (constitution §2): `TASKPAW_LLM_API_KEY` wins
  when non-blank, else the stored `agent.yaml` key, else none. #190/#192 add two
  fallback provider slots with their own env keys
  (`TASKPAW_LLM_FALLBACK1_API_KEY`, `TASKPAW_LLM_FALLBACK2_API_KEY`).
- **Holders**: process-wide immutable snapshots, set at boot by the launcher and
  after each successful Settings save (live-apply); monitors read them at call
  time — no change to the plugin protocol. `get_llm_settings()` is the primary;
  `get_llm_chain()` the usable providers in order and `get_llm_failover()` the
  failover switch (#192 AC1).
- **`chat()`** is stdlib `urllib` only, never follows redirects (D10), maps every
  failure to an `LLMError` with a FIXED message (never exception text, headers or
  body — the key must not leak, D1/D2) plus its HTTP status and `Retry-After`
  delta-seconds (#192 C1), and logs exactly one kind/status/latency line. It has no wall-clock deadline: callers that must be cancellable run it in
  the `llm-worker` child process (`core/llm_worker.py`).
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Iterable, Literal, Mapping, Optional

from taskpaw_v3 import __version__

if TYPE_CHECKING:
    from taskpaw_v3.core.config import AgentConfig

log = logging.getLogger("taskpaw.llm")

DEFAULT_LLM_API_BASE = "https://api.x.ai/v1"
DEFAULT_LLM_MODEL = "grok-4.3"
# Every LLM variable of the agent's environment starts with this (the keys, and
# the llm-worker's own base/model): children strip them all (C7, #177 AC10).
LLM_ENV_PREFIX = "TASKPAW_LLM_"
LLM_KEY_ENV = "TASKPAW_LLM_API_KEY"
LLM_FALLBACK1_KEY_ENV = "TASKPAW_LLM_FALLBACK1_API_KEY"
LLM_FALLBACK2_KEY_ENV = "TASKPAW_LLM_FALLBACK2_API_KEY"
# C1: a Retry-After above this is ignored (None), never clamped.
RETRY_AFTER_MAX_S = 3600
# The `refusal` message of a blank reply: the translator retries it as a
# transient failure at top level (#192 AC5), unlike a content-filter refusal.
EMPTY_REPLY_MESSAGE = "empty reply"

KeySource = Literal["env", "config", "none"]
ErrorKind = Literal["auth", "rate_limit", "refusal", "network", "bad_response"]
# #190/#192: the provider slots, in chain order.
LLMSlot = Literal["primary", "fallback1", "fallback2"]
LLM_SLOTS: tuple[LLMSlot, ...] = ("primary", "fallback1", "fallback2")
LLM_SLOT_KEY_ENV: Mapping[str, str] = MappingProxyType(
    {
        "primary": LLM_KEY_ENV,
        "fallback1": LLM_FALLBACK1_KEY_ENV,
        "fallback2": LLM_FALLBACK2_KEY_ENV,
    }
)


@dataclass(frozen=True)
class LLMSettings:
    api_base: str
    model: str
    api_key: str
    key_source: KeySource


def _check_slot(slot: object) -> None:
    if slot not in LLM_SLOTS:
        raise ValueError("unknown LLM slot (primary, fallback1 or fallback2)")


def llm_slot_fields(slot: str) -> tuple[str, str, str]:
    """The `AgentConfig` field names `(api_base, model, api_key)` of a provider
    slot: `llm_…` for the primary, `llm_fallbackN_…` for a fallback. Raises
    ValueError for an unknown slot."""
    _check_slot(slot)
    prefix = "llm_" if slot == "primary" else f"llm_{slot}_"
    return f"{prefix}api_base", f"{prefix}model", f"{prefix}api_key"


def resolve_llm_settings(
    api_base: str,
    model: str,
    api_key: str,
    *,
    slot: str = "primary",
    environ: Optional[Mapping[str, str]] = None,
) -> LLMSettings:
    """Env-first key resolution: the SLOT's own env var (`LLM_SLOT_KEY_ENV`) —
    never another slot's. A whitespace-only env var or stored key counts
    as absent, and the key is always stripped (a CR/LF key would otherwise make
    http.client raise with the key in the message, D1)."""
    _check_slot(slot)
    env = os.environ if environ is None else environ
    env_key = str(env.get(LLM_SLOT_KEY_ENV[slot], "") or "").strip()
    stored = str(api_key or "").strip()
    key: str
    source: KeySource
    if env_key:
        key, source = env_key, "env"
    elif stored:
        key, source = stored, "config"
    else:
        key, source = "", "none"
    return LLMSettings(
        api_base=str(api_base or "").strip().rstrip("/"),
        model=str(model or "").strip(),
        api_key=key,
        key_source=source,
    )


def llm_settings_from_config(
    config: "AgentConfig",
    *,
    slot: str = "primary",
    environ: Optional[Mapping[str, str]] = None,
) -> LLMSettings:
    base_field, model_field, key_field = llm_slot_fields(slot)
    return resolve_llm_settings(
        getattr(config, base_field),
        getattr(config, model_field),
        getattr(config, key_field),
        slot=slot,
        environ=environ,
    )


def without_llm_env(base: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """A copy of `base` (default: the agent's environment) minus every
    `TASKPAW_LLM_*` variable (case-insensitive) — the one filter for every
    child that must not inherit an LLM key (the ASR child, and each llm-worker
    before it gets its own settings, C7)."""
    src: Mapping[str, str] = os.environ if base is None else base
    return {k: v for k, v in src.items() if not k.upper().startswith(LLM_ENV_PREFIX)}


def _default_settings() -> LLMSettings:
    return LLMSettings(DEFAULT_LLM_API_BASE, DEFAULT_LLM_MODEL, "", "none")


_holder_lock = threading.Lock()
_holder: LLMSettings = _default_settings()
_chain: tuple[LLMSettings, ...] = ()
_failover = True


def set_llm_settings(settings: LLMSettings) -> None:
    """Publish a new immutable snapshot (boot + after each successful save)."""
    global _holder
    with _holder_lock:
        _holder = settings


def get_llm_settings() -> LLMSettings:
    """The current snapshot. Before the launcher initialises it: the defaults
    with no key (`key_source="none"`), so a caller can tell it is unconfigured."""
    with _holder_lock:
        return _holder


def set_llm_chain(chain: Iterable[LLMSettings], *, failover: bool = True) -> None:
    """Publish the provider chain and the failover switch together (#192 AC1;
    boot + after each successful save). `chain` = the USABLE providers in slot
    order, deduplicated by label — built by `subs.translate.llm_chain_from_config`,
    which owns the usable rule (`needs_llm_key`) and the label (`model_label`)."""
    global _chain, _failover
    snapshot = tuple(chain)
    with _holder_lock:
        _chain, _failover = snapshot, bool(failover)


def get_llm_chain() -> tuple[LLMSettings, ...]:
    """The usable providers in order (primary → fallback 1 → fallback 2). Empty
    before the launcher initialises it and whenever no provider is usable."""
    with _holder_lock:
        return _chain


def get_llm_failover() -> bool:
    """The failover switch (#192 §4); True (the default) before init."""
    with _holder_lock:
        return _failover


def reset_llm_settings() -> None:
    """Test hook (autouse fixture, D8): every holder back to its pre-init
    default — the primary, an empty chain, failover on."""
    set_llm_settings(_default_settings())
    set_llm_chain((), failover=True)


class LLMError(Exception):
    """The only exception `chat()` raises. `message` is always one of a fixed set
    of strings — never exception text, headers or body content (D1).
    `retry_after` is a server's `Retry-After` in delta-seconds (C1), else None."""

    def __init__(
        self,
        kind: ErrorKind,
        message: str,
        status: Optional[int] = None,
        retry_after: Optional[int] = None,
    ):
        super().__init__(message)
        self.kind: ErrorKind = kind
        self.message = message
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True)
class ChatResult:
    content: str
    finish_reason: str
    model: str
    latency_ms: int


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a 3xx: urllib would re-send the request (as a GET) to another
    origin. Returning None makes the 3xx surface as an HTTPError (D10)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_DEFAULT_OPENER = urllib.request.build_opener(_NoRedirect())

# finish_reason values echoed into an error message must look like an enum token;
# anything else is server-controlled text and is reported generically.
_FINISH_TOKEN = re.compile(r"[A-Za-z0-9_\-]{1,40}")
# Retry-After as delta-seconds: ASCII digits only (an HTTP-date is ignored, C1).
_DELTA_SECONDS = re.compile(r"[0-9]{1,10}")


def _envelope_error() -> LLMError:
    return LLMError("bad_response", "invalid response")


def _retry_after(headers: Any) -> Optional[int]:
    """C1: the `Retry-After` header as delta-seconds in 0–`RETRY_AFTER_MAX_S`,
    else None — an HTTP-date, a sign, a fraction, non-ASCII digits or a value
    over the bound are ignored (never clamped)."""
    raw = headers.get("Retry-After") if headers is not None else None
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not _DELTA_SECONDS.fullmatch(value):
        return None
    seconds = int(value)
    return seconds if seconds <= RETRY_AFTER_MAX_S else None


def _http_error(code: int, headers: Any = None) -> LLMError:
    retry_after = _retry_after(headers)
    if code in (401, 403):
        return LLMError("auth", "authentication failed", code, retry_after)
    if code == 429:
        return LLMError("rate_limit", "rate limited", code, retry_after)
    if 300 <= code < 400:
        return LLMError(
            "bad_response", f"HTTP {code} redirect not followed", code, retry_after
        )
    return LLMError("bad_response", f"HTTP {code}", code, retry_after)


def _is_timeout(e: BaseException) -> bool:
    if isinstance(e, urllib.error.URLError) and not isinstance(
        e, urllib.error.HTTPError
    ):
        return isinstance(e.reason, TimeoutError)
    return isinstance(e, TimeoutError)  # socket.timeout is TimeoutError (3.10+)


def _send(settings: LLMSettings, data: bytes, timeout: float, opener: Any) -> bytes:
    """Build + send the request and read the raw body; map transport failures."""
    try:
        request = urllib.request.Request(
            f"{settings.api_base}/chat/completions", data=data, method="POST"
        )
        request.add_header("Content-Type", "application/json")
        request.add_header("User-Agent", f"TaskPaw/{__version__}")
        if settings.api_key:
            # Unredirected: never re-sent to a redirect target (D10). Absent for
            # an empty key (local Ollama).
            request.add_unredirected_header(
                "Authorization", f"Bearer {settings.api_key}"
            )
        with opener.open(request, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:  # before URLError/OSError: it is both
        raise _http_error(e.code, e.headers) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        # URLError (DNS/refused), socket timeout, reset, IncompleteRead,
        # BadStatusLine, RemoteDisconnected… (D2).
        raise LLMError(
            "network", "timeout" if _is_timeout(e) else "network error"
        ) from None
    except ValueError:  # incl. UnicodeEncodeError: illegal header chars (D1)
        # http.client's message EMBEDS the header value — never surface it.
        raise LLMError("auth", "API key or URL contains invalid characters") from None


def _parse(
    settings: LLMSettings, raw: bytes, strict: bool, started: float
) -> ChatResult:
    try:
        body = json.loads(raw)
    except ValueError:  # JSONDecodeError / UnicodeDecodeError
        raise _envelope_error() from None
    if not isinstance(body, dict):
        raise _envelope_error()
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise _envelope_error()
    choice = choices[0]
    if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
        raise _envelope_error()
    message = choice["message"]
    # Some OpenAI-compatible servers omit finish_reason (A3) → treat as stop.
    finish_reason = choice.get("finish_reason") or "stop"
    if finish_reason == "content_filter":
        raise LLMError("refusal", "finish_reason=content_filter")
    if message.get("refusal"):
        raise LLMError("refusal", "model refused")
    content = message.get("content")
    model = body.get("model")
    served = model if isinstance(model, str) and model else settings.model
    if finish_reason == "length":
        if strict:
            raise LLMError("bad_response", "finish_reason=length")
        # Lenient (llm_test, D4/D14): a reply cut off by max_tokens still proves
        # auth + connectivity + model + envelope; content checks are skipped.
        return ChatResult(
            content if isinstance(content, str) else "",
            "length",
            served,
            _elapsed_ms(started),
        )
    if finish_reason != "stop":
        shown = (
            finish_reason
            if isinstance(finish_reason, str) and _FINISH_TOKEN.fullmatch(finish_reason)
            else "unexpected"
        )
        raise LLMError("bad_response", f"finish_reason={shown}")
    if not isinstance(content, str):
        raise _envelope_error()
    if not content.strip():
        raise LLMError("refusal", EMPTY_REPLY_MESSAGE)
    return ChatResult(content, "stop", served, _elapsed_ms(started))


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def chat(
    settings: LLMSettings,
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.3,
    max_tokens: Optional[int] = None,
    json_mode: bool = False,
    timeout: float = 30.0,
    strict: bool = True,
    opener: Any = None,
) -> ChatResult:
    """POST `{api_base}/chat/completions` and validate the envelope.

    `timeout` is urllib's socket-level timeout (connect + each read, C4) — not a
    wall-clock deadline. `strict=False` accepts a `length`-truncated reply (D4).
    `opener` is anything with `.open(request, timeout=)` (D16); default = the
    module's no-redirect opener. Raises only `LLMError` (D1/D2)."""
    started = time.monotonic()
    try:
        payload: dict[str, Any] = {
            "model": settings.model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        data = json.dumps(payload).encode("utf-8")
        raw = _send(settings, data, timeout, opener or _DEFAULT_OPENER)
        result = _parse(settings, raw, strict, started)
    except LLMError as e:
        log.info(
            "llm chat: %s status=%s latency_ms=%d",
            e.kind,
            e.status,
            _elapsed_ms(started),
        )
        raise
    except Exception as e:  # catch-all: nothing but LLMError escapes (D2)
        err = LLMError("bad_response", f"unexpected error: {type(e).__name__}")
        log.info(
            "llm chat: %s status=%s latency_ms=%d",
            err.kind,
            err.status,
            _elapsed_ms(started),
        )
        raise err from None
    log.info("llm chat: ok model=%s latency_ms=%d", result.model, result.latency_ms)
    return result
