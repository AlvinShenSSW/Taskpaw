"""Fixed Jasna 0.11.3 encrypted model downloader; no license/decryption handling.

Run out of process: network/DNS and large file writes must never hold up Stop.
The manifest is pinned to Kruk2/ltx-restore; stdin cannot select arbitrary URLs.
#283/#285: the three 0.11.3 quality tiers TaskPaw offers (standard/good/high),
each in an int8 and an nvfp4 (fast) file, plus the two shared VAE files.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable

REVISION = "a6e596656f9374dd85536a9313c9a5511cbdd407"
# #283: (ltx_model tier, fast) → its model file; fast = nvfp4 (--ltx-fast), else int8.
MODEL_FILES = {
    ("standard", False): "ltx-restore-alpha1-distill2-int8.safetensors.enc",
    ("standard", True): "ltx-restore-alpha1-distill2-nvfp4.safetensors.enc",
    ("good", False): "ltx-restore-alpha1-distill4-int8.safetensors.enc",
    ("good", True): "ltx-restore-alpha1-distill4-nvfp4.safetensors.enc",
    ("high", False): "ltx-restore-alpha1-distill8-int8.safetensors.enc",
    ("high", True): "ltx-restore-alpha1-distill8-nvfp4.safetensors.enc",
}
SHARED_FILES = ("vae.safetensors.enc", "vae-decoder.safetensors.enc")
FILES = {
    MODEL_FILES["standard", False]: (
        13368048749,
        "c23559f9d120df49266fc2dbe60ec384058d51834cf1a2da049e54b28aea51ad",
    ),
    MODEL_FILES["standard", True]: (
        7733294997,
        "4aa08c146c8cc335e7f90d19aa293cc0dfe4f14520fd85efc257fe809510693b",
    ),
    MODEL_FILES["good", False]: (
        13368048733,
        "1c0f1642232813f690d949b05fe90bdc02d3203e0092950de25e9cc961f30965",
    ),
    MODEL_FILES["good", True]: (
        7733294981,
        "57098e622d02598467f903049f4d1a613852f832847a01c43da148f506876973",
    ),
    MODEL_FILES["high", False]: (
        13368048773,
        "b006342a109c640a46ae91a5cd10e67186d968c63ef2f715c78bedf36936e057",
    ),
    MODEL_FILES["high", True]: (
        7733295021,
        "54f2f4f6f7d3f5d60e18aca7bf6137da206a2b66f282e1a9078743c3173e62e2",
    ),
    SHARED_FILES[0]: (
        1472223815,
        "b9252d5a799748d1932d4eed60a2d77461949a19f6feb57f2238dbffe08f5e25",
    ),
    SHARED_FILES[1]: (
        38718609,
        "c60c3572871203bd364d6bb2390225fa4c347f7ea5c64fe468256c900caeea2d",
    ),
}


def worker_argv() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "jasna-download"]
    return [sys.executable, "-m", __name__]


def _publish(temp: Path, target: Path) -> None:
    """Atomic no-clobber publication on both platforms; race means reuse."""
    try:
        if os.name == "nt":
            os.rename(temp, target)  # Windows rename refuses existing targets.
        else:
            os.link(temp, target)
    except FileExistsError:
        if not target.is_file():
            raise


def download(destination: Path, names: list[str], emit: Callable[[dict], None]) -> None:
    if not names or len(names) != len(set(names)) or any(n not in FILES for n in names):
        raise ValueError("unknown or duplicate model files")
    destination.mkdir(parents=True, exist_ok=True)
    total = sum(FILES[n][0] for n in names)
    done = 0
    for name in names:
        size, expected = FILES[name]
        target = destination / name
        if target.is_file():
            done += size
            continue
        for host in ("huggingface.co", "hf-mirror.com"):
            temp: Path | None = None
            try:
                digest = hashlib.sha256()
                received = 0
                last = 0.0
                url = f"https://{host}/Kruk2/ltx-restore/resolve/{REVISION}/{name}"
                with tempfile.NamedTemporaryFile(
                    dir=destination, prefix=f".{name}.", suffix=".part", delete=False
                ) as output:
                    temp = Path(output.name)
                    with urllib.request.urlopen(url, timeout=30) as response:
                        while chunk := response.read(1024 * 1024):
                            received += len(chunk)
                            if received > size:
                                raise OSError("model exceeds expected size")
                            output.write(chunk)
                            digest.update(chunk)
                            now = time.monotonic()
                            if now - last >= 0.5:
                                emit(
                                    dict(
                                        state="downloading",
                                        file=name,
                                        downloaded_bytes=done + received,
                                        total_bytes=total,
                                    )
                                )
                                last = now
                    if received != size or digest.hexdigest() != expected:
                        raise OSError("model size or SHA256 mismatch")
                    output.flush()
                    os.fsync(output.fileno())
                _publish(temp, target)
                break
            except (OSError, http.client.HTTPException):
                if host == "hf-mirror.com":
                    raise
            finally:
                if temp is not None:
                    temp.unlink(missing_ok=True)
        done += size
    emit(dict(state="complete", downloaded_bytes=total, total_bytes=total))


def main() -> int:
    line = sys.stdin.buffer.readline(16385)
    if not line:
        return 0

    def parent_watch() -> None:
        # Parent retains stdin after the one request. EOF means ownership ended.
        # A buffered read here holds stdin's lock during normal interpreter
        # shutdown, aborting an otherwise successful worker. Read the raw pipe.
        try:
            while os.read(sys.stdin.fileno(), 1):
                continue
        finally:
            os._exit(0)

    threading.Thread(target=parent_watch, daemon=True, name="jasna-parent").start()

    def emit(message: dict) -> None:
        print(json.dumps(message, ensure_ascii=True), flush=True)

    try:
        request = json.loads(line)
        if not isinstance(request, dict) or set(request) != {"destination", "files"}:
            raise ValueError("invalid download request")
        dest, names = request["destination"], request["files"]
        if not isinstance(dest, str) or not Path(dest).is_absolute():
            raise ValueError("destination must be absolute")
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise ValueError("invalid file list")
        download(Path(dest), names, emit)
        return 0
    except Exception as exc:
        # No request headers, tokens or redirect query strings in UI/logs.
        emit(
            dict(
                state="error",
                error=f"Download failed ({type(exc).__name__}). Check disk space and network, then retry.",
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
