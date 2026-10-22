"""U1 L: the read-only MDCX metadata preview of a film before it is planned.

Raw INBOX downloads carry no NFO or cover, so the 待处理 card asks Miko once per
(film, input revision): MDCX304's `python -m mdcx.cmd.lookup_one` (contract
`lookup_one/1`, MDCX304#298) runs off the dispatcher thread in its own worker,
with a bounded timeout, no GPU, stdin closed and no console window.

Profile isolation (v2.1): each run gets a fresh config directory under the work
dir, built as the M01 scrape attempt builds its own — the operator's profile
JSON copied in (here as `config.json`, the name lookup_one loads without a
mark) plus its userdata, and no `MDCx.config` mark. Whatever Miko adds there is
discarded with the run. The result and the poster are cached under
`<work>/lookup/<film>-r<revision>/`; failures are cached too (manual 重新查询).
A run cut short by an app stop caches nothing: the next start runs it again.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from . import models as m
from .owned_process import OwnedJob
from .scrape_step import _userdata_files
from .stage_results import main_videos

if TYPE_CHECKING:
    from .local_runtime import LocalRuntime

log = logging.getLogger("taskpaw.workflow.lookup")

CLI_VERSION = "lookup_one/1"
SCHEMA = 1
# lookup_one's own budget (MDCX304 8a3f519): 240 s metadata + 30 s poster. The
# wall-clock kill below comes after it, so a slow site reports 资料查询超时.
MDCX_BUDGET_SECONDS = 270.0
TIMEOUT_SECONDS = 300.0
POLL_SECONDS = 5.0
RECORD_NAME = "lookup.json"
POSTER_NAME = "poster.jpg"
CONFIG_NAME = "config.json"
# lookup_one's own messages the card keys on (MDCX304 mdcx/cmd/lookup_one.py)
NO_NUMBER = "文件名中没有番号"
NOT_FOUND = "未查到资料"
TIMED_OUT = "资料查询超时"
NO_VIDEO = "没有主视频"
_FIELDS = ("number", "title", "studio", "release", "runtime_min")


class _Stopped(Exception):
    """The worker was stopped mid-lookup: no result, the film stays queued."""


@dataclass(frozen=True)
class LookupResult:
    """One cached lookup of a (film, input revision)."""

    film_id: str
    input_revision: int
    file_name: str
    status: str  # succeeded | failed
    number: str | None
    title: str | None
    actors: tuple[str, ...]
    studio: str | None
    release: str | None
    runtime_min: int | None
    poster: Path | None  # the cached cover, when there is one
    poster_sha: str | None
    error_class: str | None
    message: str
    finished_at: str

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"

    @property
    def can_requery(self) -> bool:
        """A name without a number stays one: 重新查询 cannot help (v2.1)."""
        return not (self.status == "failed" and self.message.startswith(NO_NUMBER))

    def view(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "number": self.number,
            "title": self.title,
            "actors": list(self.actors),
            "studio": self.studio,
            "release": self.release,
            "runtime_min": self.runtime_min,
            "has_poster": self.poster is not None,
            "error_class": self.error_class,
            "message": self.message or None,
            "can_requery": self.can_requery,
        }


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _sha16(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def main_file_name(manifest: m.InputManifest) -> str | None:
    """The file name lookup_one parses the number from: the first main video's."""
    videos = main_videos(manifest)
    return PurePosixPath(videos[0].relative_path).name if videos else None


