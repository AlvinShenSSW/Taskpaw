"""Talk to the local Filmpaw sidecar that owns the NAS library (F02-P1 §3.2/§3.3).

Filmpaw writes ``workflow-endpoint.json`` (port, pid, session token) next to its library DB while it
runs. Everything here is a bounded local HTTP call; any way of not reaching Filmpaw is
``FilmpawUnavailable`` so the caller simply waits for the next round.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ENDPOINT_FILE = "workflow-endpoint.json"
TIMEOUT_SECONDS = 30.0


class FilmpawUnavailable(Exception):
    """Filmpaw is not running, or did not answer."""


class FilmpawRefused(Exception):
    """Filmpaw answered with an error (status code and its message)."""

    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


def default_data_dir() -> Path:
    return Path(os.environ.get("APPDATA") or Path.home()) / "Filmpaw"


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(
            0x1000, False, pid
        )  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass(frozen=True)
class Endpoint:
    port: int
    token: str


def read_endpoint(data_dir: Path) -> Endpoint:
    try:
        data = json.loads((data_dir / ENDPOINT_FILE).read_text(encoding="utf-8"))
        port, pid, token = int(data["port"]), int(data["pid"]), str(data["token"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise FilmpawUnavailable(f"Filmpaw 未运行（{data_dir / ENDPOINT_FILE}）") from e
    if not _pid_alive(pid):
        raise FilmpawUnavailable("Filmpaw 未运行（端点文件已过期）")
    return Endpoint(port=port, token=token)


class FilmpawClient:
    def __init__(self, data_dir: Path | None = None, timeout: float = TIMEOUT_SECONDS):
        self.data_dir = data_dir or default_data_dir()
        self.timeout = timeout

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        data, _ = self._request(method, path, body)
        try:
            return json.loads(data or b"null")
        except ValueError as e:
            raise FilmpawUnavailable(f"Filmpaw 无响应：{e}") from e

    def _request(self, method: str, path: str, body: Any = None) -> tuple[bytes, str]:
        """The raw response body and its media type."""
        endpoint = read_endpoint(self.data_dir)
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{endpoint.port}{path}",
            data=data,
            method=method,
            headers={
                "X-Filmpaw-Session": endpoint.token,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read(), response.headers.get_content_type()
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read() or b"{}").get("detail", "")
            except ValueError:
                detail = ""
            raise FilmpawRefused(e.code, str(detail) or e.reason) from None
        except (OSError, ValueError) as e:  # URLError, timeouts, resets
            raise FilmpawUnavailable(f"Filmpaw 无响应：{e}") from e

    def performers(self, q: str) -> list[dict[str, Any]]:
        """Present performers matching a name or alias, for the operator to pick (T06-P1 D08)."""
        query = urllib.parse.urlencode(
            {"q": q, "include_missing": "false", "page_size": 50}
        )
        data = self._call("GET", f"/api/performers?{query}")
        try:
            return [
                {
                    "id": str(item["id"]),
                    "name": str(item["name"]),
                    "source_label": item.get("source_label"),
                    "unc_path": str(item["unc_path"]),
                    "has_thumb": bool(item.get("has_thumb")),
                    "aliases": [str(a["alias"]) for a in item.get("aliases") or ()],
                }
                for item in data["items"]
            ]
        except (KeyError, TypeError, AttributeError) as e:
            raise FilmpawUnavailable(f"Filmpaw 返回的演员列表无效：{e}") from e

    def thumb(self, performer_id: str) -> tuple[bytes, str]:
        """A performer's avatar bytes and media type; FilmpawRefused 404 when it has none."""
        quoted = urllib.parse.quote(performer_id, safe="")
        return self._request("GET", f"/api/performers/{quoted}/thumb")

    def target(self, performer_id: str, film_dir: str) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            {"performer_id": performer_id, "film_dir": film_dir}
        )
        return self._call("GET", f"/api/workflow/archive/target?{query}")

    def archive(
        self,
        operation_id: str,
        package_dir: Path,
        performer_id: str,
        expected_target: dict[str, Any],
        files: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self._call(
            "POST",
            "/api/workflow/archive",
            {
                "operation_id": operation_id,
                "package_dir": str(package_dir),
                "performer_id": performer_id,
                "expected_target": expected_target,
                "files": files,
            },
        )

    def lookup(self, operation_id: str) -> dict[str, Any] | None:
        """The archive task for this operation key; None when Filmpaw never accepted it."""
        try:
            return self._call(
                "GET",
                f"/api/workflow/archive/{urllib.parse.quote(operation_id, safe='')}",
            )
        except FilmpawRefused as e:
            if e.status == 404:
                return None
            raise
