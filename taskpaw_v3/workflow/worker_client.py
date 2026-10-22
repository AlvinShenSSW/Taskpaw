"""P2-2 (#293): a worker's HTTP client for its controller's worker API.

No proxy (`ProxyHandler({})`), no redirect (`NoRedirectHandler`), a timeout
on every call. The Bearer `<credential_id>.<secret>` travels only in the
request header: never in a URL, a log line or an error. `download_film()`
fetches the work copies into `<staging>/work/<relative_path>.part`, hashing
while writing (the prefix is re-hashed on resume), checks size and sha256
against the envelope and renames atomically; a mismatch deletes the part and
fetches the file once more. `upload_file()` streams one output from the
controller's received offset and re-sends it whole after a mismatch; only the
controller's `done` is success (IR2). `GET`/`POST outputs` may re-hash a large
part on the controller, so they get a long timeout (IR3).
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import SecretStr, TypeAdapter, ValidationError

from . import models as m
from .staging import Staging, StagingError

PREFIX = "/worker/v1"
TIMEOUT_SECONDS = 30.0
VERIFY_TIMEOUT_SECONDS = 1800.0  # outputs: the controller may hash 40 GB (IR3)
CHUNK = 4 << 20
BACKOFF_CAP_SECONDS = 30.0  # R8
_PATH = TypeAdapter(m.RelativePath)
_ID = TypeAdapter(m.Id)
_TRANSIENT = frozenset({0, 429, 500, 502, 503, 504, 507})
# a PUT that ended without `done`: GET outputs says where to go on
_RESUMABLE = frozenset(
    {"unreachable", "offset_mismatch", "prefix_unverified", "hash_mismatch"}
    | {"commit_pending", "busy"}
)


class ApiError(Exception):
    """A refused or failed call: HTTP status (0: unreachable) and code."""

    def __init__(
        self,
        status: int,
        code: str,
        *,
        retry_after: float | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{status} {code}")
        self.status, self.code, self.retry_after = status, code, retry_after
        self.body = dict(body or {})

    @property
    def transient(self) -> bool:
        return self.status in _TRANSIENT


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """A redirect is an error (it would carry the Bearer elsewhere)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def backoff(
    attempt: int, *, base: float = 1.0, cap: float = BACKOFF_CAP_SECONDS
) -> float:
    return min(cap, base * (2 ** min(max(0, attempt), 32)))  # CX1: no overflow


def host_text(host: str) -> str:
    try:
        return f"[{host}]" if ipaddress.ip_address(host).version == 6 else host
    except ValueError:
        return host