class MetadataLookup:
    """The lookup worker and its on-disk cache (one per workflow host)."""

    def __init__(
        self,
        runtime: LocalRuntime,
        *,
        python: str,
        root: str,
        profile: str,
        userdata: str | None = None,
        launch_prefix: Sequence[str] = (),
        timeout: float = TIMEOUT_SECONDS,
        poll: float = POLL_SECONDS,
    ) -> None:
        """`launch_prefix` replaces `python -m mdcx.cmd.lookup_one` in tests."""
        self.runtime = runtime
        self.python, self.root, self.profile = python, Path(root), Path(profile)
        self.userdata = Path(userdata) if userdata else self.profile.parent / "userdata"
        self.launch_prefix = list(launch_prefix)
        self.timeout, self.poll = timeout, poll
        self.cancel = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._records: dict[tuple[str, int], LookupResult | None] = {}
        self._running: tuple[str, int] | None = None

    # ── lifecycle ───────────────────────────────────────────────────────
    @property
    def cache_dir(self) -> Path:
        return self.runtime.work_dir / "lookup"

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name="workflow-lookup", daemon=True
        )
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self, timeout: float = 30.0) -> bool:
        self.cancel.set()
        self._wake.set()
        if self._thread is None:
            return True
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    def _loop(self) -> None:
        while not self.cancel.is_set():
            try:
                worked = self.run_once()
            except Exception:
                log.exception("workflow lookup tick failed")
                worked = False
            if not worked:
                self._wake.wait(self.poll)
                self._wake.clear()

    # ── cache ───────────────────────────────────────────────────────────
    def _dir(self, film_id: str, revision: int) -> Path:
        return self.cache_dir / f"{film_id}-r{revision}"

    def result(self, film_id: str, revision: int | None) -> LookupResult | None:
        """The cached lookup of a (film, revision); None before it ran."""
        if revision is None:
            return None
        key = (film_id, revision)
        with self._lock:
            if key in self._records:
                return self._records[key]
        value = self._read(film_id, revision)
        with self._lock:
            self._records.setdefault(key, value)
            return self._records[key]

    def state(self, film_id: str, revision: int) -> dict[str, Any]:
        """The 待处理 card's lookup block: the result, or queued / running."""
        found = self.result(film_id, revision)
        if found is not None:
            return found.view()
        with self._lock:
            running = self._running == (film_id, revision)
        return {
            "status": "running" if running else "queued",
            "number": None,
            "title": None,
            "actors": [],
            "studio": None,
            "release": None,
            "runtime_min": None,
            "has_poster": False,
            "error_class": None,
            "message": None,
            "can_requery": False,
        }

    def _read(self, film_id: str, revision: int) -> LookupResult | None:
        base = self._dir(film_id, revision)
        try:
            data = json.loads((base / RECORD_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            return None
        poster = base / POSTER_NAME
        has_poster = bool(data.get("poster")) and poster.is_file()
        return LookupResult(
            film_id=film_id,
            input_revision=revision,
            file_name=str(data.get("file_name") or ""),
            status="succeeded" if data.get("status") == "succeeded" else "failed",
            number=_text(data.get("number")),
            title=_text(data.get("title")),
            actors=tuple(
                a.strip()
                for a in data.get("actors") or ()
                if isinstance(a, str) and a.strip()
            ),
            studio=_text(data.get("studio")),
            release=_text(data.get("release")),
            runtime_min=data.get("runtime_min")
            if type(data.get("runtime_min")) is int
            else None,
            poster=poster if has_poster else None,
            poster_sha=str(data.get("poster_sha") or "") or None
            if has_poster
            else None,
            error_class=_text(data.get("error_class")),
            message=str(data.get("message") or ""),
            finished_at=str(data.get("finished_at") or ""),
        )

    def _write(self, film_id: str, revision: int, record: dict[str, Any]) -> None:
        base = self._dir(film_id, revision)
        base.mkdir(parents=True, exist_ok=True)
        tmp = base / f".{RECORD_NAME}.tmp"
        tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, base / RECORD_NAME)
        value = self._read(film_id, revision)
        with self._lock:
            self._records[(film_id, revision)] = value

    def requery(self, film_id: str, revision: int) -> None:
        """重新查询: forget the cached result so the worker runs it again.

        WorkflowError `precondition_failed` while it runs or when the name has no
        number (it cannot change within an input revision)."""
        current = self.result(film_id, revision)
        with self._lock:
            if self._running == (film_id, revision):
                raise m.WorkflowError("precondition_failed")
        if current is not None and not current.can_requery:
            raise m.WorkflowError("precondition_failed")
        try:
            (self._dir(film_id, revision) / RECORD_NAME).unlink()
        except FileNotFoundError:
            pass
        with self._lock:
            self._records[(film_id, revision)] = None
        self.wake()

    # ── work ────────────────────────────────────────────────────────────
    def _candidates(self) -> list[tuple[str, int]]:
        """待处理 films first (their cards wait for it), then the films already
        planned before their lookup ran that Miko has not named yet."""
        rt = self.runtime
        keys = [
            (film.film_id, film.input_revision)
            for film in rt.store.list_unplanned(rt.operator)
        ]
        keys += [
            (film.film_id, film.input_revision)
            for film in rt.store.film_board(rt.operator)
            if film.input_revision is not None and film.final_revision is None
        ]
        return list(dict.fromkeys(keys))

    def run_once(self) -> bool:
        """Look up the next film without a cached result; True if one ran."""
        rt = self.runtime
        for film_id, revision in self._candidates():
            if self.cancel.is_set():
                return False
            if self.result(film_id, revision) is not None:
                continue
            manifest = rt.store.get_input_manifest(rt.operator, film_id, revision)
            self.run(film_id, revision, main_file_name(manifest))
            return True
        return False

    def run(self, film_id: str, revision: int, file_name: str | None) -> None:
        with self._lock:
            self._running = (film_id, revision)
        try:
            record = self._lookup(film_id, revision, file_name)
        except _Stopped:
            return  # an app shutdown is not a failed lookup: rerun on next start
        except Exception as exc:  # never a stuck card: the failure is the result
            log.exception("workflow lookup of %s failed", film_id)
            record = _failure("needs_attention", f"资料查询异常：{exc}")
        finally:
            with self._lock:
                self._running = None
        record.update(
            schema=SCHEMA,
            cli_version=CLI_VERSION,
            film_id=film_id,
            input_revision=revision,
            file_name=file_name or "",
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        self._write(film_id, revision, record)

    def _lookup(
        self, film_id: str, revision: int, file_name: str | None
    ) -> dict[str, Any]:
        if not file_name:
            return _failure("needs_attention", NO_VIDEO)
        if not self.profile.is_file():
            return _failure("needs_attention", f"找不到 Miko 配置：{self.profile}")
        base = self._dir(film_id, revision)
        run = base / f"run-{uuid.uuid4().hex}"
        try:
            config, out = run / "config", run / "out"
            out.mkdir(parents=True)
            self._copy_config(config)
            request, result = run / "request.json", run / "result.json"
            request.write_text(
                json.dumps(
                    {"schema": SCHEMA, "file_name": file_name, "out_dir": str(out)},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            code = self._launch(run, config, request, result)
            if code is None:
                return _failure("transient", TIMED_OUT)
            data = self._parse(result, code)
            if data.get("status") != "succeeded":
                return data
            poster = out / POSTER_NAME
            target = base / POSTER_NAME
            if data.get("poster") == POSTER_NAME and poster.is_file():
                os.replace(poster, target)
                data["poster"] = POSTER_NAME
                data["poster_sha"] = _sha16(target)
            else:
                target.unlink(missing_ok=True)
                data["poster"] = None
            return data
        finally:
            shutil.rmtree(run, ignore_errors=True)

    def _copy_config(self, config: Path) -> None:
        """M01's config copy: the profile and its userdata, no `MDCx.config` mark."""
        config.mkdir(parents=True)
        shutil.copyfile(self.profile, config / CONFIG_NAME)
        for path in _userdata_files(self.userdata):
            dest = config / "userdata" / path.relative_to(self.userdata)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)

    def _launch(
        self, run: Path, config: Path, request: Path, result: Path
    ) -> int | None:
        """The exit code; None when the run timed out. `_Stopped` on cancel."""
        command = self.launch_prefix or [self.python, "-m", "mdcx.cmd.lookup_one"]
        env = {**os.environ, "MDCX_CONFIG_DIR": str(config)}
        processes = OwnedJob()
        try:
            child = processes.launch(
                [*command, "--request", str(request), "--result", str(result)],
                cwd=str(self.root),
                env=env,
                on_line=lambda _, text: None,
            )
            deadline = time.monotonic() + self.timeout
            while (code := child.poll()) is None:
                if self.cancel.is_set():
                    raise _Stopped
                if time.monotonic() >= deadline:
                    return None
                self.cancel.wait(0.1)
            for worker in child.threads:
                worker.join(timeout=5)
            return code
        finally:
            processes.terminate()
            processes.settle(10.0)

    @staticmethod
    def _parse(result: Path, code: int) -> dict[str, Any]:
        try:
            data = json.loads(result.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _failure("transient", f"资料查询没有返回结果（退出码 {code}）")
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            return _failure("needs_attention", "资料查询结果版本不符")
        if data.get("status") != "succeeded":
            error = data.get("error_class")
            return _failure(
                error
                if error in ("transient", "needs_attention")
                else "needs_attention",
                str(data.get("message") or NOT_FOUND),
            )
        value: dict[str, Any] = {
            "status": "succeeded",
            "error_class": None,
            "message": "",
            "poster": data.get("poster"),
            "actors": [a for a in data.get("actors") or () if isinstance(a, str)],
        }
        for name in _FIELDS:
            value[name] = data.get(name)
        return value


def _failure(error_class: str, message: str) -> dict[str, Any]:
    return {
        "status": "failed",
        "error_class": error_class,
        "message": message,
        "poster": None,
        "actors": [],
        **{name: None for name in _FIELDS},
    }
