"""TIFF с пустым каталогом после изображения (заказ 26264994): читаемая копия."""

from __future__ import annotations

import hashlib
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from core import tiff_repair
from core.tiff_repair import find_stray_ifd, readable_tiff, substitute_magick_inputs
from core.tool_runner import run_command


MAGICK = shutil.which("magick")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGE_MAGIC_TIFF_REPAIR_DIR", str(tmp_path / "repair"))
    tiff_repair._cache.clear()
    yield
    tiff_repair._cache.clear()


def _append_stray_ifd(path: Path) -> None:
    """Дописать в конец цепочки каталог с одним частным тегом 52545, как в заказе."""
    data = bytearray(path.read_bytes())
    e = "<" if data[:2] == b"II" else ">"
    offset = struct.unpack(e + "I", data[4:8])[0]
    while True:
        count = struct.unpack(e + "H", data[offset : offset + 2])[0]
        next_field = offset + 2 + count * 12
        nxt = struct.unpack(e + "I", data[next_field : next_field + 4])[0]
        if not nxt:
            break
        offset = nxt
    if len(data) % 2:
        data += b"\0"
    payload_at = len(data)
    data += b"private-data" * 10
    if len(data) % 2:
        data += b"\0"
    stray_at = len(data)
    data += struct.pack(e + "H", 1)
    data += struct.pack(e + "HHII", 52545, 7, 120, payload_at)  # UNDEFINED, 120 байт
    data += struct.pack(e + "I", 0)
    data[next_field : next_field + 4] = struct.pack(e + "I", stray_at)
    path.write_bytes(bytes(data))


def _make_tiff(path: Path) -> None:
    subprocess.run(
        [MAGICK, "-size", "40x30", "xc:cmyk(10%,20%,30%,0%)", "-colorspace", "CMYK",
         "-density", "300", "-units", "PixelsPerInch", str(path)],
        check=True,
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_clean_and_non_tiff_files_are_left_alone(tmp_path):
    other = tmp_path / "a.tif"
    other.write_bytes(b"not a tiff at all")
    assert find_stray_ifd(other) is None
    assert readable_tiff(other) is None
    assert readable_tiff(tmp_path / "missing.tif") is None


def test_non_magick_commands_are_not_rewritten(tmp_path):
    source = tmp_path / "x.tif"
    source.write_bytes(b"II*\0")
    command = ["gs", str(source), "out.tif"]
    assert substitute_magick_inputs(command) == command


@pytest.mark.skipif(MAGICK is None, reason="нужен ImageMagick")
def test_stray_ifd_is_detected_and_copy_is_readable(tmp_path):
    source = tmp_path / "order-face.tif"
    _make_tiff(source)
    assert find_stray_ifd(source) is None
    _append_stray_ifd(source)
    before = _sha(source)

    stray = find_stray_ifd(source)
    assert stray is not None and stray[1] == 1 and stray[2] == [52545]

    # Без подмены ImageMagick на таком файле падает — как в заказе 26264994.
    raw = subprocess.run([MAGICK, "identify", "-ping", str(source)], capture_output=True)
    if raw.returncode == 0:
        pytest.skip("эта сборка libtiff не останавливается на пустом каталоге")

    result = run_command(
        [MAGICK, "identify", "-ping", "-format", "%w %h %f\n", str(source)],
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.split() == ["40", "30", "order-face.tif"]

    pdf = tmp_path / "out.pdf"
    run_command([MAGICK, f"{source}[0]", "-compress", "none", str(pdf)], check=True)
    assert pdf.stat().st_size > 0

    assert _sha(source) == before  # исходник клиента не меняется
    copy = Path(readable_tiff(source))
    assert copy.name == source.name and copy.parent.parent == tmp_path / "repair"
    assert find_stray_ifd(copy) is None


@pytest.mark.skipif(MAGICK is None, reason="нужен ImageMagick")
def test_output_argument_is_never_substituted(tmp_path):
    source = tmp_path / "in.tif"
    target = tmp_path / "out.tif"
    _make_tiff(source)
    _make_tiff(target)
    _append_stray_ifd(source)
    _append_stray_ifd(target)
    command = ["magick", str(source), "-strip", str(target)]
    rewritten = substitute_magick_inputs(command)
    assert rewritten[1] != str(source) and rewritten[1].endswith("in.tif")
    assert rewritten[-1] == str(target)

    frame = substitute_magick_inputs(["magick", "identify", f"{source}[0]"])
    assert frame[-1].endswith("in.tif[0]") and frame[-1] != f"{source}[0]"


@pytest.mark.skipif(MAGICK is None, reason="нужен ImageMagick")
def test_copy_is_reused_until_source_changes(tmp_path):
    source = tmp_path / "s.tif"
    _make_tiff(source)
    _append_stray_ifd(source)
    first = readable_tiff(source)
    tiff_repair._cache.clear()
    assert readable_tiff(source) == first  # тот же размер и mtime → та же копия

    _make_tiff(source)  # клиент пересохранил файл без лишнего каталога
    assert readable_tiff(source) is None
