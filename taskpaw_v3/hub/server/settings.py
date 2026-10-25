"""#299 R14/R15: the Hub's settings from its UI, without ever rewriting
hub.yaml.

Each edited setting is an override in the store's `config` table (the
existing `polling_token` pattern). At startup `effective_hub_config` lays the
overrides over hub.yaml, each validated on its own: an invalid one is ignored
and the log names its key, never its value. `openclaw_enabled`,
`openclaw_token`, `openclaw_url` and `polling_token` are read live by the
poller; every other setting applies after a restart.

`GET /config/settings` shows the saved and the running values (tokens masked
as "***"); `PUT /config/settings` takes a partial patch with per-field
Chinese errors ("***" keeps a token, null clears it); `POST
/config/openclaw/test` sends one test notification with the form's address
and token; `POST /restart` restarts the Hub (only a concurrent save blocks).
"""

from __future__ import annotations

import logging
import socket
import threading
import time
import urllib.error
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any, Callable, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.background import BackgroundTask

from taskpaw_v3.core.config import HubConfig
from taskpaw_v3.core.net import bind_is_loopback, guard_bind_exposure

if TYPE_CHECKING:
    from taskpaw_v3.hub.server.store import HubStore

log = logging.getLogger("taskpaw.hub.settings")

LIVE = ("openclaw_enabled", "openclaw_token", "openclaw_url", "polling_token")
RESTART = (
    "poll_interval",
    "write_status_md",
    "status_md_path",
    "status_log_retention_days",
    "lan_access",
    "bind_host",
    "api_token",
)
OVERRIDES = LIVE + RESTART
SECRETS = ("openclaw_token", "polling_token", "api_token")
_BOOLS = ("openclaw_enabled", "write_status_md", "lan_access")
_INTS = ("poll_interval", "status_log_retention_days")
POLL_CHOICES = (15, 30, 60)
MASK = "***"
HINT_MIN = 16
_MISSING = "\x00missing"
BUSY_SAVE = "正在保存其他设置，请稍后再试"
RESTARTING = {"detail": "正在重启", "code": "restarting"}


def _encode(key: str, value: Any) -> str:
    if key in _BOOLS:
        return "1" if value else "0"
    if value is None:
        return ""
    return str(value)


def _decode(key: str, raw: str) -> Any:
    if key in _BOOLS:
        if raw not in ("0", "1"):
            raise ValueError(key)
        return raw == "1"
    if key in _INTS:
        return int(raw)
    if key == "status_md_path":
        return raw or None
    return raw


def stored_overrides(store: "HubStore") -> dict[str, str]:
    found: dict[str, str] = {}
    for key in OVERRIDES:
        raw = store.get_config(key, _MISSING)
        if raw != _MISSING:
            found[key] = raw
    return found


def effective_hub_config(config: HubConfig, store: "HubStore") -> HubConfig:
    """R15: hub.yaml with every valid store override laid over it."""
    data = config.model_dump()
    for key, raw in stored_overrides(store).items():
        try:
            value = _decode(key, raw)
            candidate = HubConfig(**{**data, key: value})
            _check_value(key, value)
        except (ValueError, TypeError, ValidationError):
            log.warning("hub setting override ignored: %s", key)
            continue
        data = candidate.model_dump()
    merged = HubConfig(**data)
    try:
        guard_bind_exposure(merged.bind_host, merged.api_token, label="Hub API")
    except ValueError:
        log.warning("hub setting overrides ignored: lan_access, bind_host, api_token")
        for key in ("lan_access", "bind_host", "api_token"):
            data[key] = getattr(config, key)
        merged = HubConfig(**data)
    return merged


def live_openclaw_url(store: "HubStore", fallback: str) -> str:
    """#299 D4 / review CX4: the poller's per-delivery address — the store
    override validated like `effective_hub_config` does, else hub.yaml's."""
    raw = store.get_config("openclaw_url", _MISSING)
    if raw == _MISSING:
        return fallback
    try:
        return _check_url(raw)
    except ValueError:
        return fallback


CONTROLLER_KEY = "controller_server_id"
NO_SUCH_SERVER = "未找到这台设备"


