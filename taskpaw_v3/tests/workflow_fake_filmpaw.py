"""In-process stand-in for Filmpaw's workflow archive routes (F02-P1 §3.2), for tests only.

Same wire contract as Filmpaw ``routes_workflow.py``: target proof, POST copies the package into a new
folder under the performer directory (never overwriting), lookup by operation key. Copies are
synchronous. ``mode`` injects failures: "lose_response" (do the work, then drop the connection once),
"busy" (409 without creating a task), "fail" (task ends failed, nothing copied).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

TOKEN = "f" * 64


def _identity(path: Path) -> list[int]:
    st = os.stat(path)
    return [st.st_dev, st.st_ino]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FakeFilmpaw:
    def __init__(self, data_dir: Path, performers: dict[str, Path]):
        self.data_dir = data_dir
        self.performers = performers
        self.tasks: dict[str, dict[str, Any]] = {}
        self.mode = ""
        self.copies = 0
        self.posts = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, status: int, body: Any) -> None:
                data = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authorised(self) -> bool:
                if self.headers.get("X-Filmpaw-Session") != TOKEN:
                    self._send(403, {"detail": "会话凭据无效"})
                    return False
                return True

            def do_GET(self) -> None:
                if not self._authorised():
                    return
                url = urlparse(self.path)
                if url.path == "/api/workflow/archive/target":
                    q = {k: v[0] for k, v in parse_qs(url.query).items()}
                    status, body = fake.target(q["performer_id"], q["film_dir"])
                else:
                    task = fake.tasks.get(url.path.rsplit("/", 1)[1])
                    status, body = (
                        (200, task) if task else (404, {"detail": "归档操作不存在"})
                    )
                self._send(status, body)

            def do_POST(self) -> None:
                if not self._authorised():
                    return
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.posts += 1
                if fake.mode == "busy":
                    self._send(409, {"detail": "正在核对上次中断的传输，请稍候"})
                    return
                status, task = fake.archive(body)
                if fake.mode == "lose_response":
                    fake.mode = ""
                    self.close_connection = True
                    self.connection.shutdown(2)
                    return
                self._send(status, task)

        self._handler = Handler

    def __enter__(self) -> FakeFilmpaw:
        """Start (again): a restarted Filmpaw keeps its tasks but gets a new port."""
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "workflow-endpoint.json").write_text(
            json.dumps(
                {
                    "port": self.server.server_address[1],
                    "pid": os.getpid(),
                    "token": TOKEN,
                }
            ),
            encoding="utf-8",
        )
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        (self.data_dir / "workflow-endpoint.json").unlink(missing_ok=True)

    # ── behaviour ───────────────────────────────────────────────────────
    def target(self, performer_id: str, film_dir: str) -> tuple[int, dict[str, Any]]:
        target = self.performers.get(performer_id)
        if target is None:
            return 404, {"detail": "所选演员记录已不存在"}
        present = {p.name.casefold() for p in target.iterdir()}
        return 200, {
            "performer_id": performer_id,
            "absent": film_dir.casefold() not in present,
            "expected_target": {
                "target_path": str(target),
                "directory_identity": _identity(target),
            },
        }

    def archive(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        op = body["operation_id"]
        if op in self.tasks:
            return 202, self.tasks[op]
        target = self.performers[body["performer_id"]]
        if body["expected_target"] != {
            "target_path": str(target),
            "directory_identity": _identity(target),
        }:
            return 409, {"detail": "NAS 演员目录已变化，请重新确认入库"}
        package = Path(body["package_dir"])
        task: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "request_id": op,
            "status": "completed",
            "message": "",
        }
        expected = {f["path"]: (f["size"], f["sha256"]) for f in body["files"]}
        actual = {
            p.relative_to(package).as_posix(): p
            for p in package.rglob("*")
            if p.is_file()
        }
        dest = target / package.name
        if self.mode == "fail":
            task.update(status="failed", message="NAS 可用空间不足，整批未开始")
        elif dest.exists():
            task.update(
                status="failed", message="存在同名目标", conflicts=[package.name]
            )
        elif actual.keys() != expected.keys() or any(
            (p.stat().st_size, _sha(p)) != expected[k] for k, p in actual.items()
        ):
            task.update(status="failed", message="影片包内容与已批准清单不一致")
        else:
            shutil.copytree(package, dest)
            self.copies += 1
            task["receipt"] = {
                "task_id": task["id"],
                "target_path": str(dest),
                "directory_identity": _identity(dest),
                "files": sorted(
                    (
                        {"path": k, "size": p.stat().st_size, "sha256": _sha(p)}
                        for k, p in actual.items()
                    ),
                    key=lambda f: f["path"],
                ),
            }
        self.tasks[op] = task
        return 202, task
