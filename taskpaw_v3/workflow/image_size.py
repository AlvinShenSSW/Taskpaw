"""The pixel size of a cover image, read from its header (U1b).

The 大图 (lightbox) wants the full jacket: the largest landscape image a film
has. Only the header is read (no Pillow in the agent): JPEG, PNG, GIF, BMP and
WebP. Sizes are cached by path, mtime and size, since every list poll asks.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import BinaryIO

Size = tuple[int, int]

# JPEG start-of-frame markers (every SOFn but DHT C4, JPG C8 and DAC CC)
_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
# Markers without a length: TEM, RST0–7 (SOI/EOI end the walk before)
_BARE = {0x01, *range(0xD0, 0xD8)}
_JPEG_SCAN_LIMIT = 4 * 1024 * 1024
_CACHE: dict[Path, tuple[tuple[int, int], Size | None]] = {}
_CACHE_LIMIT = 2048


def image_size(path: Path) -> Size | None:
    """(width, height) of the image at `path`; None when unreadable or unknown."""
    try:
        st = path.stat()
        key = (st.st_mtime_ns, st.st_size)
        cached = _CACHE.get(path)
        if cached is not None and cached[0] == key:
            return cached[1]
        with path.open("rb") as stream:
            size = _read(stream)
    except (OSError, struct.error, ValueError):
        return None
    if len(_CACHE) >= _CACHE_LIMIT:
        _CACHE.clear()
    _CACHE[path] = (key, size)
    return size


def is_landscape(path: Path) -> Size | None:
    """The size of a landscape image (wider than tall), else None."""
    size = image_size(path)
    return size if size is not None and size[0] > size[1] else None


def _read(stream: BinaryIO) -> Size | None:
    head = stream.read(32)
    if head.startswith(b"\xff\xd8"):
        stream.seek(2)
        return _valid(_jpeg(stream))
    if head.startswith(b"\x89PNG\r\n\x1a\n") and head[12:16] == b"IHDR":
        return _valid(struct.unpack(">II", head[16:24]))
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return _valid(struct.unpack("<HH", head[6:10]))
    if head.startswith(b"BM"):
        return _valid(_bmp(head))
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return _valid(_webp(head))
    return None


def _valid(size: tuple[int, int] | None) -> Size | None:
    if size is None or size[0] <= 0 or size[1] <= 0:
        return None
    return int(size[0]), int(size[1])


def _jpeg(stream: BinaryIO) -> tuple[int, int] | None:
    while stream.tell() < _JPEG_SCAN_LIMIT:
        byte = stream.read(1)
        if not byte:
            return None
        if byte != b"\xff":
            continue  # entropy-coded data or padding between segments
        marker = stream.read(1)
        while marker == b"\xff":  # fill bytes
            marker = stream.read(1)
        if not marker:
            return None
        code = marker[0]
        if code in (0x00, *_BARE):
            continue
        if code == 0xD9:  # EOI before a frame
            return None
        length = struct.unpack(">H", stream.read(2))[0]
        if length < 2:
            return None
        if code in _SOF:
            data = stream.read(5)
            height, width = struct.unpack(">HH", data[1:5])
            return width, height
        stream.seek(length - 2, 1)
    return None


def _bmp(head: bytes) -> tuple[int, int] | None:
    header = struct.unpack("<I", head[14:18])[0]
    if header == 12:  # BITMAPCOREHEADER
        return struct.unpack("<HH", head[18:22])
    width, height = struct.unpack("<ii", head[18:26])
    return abs(width), abs(height)


def _webp(head: bytes) -> tuple[int, int] | None:
    chunk = head[12:16]
    if chunk == b"VP8 ":
        width, height = struct.unpack("<HH", head[26:30])
        return width & 0x3FFF, height & 0x3FFF
    if chunk == b"VP8L":
        bits = struct.unpack("<I", head[21:25])[0]
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X":
        width = int.from_bytes(head[24:27], "little") + 1
        height = int.from_bytes(head[27:30], "little") + 1
        return width, height
    return None