def controller_server_id(store: "HubStore") -> Optional[int]:
    """#299 review IR2: the agent the operator chose as 主控 (None: not
    chosen, or that server was removed) — never an implicit first server."""
    raw = store.get_config(CONTROLLER_KEY, "")
    if not raw.isdigit():
        return None
    sid = int(raw)
    return sid if store.get_server(sid) is not None else None


def token_hint(token: str) -> Optional[str]:
    token = (token or "").strip()
    return token[-4:] if len(token) >= HINT_MIN else None


def _control(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _check_url(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise ValueError("请填写 http:// 或 https:// 开头的地址")
    value = value.strip()
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("请填写 http:// 或 https:// 开头的地址")
    if parts.username or parts.password or _control(value):
        raise ValueError("地址格式不正确")
    return value


def _check_path(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("格式不正确")
    text = value.strip()
    if not text:
        return None
    if _control(text) or text.startswith(("\\\\", "//")):
        raise ValueError("不能是网络路径")
    if ".." in PureWindowsPath(text).parts or ".." in PurePosixPath(text).parts:
        raise ValueError("不能包含 ..")
    if not Path(text).expanduser().is_absolute():
        raise ValueError("必须是完整路径")
    return text


def _check_value(key: str, value: Any) -> Any:
    """One field's rule (beyond the model's); ValueError carries the message."""
    if key in _BOOLS:
        if not isinstance(value, bool):
            raise ValueError("格式不正确")
        return value
    if key == "poll_interval":
        if isinstance(value, bool) or value not in POLL_CHOICES:
            raise ValueError("请选择 15、30 或 60 秒")
        return value
    if key == "status_log_retention_days":
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 3650
        ):
            raise ValueError("请填写 0–3650 之间的天数")
        return value
    if key == "openclaw_url":
        return _check_url(value)
    if key == "status_md_path":
        return _check_path(value)
    if key in SECRETS:
        if not isinstance(value, str) or _control(value) or len(value) > 1024:
            raise ValueError("令牌格式不正确")
        return value.strip()
    if key == "bind_host":
        if not isinstance(value, str):
            raise ValueError("格式不正确")
        return value.strip()
    raise ValueError("未知字段")


def masked(config: HubConfig) -> dict[str, Any]:
    view: dict[str, Any] = {key: getattr(config, key) for key in OVERRIDES}
    for key in SECRETS:
        view[key] = MASK if getattr(config, key) else ""
    view["polling_token_hint"] = token_hint(config.polling_token)
    view["status_md_default"] = str(Path(config.data_dir).expanduser() / "status.md")
    return view


class HubSettings:
    """The saved overrides, one writer at a time."""

    def __init__(self, yaml_config: HubConfig, running: HubConfig, store: "HubStore"):
        self.yaml_config = yaml_config
        self.running = running
        self.store = store
        self._lock = threading.Lock()
        self.restarting = False  # #299 review IR5: set by a granted restart

    @contextmanager
    def admission(self):
        if not self._lock.acquire(timeout=1):
            raise ValueError("operation_busy")
        try:
            if self.restarting:
                raise ValueError("restarting")
            yield
        finally:
            self._lock.release()

    def saved(self) -> HubConfig:
        return effective_hub_config(self.yaml_config, self.store)

    def pending(self) -> list[str]:
        saved = self.saved()
        return [k for k in RESTART if getattr(saved, k) != getattr(self.running, k)]

    def view(self) -> dict[str, Any]:
        from taskpaw_v3.core import lan

        saved = self.saved()
        fields = self.pending()
        return {
            "saved": masked(saved),
            "running": masked(self.running),
            "restart_required": bool(fields),
            "fields": fields,
            "addresses": [a.view() for a in lan.local_addresses()],
            "controller_server_id": controller_server_id(self.store),
        }

    def bind_controller(self, ip: str, port: int) -> dict[str, Any]:
        """#299 Codex r4: 更换主控 names the server row for that address the
        controller (an existing one, else a new one) — it never edits the polled
        row, whose event cursor and acks belong to the old agent. Address edits
        stay with PATCH /servers/{id} for the same agent."""
        with self.admission():
            match = next(
                (
                    s
                    for s in self.store.list_servers()
                    if s["ip"].lower() == ip.lower() and s["port"] == port
                ),
                None,
            )
            if match is None:
                names = {s["name"] for s in self.store.list_servers()}
                name, n = "主控", 1
                while name in names:
                    n += 1
                    name = f"主控 {n}"
                sid = self.store.add_server(name, ip, port)
            else:
                sid = match["id"]
                if not match["enabled"]:
                    self.store.set_server_enabled(sid, True)
            self.store.set_config(CONTROLLER_KEY, str(sid))
            log.info("hub controller bound to server %s", sid)
            return {"ok": True, "id": sid, "created": match is None}

    def update(self, patch: Any) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise Refused({"settings": "格式不正确"})
        with self.admission():
            saved = self.saved()
            data = saved.model_dump()
            errors: dict[str, str] = {}
            changes: dict[str, Any] = {}
            controller: Any = _MISSING
            for key, value in patch.items():
                if key == CONTROLLER_KEY:  # live, outside HubConfig
                    if value is None:
                        controller = ""
                    elif (
                        isinstance(value, int)
                        and not isinstance(value, bool)
                        and value > 0
                        and self.store.get_server(value) is not None
                    ):
                        controller = str(value)
                    else:
                        errors[key] = NO_SUCH_SERVER
                    continue
                if key not in OVERRIDES:
                    errors[str(key)[:64]] = "未知字段"
                    continue
                if key in SECRETS:
                    if isinstance(value, str) and value.strip() in ("", MASK):
                        continue  # keep the saved token
                    if value is None:
                        value = ""
                try:
                    changes[key] = _check_value(key, value)
                except ValueError as exc:
                    errors[key] = str(exc)
            if errors:
                raise Refused(errors)
            if "lan_access" in changes:
                if not changes["lan_access"]:
                    changes["bind_host"] = "127.0.0.1"
                elif bind_is_loopback(changes.get("bind_host", data["bind_host"])):
                    from taskpaw_v3.core import lan

                    primary = next(
                        (a.ip for a in lan.local_addresses() if a.primary), None
                    )
                    if primary is None:
                        raise Refused({"lan_access": "未找到本机局域网地址"})
                    changes["bind_host"] = primary
            merged = {**data, **changes}
            if (
                not bind_is_loopback(merged["bind_host"])
                and not str(merged["api_token"]).strip()
            ):
                raise Refused({"api_token": "开启局域网只读接口需要访问令牌"})
            try:
                validated = HubConfig(**merged)
                guard_bind_exposure(
                    validated.bind_host, validated.api_token, label="Hub API"
                )
            except (ValidationError, ValueError):
                raise Refused({"bind_host": "地址不可用"}) from None
            # #299 §10.1 / review CX1: every changed read-API address (also a
            # switch back to loopback) is bound once — one that cannot start
            # would leave no UI
            if (validated.bind_host, validated.bind_port) != (
                self.running.bind_host,
                self.running.bind_port,
            ):
                from taskpaw_v3.core import lan

                why = lan.probe_bind(validated.bind_host, validated.bind_port)
                if why:
                    raise Refused(
                        {"bind_host" if why == lan.NOT_LOCAL else "bind_port": why}
                    )
            writes = {
                key: _encode(key, getattr(validated, key))
                for key in changes
                if getattr(validated, key) != getattr(saved, key)
            }
            if controller != _MISSING:
                writes[CONTROLLER_KEY] = controller
            if writes:
                self.store.set_configs(writes)
            log.info("hub settings saved: %s", ", ".join(sorted(writes)) or "none")
            fields = self.pending()
            return {"ok": True, "restart_required": bool(fields), "fields": fields}


class Refused(Exception):
    def __init__(self, fields: dict[str, str]) -> None:
        super().__init__("设置有误")
        self.fields = fields


Sender = Callable[[str, str, dict, float], None]


def openclaw_test(
    url: str,
    token: str,
    *,
    send: Optional[Sender] = None,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = datetime.now,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """R14 测试发送: `{ok, at, elapsed_s}` or `{ok: false, error[, status]}`
    (`unreachable`, `token`, `timeout`, `http`). Never returns the token or
    exception text."""
    from taskpaw_v3.hub.server import openclaw
    from taskpaw_v3.hub.server.upstream_worker import UpstreamError

    sender = send or openclaw.send_payload
    started = clock()
    try:
        sender(url, token, {"text": "TaskPaw Hub 测试通知"}, timeout)
    except UpstreamError as exc:
        reason = exc.reason
        if reason in ("upstream_deadline", "socket_timeout"):
            error = "timeout"
        elif reason == "http_auth":
            error = "token"
        elif reason in (
            "http_refused",
            "http_incomplete",
            "http_encoding",
            "http_metadata_oversize",
            "body_oversize",
        ):
            error = "http"
        else:
            error = "unreachable"
            if reason not in (
                "network_unreachable",
                "delivery_busy",
                "helper_cancelled",
                "helper_cleanup_failed",
                "helper_failed",
                "request_invalid",
            ):
                reason = "upstream_failed"
        return {"ok": False, "error": error, "reason": reason}
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return {"ok": False, "error": "token", "status": exc.code}
        return {"ok": False, "error": "http", "status": exc.code}
    except (socket.timeout, TimeoutError):
        return {"ok": False, "error": "timeout"}
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            return {"ok": False, "error": "timeout"}
        return {"ok": False, "error": "unreachable"}
    except (OSError, ValueError):
        return {"ok": False, "error": "unreachable"}
    elapsed = max(0.0, clock() - started)
    return {"ok": True, "at": now().strftime("%H:%M"), "elapsed_s": round(elapsed, 1)}


def register_hub_settings(
    app: FastAPI,
    settings: HubSettings,
    *,
    boot_id: Optional[str],
    on_restart: Optional[Callable[[], None]],
    send: Optional[Sender] = None,
) -> None:
    def refusal(fields: dict[str, str]) -> JSONResponse:
        return JSONResponse(
            status_code=422, content={"detail": "设置有误", "fields": fields}
        )

    @app.get("/config/settings")
    def hub_settings() -> dict[str, Any]:
        return {**settings.view(), "restart_available": on_restart is not None}

    @app.put("/config/settings")
    async def hub_settings_put(request: Request) -> Any:
        try:
            body = await request.json()
        except ValueError:
            body = None
        try:
            return settings.update(body)
        except Refused as exc:
            return refusal(exc.fields)
        except ValueError as exc:  # operation_busy / restarting
            if str(exc) == "restarting":
                return JSONResponse(status_code=409, content=RESTARTING)
            return JSONResponse(
                status_code=409, content={"detail": BUSY_SAVE, "code": "operation_busy"}
            )
        except Exception:
            log.error("hub settings could not be saved")
            return JSONResponse(status_code=500, content={"detail": "保存失败"})

    @app.post("/config/openclaw/test")
    async def hub_openclaw_test(request: Request) -> Any:
        from starlette.concurrency import run_in_threadpool

        try:
            body = await request.json()
        except ValueError:
            body = None
        body = body if isinstance(body, dict) else {}
        saved = settings.saved()
        raw_url = body.get("url")
        url_value = saved.openclaw_url if raw_url in (None, "") else raw_url
        try:
            url = _check_url(url_value)
        except ValueError as exc:
            return refusal({"openclaw_url": str(exc)})
        token = body.get("token")
        if not isinstance(token, str) or token.strip() in ("", MASK):
            token = saved.openclaw_token
        elif _control(token):
            return refusal({"openclaw_token": "令牌格式不正确"})
        return await run_in_threadpool(openclaw_test, url, token.strip(), send=send)

    @app.post("/restart")
    async def hub_restart(request: Request) -> Any:
        try:
            body = await request.json()
        except ValueError:
            body = None
        asked = body.get("boot_id") if isinstance(body, dict) else None
        if on_restart is None:
            return JSONResponse(
                status_code=409,
                content={"detail": "请退出并重新打开 TaskPaw", "code": "unsupported"},
            )
        if not isinstance(asked, str) or asked != boot_id:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "TaskPaw 已重新启动过，请刷新",
                    "code": "stale_boot",
                },
            )
        try:
            with settings.admission():
                settings.restarting = True
        except ValueError as exc:
            if str(exc) == "restarting":
                return JSONResponse(status_code=409, content=RESTARTING)
            return JSONResponse(
                status_code=409, content={"detail": BUSY_SAVE, "code": "operation_busy"}
            )
        return JSONResponse(
            {"restarting": True}, status_code=202, background=BackgroundTask(on_restart)
        )