class WorkerClient:
    def __init__(
        self,
        host: str,
        port: int,
        credential: tuple[str, SecretStr] | None = None,
        *,
        timeout: float = TIMEOUT_SECONDS,
        verify_timeout: float = VERIFY_TIMEOUT_SECONDS,
    ) -> None:
        self.host, self.port = host, port
        self.base = f"http://{host_text(host)}:{port}{PREFIX}"
        self._credential = credential
        self.timeout, self.verify_timeout = timeout, verify_timeout
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirectHandler()
        )

    def __repr__(self) -> str:
        return f"WorkerClient({self.base!r})"

    # ── plumbing ─────────────────────────────────────────────────────────
    def _headers(self, auth: bool) -> dict[str, str]:
        if not auth:
            return {}
        if self._credential is None:
            raise ApiError(401, "unauthorized")
        credential_id, secret = self._credential
        return {"Authorization": f"Bearer {credential_id}.{secret.get_secret_value()}"}

    def open(
        self,
        method: str,
        path: str,
        *,
        data: Any = None,
        headers: Mapping[str, str] | None = None,
        auth: bool = True,
        timeout: float | None = None,
    ) -> Any:
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={**self._headers(auth), **(headers or {})},
        )
        try:
            return self._opener.open(request, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as exc:
            raise _api_error(exc) from None
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
            raise ApiError(0, "unreachable") from None

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        auth: bool = True,
        timeout: float | None = None,
    ) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"} if data is not None else {}
        with self.open(
            method, path, data=data, headers=headers, auth=auth, timeout=timeout
        ) as reply:
            return _read_json(reply, default=b"null")

    # ── enrollment and live state ────────────────────────────────────────
    def pair(
        self, pairing_id: str, ticket: SecretStr, credential_id: str, secret: SecretStr
    ) -> dict[str, Any]:
        return self.call(
            "POST",
            "/pair",
            {
                "pairing_id": pairing_id,
                "ticket": ticket.get_secret_value(),
                "credential_id": credential_id,
                "secret": secret.get_secret_value(),
            },
            auth=False,
        )

    def self_view(self) -> dict[str, Any]:
        return self.call("GET", "/self")

    def capabilities(self, probe: Mapping[str, Any]) -> dict[str, Any]:
        return self.call("POST", "/capabilities", dict(probe))

    def boot(
        self, boot_id: str, attempts: Sequence[Mapping[str, Any]] | None = None
    ) -> dict[str, Any]:
        """P2-3 §5: `attempts` reports the local attempt files."""
        return self.call(
            "POST",
            "/boot",
            {
                "boot_id": boot_id,
                "assignment_ids": [],
                "local_owners": [],
                "attempts": list(attempts or []),
            },
        )

    # ── remote execution (P2-3) ──────────────────────────────────────────
    def claim(self, body: Mapping[str, Any]) -> dict[str, Any]:
        return self.call("POST", "/claim", dict(body))

    def renew(self, attempt_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        return self.call("POST", f"/attempts/{attempt_id}/renew", dict(body))

    def finish(self, attempt_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        return self.call(
            "POST",
            f"/attempts/{attempt_id}/finish",
            dict(body),
            timeout=self.verify_timeout,  # the refs are checked first
        )

    def attempt(self, attempt_id: str) -> dict[str, Any]:
        return self.call("GET", f"/attempts/{attempt_id}")

    def heartbeat(self, beat: Mapping[str, Any]) -> dict[str, Any]:
        return self.call("POST", "/heartbeat", dict(beat))

    def envelope(self, film_id: str, revision: int) -> dict[str, Any]:
        return self.call("GET", f"/films/{film_id}/{revision}")

    def plan_outputs(
        self, attempt_id: str, files: list[Mapping[str, Any]]
    ) -> dict[str, Any]:
        return self.call(
            "POST",
            f"/attempts/{attempt_id}/outputs",
            list(files),
            timeout=self.verify_timeout,
        )

    def outputs(self, attempt_id: str) -> dict[str, Any]:
        """The plan's state; the controller settles and re-hashes parts first."""
        return self.call(
            "GET", f"/attempts/{attempt_id}/outputs", timeout=self.verify_timeout
        )

    # ── download (R12) ───────────────────────────────────────────────────
    def download_film(
        self,
        envelope: Mapping[str, Any],
        staging: Staging,
        *,
        cancel: threading.Event | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[Path]:
        """Every work copy of the envelope, verified, under `<staging>/work`."""
        items = [_item(staging, value) for value in envelope["workcopy"]]
        total = sum(item[3] for item in items)
        present = sum(_present(final) for _, final, _, _, _ in items)
        staging.admit(total, present)
        done, paths = 0, []
        for item_id, final, _, size, sha in items:
            self._download_item(
                envelope, item_id, final, size, sha, cancel, on_progress, done, total
            )
            done += size
            paths.append(final)
        return paths

    def _download_item(
        self,
        envelope: Mapping[str, Any],
        item_id: str,
        final: Path,
        size: int,
        sha: str,
        cancel: threading.Event | None,
        on_progress: Callable[[int, int], None] | None,
        before: int,
        total: int,
    ) -> None:
        if final.is_file():
            if final.stat().st_size == size and _sha_file(final) == sha:
                return
            final.unlink()
        part = final.with_name(final.name + ".part")
        final.parent.mkdir(parents=True, exist_ok=True)
        url = f"/films/{envelope['film']}/{envelope['rev']}/items/{item_id}"
        for _ in range(2):  # one re-fetch after a whole-file mismatch
            offset = part.stat().st_size if part.is_file() else 0
            if offset > size:
                part.unlink()
                offset = 0
            digest = _hash_prefix(part, offset)
            if offset < size:
                headers = {"Range": f"bytes={offset}-"} if offset else {}
                with self.open(
                    "GET", url, headers=headers, timeout=self.timeout
                ) as reply:
                    if (
                        reply.headers.get("X-Sha256") != sha
                        or reply.headers.get("X-Size") != str(size)
                        or reply.status != (206 if offset else 200)
                    ):
                        raise ApiError(409, "item_changed")
                    with part.open("ab") as stream:
                        while True:
                            if cancel is not None and cancel.is_set():
                                raise ApiError(0, "cancelled")
                            try:
                                chunk = reply.read(CHUNK)
                            except (http.client.HTTPException, OSError):
                                raise ApiError(0, "unreachable") from None  # CX7
                            if not chunk:
                                break
                            stream.write(chunk)
                            digest.update(chunk)
                            offset += len(chunk)
                            if on_progress is not None:
                                on_progress(before + offset, total)
                        stream.flush()
                        os.fsync(stream.fileno())
                if offset < size:  # CX7: the reply ended early; the part stays
                    raise ApiError(0, "unreachable")
            if size == 0:
                part.touch()  # CX5: an empty work copy has nothing to fetch
            if offset == size and digest.hexdigest() == sha:
                os.replace(part, final)
                return
            part.unlink(missing_ok=True)  # a whole-file mismatch: start again
        raise ApiError(422, "hash_mismatch")

    # ── upload (R13) ─────────────────────────────────────────────────────
    def upload_file(
        self,
        attempt_id: str,
        path: str,
        local: Path,
        *,
        size: int,
        sha256: str,
        attempts: int = 3,
        on_progress: Callable[[int], None] | None = None,
    ) -> dict[str, Any]:
        """One output from the controller's received offset to the end.

        Success is the controller's `done` only: a file fully received but not
        committed is 503 `commit_pending` (transient), never done (IR2). After
        any cut, refusal or reset the next offset comes from GET outputs, which
        also reads the real refusal when the server answered before reading
        the body (IR12) and rebuilds the hash state of a part (IR3)."""
        entry = self._entry(attempt_id, path)
        url = f"/attempts/{attempt_id}/files/{quote(path, safe='/')}"
        last: ApiError | None = None
        for _ in range(attempts):
            if entry["done"]:
                return entry
            received = int(entry["received"])
            if received >= size:  # GET just tried the commit again
                raise ApiError(503, "commit_pending")
            headers = {
                "Content-Range": f"bytes {received}-{size - 1}/{size}",
                "Content-Length": str(size - received),
                "Content-Type": "application/octet-stream",
            }
            try:
                with self.open(
                    "PUT",
                    url,
                    data=_blocks(local, received, on_progress),
                    headers=headers,
                ) as reply:
                    result = _read_json(reply)
                if isinstance(result, dict) and result.get("done") is True:
                    return result
                last = ApiError(0, "unreachable")
            except ApiError as exc:
                if exc.code not in _RESUMABLE:
                    raise
                last = exc  # S1: after a hash_mismatch the plan says received 0
            entry = self._entry(attempt_id, path)
        if entry["done"]:
            return entry
        raise last or ApiError(0, "unreachable")

    def _entry(self, attempt_id: str, path: str) -> dict[str, Any]:
        entry = next(
            (f for f in self.outputs(attempt_id)["files"] if f["path"] == path), None
        )
        if entry is None:
            raise ApiError(404, "not_found")
        return dict(entry)


def _read_json(reply: Any, *, default: bytes = b"") -> Any:
    """A reply body as JSON; a cut or partial body is a transient failure
    (CX6), so a lost-reply recovery such as /self still runs."""
    try:
        return json.loads(reply.read() or default)
    except (http.client.HTTPException, OSError, ValueError):
        raise ApiError(0, "unreachable") from None


def _api_error(exc: urllib.error.HTTPError) -> ApiError:
    try:
        body = json.loads(exc.read() or b"{}")
    except (http.client.HTTPException, OSError, ValueError):
        body = {}  # CX12: a cut error body keeps the status (and its class)
    if not isinstance(body, dict):
        body = {}
    retry = exc.headers.get("Retry-After") if exc.headers else None
    try:
        retry_after = float(retry) if retry else None
    except ValueError:
        retry_after = None
    found = body.get("code")
    code = found if isinstance(found, str) else "http_error"
    return ApiError(exc.code, code, retry_after=retry_after, body=body)


def _item(
    staging: Staging, value: Mapping[str, Any]
) -> tuple[str, Path, str, int, str]:
    """A work copy from the envelope; its path must stay under staging/work."""
    try:
        relative = _PATH.validate_python(value["relative_path"])
        item_id = _ID.validate_python(value["item_id"])
    except (ValidationError, KeyError, TypeError):
        raise ApiError(422, "invalid_envelope") from None
    size, sha = value.get("size"), value.get("sha256")
    if type(size) is not int or size < 0 or not isinstance(sha, str) or len(sha) != 64:
        raise ApiError(422, "invalid_envelope")
    root = staging.work_dir.resolve()
    final = staging.work_dir / relative
    if root not in final.resolve().parents:
        raise ApiError(422, "invalid_envelope")
    return item_id, final, relative, size, sha


def _present(final: Path) -> int:
    total = 0
    for path in (final, final.with_name(final.name + ".part")):
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


def _hash_prefix(path: Path, size: int) -> Any:
    digest = hashlib.sha256()
    if not size:
        return digest
    with path.open("rb") as stream:
        left = size
        while left:
            chunk = stream.read(min(CHUNK, left))
            if not chunk:
                break
            digest.update(chunk)
            left -= len(chunk)
    return digest


def _sha_file(path: Path) -> str:
    return _hash_prefix(path, path.stat().st_size).hexdigest()


def _blocks(
    path: Path, offset: int, on_progress: Callable[[int], None] | None = None
) -> Iterator[bytes]:
    with path.open("rb") as stream:
        stream.seek(offset)
        while True:
            chunk = stream.read(CHUNK)
            if not chunk:
                return
            offset += len(chunk)
            yield chunk
            if on_progress is not None:
                on_progress(offset)


__all__ = [
    "ApiError",
    "NoRedirectHandler",
    "StagingError",
    "WorkerClient",
    "backoff",
]
