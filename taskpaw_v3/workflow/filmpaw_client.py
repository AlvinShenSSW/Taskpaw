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
                return json.loads(response.read() or b"null")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read() or b"{}").get("detail", "")
            except ValueError:
                detail = ""
            raise FilmpawRefused(e.code, str(detail) or e.reason) from None
        except (OSError, ValueError) as e:  # URLError, timeouts, resets, bad JSON
            raise FilmpawUnavailable(f"Filmpaw 无响应：{e}") from e

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
