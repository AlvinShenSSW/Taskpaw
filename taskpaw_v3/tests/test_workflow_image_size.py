"""U1b: a cover's pixel size from its header (JPEG, PNG, GIF, BMP, WebP)."""

from __future__ import annotations

import struct
import time
import zlib

import pytest

from taskpaw_v3.workflow import image_size as isz


def jpeg(width: int, height: int, *, exif: int = 0) -> bytes:
    """A JPEG header as cameras and MDCX write it: APP0, an optional APP1 of
    `exif` bytes, a DQT, then the frame (SOF0) — enough for the size."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01" + b"\x00" * 7
    app1 = b"\xff\xe1" + struct.pack(">H", exif + 2) + b"\xff" * exif if exif else b""
    dqt = b"\xff\xdb" + struct.pack(">H", 67) + b"\x00" * 65
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, height, width, 3) + b"\x00" * 9
    return b"\xff\xd8" + app0 + app1 + dqt + sof + b"\xff\xda\x00\x02" + b"\x00" * 8


def png(width: int, height: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = struct.pack(">I", 13) + b"IHDR" + ihdr
    return b"\x89PNG\r\n\x1a\n" + chunk + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))


@pytest.mark.parametrize(
    ("data", "size"),
    [
        (jpeg(2184, 1477), (2184, 1477)),
        (jpeg(421, 600, exif=30000), (421, 600)),  # a long EXIF block first
        (b"\xff\xd8\xff\xc2" + struct.pack(">HBHH", 17, 8, 568, 840), (840, 568)),
        (png(840, 568), (840, 568)),
        (b"GIF89a" + struct.pack("<HH", 300, 200) + b"\x00" * 22, (300, 200)),
        (
            b"BM" + b"\x00" * 12 + struct.pack("<Iii", 40, 640, -480) + b"\x00" * 6,
            (640, 480),
        ),
        (
            b"RIFF\x00\x00\x00\x00WEBPVP8X"
            + b"\x00" * 8
            + (799).to_bytes(3, "little")
            + (599).to_bytes(3, "little")
            + b"\x00" * 2,
            (800, 600),
        ),
        (
            b"RIFF\x00\x00\x00\x00WEBPVP8 "
            + b"\x00" * 4
            + b"\x00\x00\x00\x9d\x01\x2a"
            + struct.pack("<HH", 1024, 768)
            + b"\x00" * 2,
            (1024, 768),
        ),
    ],
    ids=[
        "jpeg",
        "jpeg-exif",
        "jpeg-progressive",
        "png",
        "gif",
        "bmp",
        "webp-x",
        "webp",
    ],
)
def test_the_size_comes_from_the_header(tmp_path, data, size):
    path = tmp_path / "cover.img"
    path.write_bytes(data)
    assert isz.image_size(path) == size


@pytest.mark.parametrize(
    "data",
    [b"image fanart.jpg", b"\xff\xd8\xff\xe0\x00", b"", b"\xff\xd8\xff\xd9"],
    ids=["text", "truncated", "empty", "no-frame"],
)
def test_what_is_no_image_has_no_size(tmp_path, data):
    path = tmp_path / "cover.jpg"
    path.write_bytes(data)
    assert isz.image_size(path) is None
    assert isz.image_size(tmp_path / "missing.jpg") is None


def test_landscape_is_wider_than_tall_and_a_rewrite_is_read_again(tmp_path):
    path = tmp_path / "fanart.jpg"
    path.write_bytes(jpeg(2184, 1477))
    assert isz.is_landscape(path) == (2184, 1477)
    path.write_bytes(jpeg(421, 600, exif=4))  # a new size: the cache follows
    assert isz.image_size(path) == (421, 600)
    assert isz.is_landscape(path) is None
    path.write_bytes(jpeg(500, 500))
    assert isz.is_landscape(path) is None


def test_a_forged_header_with_a_huge_side_has_no_size(tmp_path):
    """A PNG IHDR of 4294967295 x 1 must not win the jacket pick."""
    path = tmp_path / "forged.png"
    path.write_bytes(png(4294967295, 1))
    assert isz.image_size(path) is None
    assert isz.is_landscape(path) is None
    path.write_bytes(png(65535, 1))  # the largest side a header may claim
    assert isz.image_size(path) == (65535, 1)
    path.write_bytes(png(65536, 1))
    assert isz.image_size(path) is None


@pytest.mark.parametrize(
    "padding",
    [b"\xff" * (20 * 1024 * 1024), b"\x00" * (4 * 1024 * 1024)],
    ids=["ff-fill-20mb", "zero-padding-4mb"],
)
def test_a_padded_jpeg_gives_up_fast(tmp_path, padding):
    path = tmp_path / "padded.jpg"
    path.write_bytes(b"\xff\xd8" + padding)
    started = time.perf_counter()
    assert isz.image_size(path) is None
    assert time.perf_counter() - started < 0.5


def test_a_frame_after_fill_bytes_is_still_found(tmp_path):
    path = tmp_path / "filled.jpg"
    path.write_bytes(b"\xff\xd8" + b"\xff" * 5000 + jpeg(800, 600)[2:])
    assert isz.image_size(path) == (800, 600)
