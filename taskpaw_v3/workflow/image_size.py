"""The pixel size of a cover image, read from its header (U1b).

The 大图 (lightbox) wants the full jacket: the largest landscape image a film
has. Only the header is read (no Pillow in the agent): JPEG, PNG, GIF, BMP and
WebP. Sizes are cached by path, mtime and size, since every list poll asks.
"""

from __future__ import annotations

import re
import struct
from pathlib import Path
from typing import BinaryIO

Size = tuple[int, int]

# JPEG start-of-frame markers (every SOFn but DHT C4, JPG C8 and DAC CC)
_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
# Markers without a length: TEM, RST0–7 (SOI/EOI end the walk before)
_BARE = {0x01, *range(0xD0, 0xD8)}
_JPEG_SCAN_LIMIT = 4 * 1024 * 1024
_FILL = re.compile(rb"\xff+")
# A header may claim any 32-bit side; no real cover has one past 16 bits (JPEG, WebP VP8)
_MAX_SIDE = 65535
# A same-size rewrite within one mtime tick can return the earlier size: acceptable for scraped artwork.
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
    if size is None or not (0 < size[0] <= _MAX_SIDE and 0 < size[1] <= _MAX_SIDE):
        return None
    return int(size[0]), int(size[1])


def _jpeg(stream: BinaryIO) -> tuple[int, int] | None:
    """Walk the segments of the first `_JPEG_SCAN_LIMIT` bytes, held in memory."""
    data = stream.read(_JPEG_SCAN_LIMIT)  # the stream sits after SOI
    end = len(data)
    pos = 0
    while True:
        pos = data.find(b"\xff", pos)  # entropy-coded data or padding in between
        if pos < 0:
            return None
        fill = _FILL.match(data, pos)  # a run of 0xFF fill bytes, skipped at C speed
        pos = fill.end() if fill else pos + 1
        if pos >= end:
            return None
        code = data[pos]
        pos += 1
        if code in (0x00, *_BARE):
            continue
        if code == 0xD9:  # EOI before a frame
            return None
        if pos + 2 > end:
            return None
        length = struct.unpack_from(">H", data, pos)[0]
        if length < 2:
            return None
        if code in _SOF:
            if pos + 7 > end:
                return None
            height, width = struct.unpack_from(">HH", data, pos + 3)
            return width, height
        pos += length


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
