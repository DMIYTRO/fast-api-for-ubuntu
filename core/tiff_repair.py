"""TIFF с «пустыми» каталогами после изображения: читаемая копия для ImageMagick.

Некоторые программы дописывают в цепочку каталогов TIFF (IFD) после изображения
служебный каталог без картинки — например, с одним частным тегом 52545 (0xcd41)
и его данными (заказ 26264994). Photoshop такой каталог пропускает и открывает
файл без замечаний, а libtiff в ImageMagick останавливается на нём с
``TIFF directory is missing required "ImageLength" field`` и код возврата 1 —
и чтение параметров, и превью, и PDF такого файла падают.

``readable_tiff`` находит первый каталог без ``ImageWidth``/``ImageLength`` и
делает локальную копию, в которой цепочка обрывается перед ним (меняются только
4 байта ссылки на следующий каталог). Исходник клиента не меняется никогда.
Копия лежит во временной папке под исходным именем файла (``%f`` в выводе
``identify`` остаётся прежним) и переиспользуется, пока у исходника те же
размер и время изменения. ``core.tool_runner.run_command`` подставляет копию во
все вызовы ImageMagick, поэтому отдельные места обработки об этом не знают.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import struct
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path


logger = logging.getLogger("image_magic.tiff_repair")

TIFF_SUFFIXES = (".tif", ".tiff")
#: Каталоги без изображения: обязательные теги ImageWidth и ImageLength.
_IMAGE_TAGS = {256, 257}
_MAX_IFDS = 64
_MAX_ENTRIES = 4096
_CACHE_SIZE = 512
#: Копии старше суток удаляются при создании новой.
_COPY_MAX_AGE_SECONDS = 24 * 3600

_lock = threading.Lock()
_cache: "OrderedDict[tuple[str, int, int], str | None]" = OrderedDict()


def repair_dir() -> Path:
    override = os.environ.get("IMAGE_MAGIC_TIFF_REPAIR_DIR")
    return Path(override) if override else Path(tempfile.gettempdir()) / "image-magic-tiff-repair"


def find_stray_ifd(path: str | os.PathLike[str]) -> tuple[int, int, list[int]] | None:
    """``(смещение поля «следующий IFD», номер каталога, теги)`` первого каталога без изображения.

    ``None`` — файл не TIFF, цепочка в порядке или устроена необычно (тогда
    ничего не трогаем). Первый каталог без изображения не считается: такой файл
    не чинится обрезкой.
    """
    try:
        with open(path, "rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            head = fh.read(16)
            if len(head) < 8 or head[:2] not in (b"II", b"MM"):
                return None
            e = "<" if head[:2] == b"II" else ">"
            magic = struct.unpack(e + "H", head[2:4])[0]
            if magic == 42:
                big = False
                offset = struct.unpack(e + "I", head[4:8])[0]
            elif magic == 43 and len(head) >= 16:
                big = True
                offset = struct.unpack(e + "Q", head[8:16])[0]
            else:
                return None
            count_fmt, count_size = ("Q", 8) if big else ("H", 2)
            entry_size = 20 if big else 12
            ptr_fmt, ptr_size = ("Q", 8) if big else ("I", 4)

            seen: set[int] = set()
            previous_next_field: int | None = None
            index = 0
            while offset and index < _MAX_IFDS:
                if offset in seen or offset + count_size > size:
                    return None
                seen.add(offset)
                fh.seek(offset)
                count = struct.unpack(e + count_fmt, fh.read(count_size))[0]
                if not 0 < count <= _MAX_ENTRIES:
                    return None
                table = fh.read(count * entry_size)
                if len(table) != count * entry_size:
                    return None
                tags = [
                    struct.unpack(e + "H", table[i * entry_size : i * entry_size + 2])[0]
                    for i in range(count)
                ]
                if not _IMAGE_TAGS <= set(tags):
                    if index == 0 or previous_next_field is None:
                        return None
                    return previous_next_field, index, sorted(tags)
                previous_next_field = offset + count_size + count * entry_size
                raw = fh.read(ptr_size)
                if len(raw) != ptr_size:
                    return None
                offset = struct.unpack(e + ptr_fmt, raw)[0]
                index += 1
    except (OSError, struct.error):
        return None
    return None


def readable_tiff(path: str | os.PathLike[str]) -> str | None:
    """Путь читаемой копии TIFF или ``None``, если исходник читается как есть."""
    source = os.fspath(path)
    try:
        stat = os.stat(source)
    except OSError:
        return None
    key = (os.path.realpath(source), stat.st_size, stat.st_mtime_ns)
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            cached = _cache[key]
            if cached is None or os.path.isfile(cached):
                return cached
    result = _make_copy(source, key)
    with _lock:
        _cache[key] = result
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)
    return result


def _make_copy(source: str, key: tuple[str, int, int]) -> str | None:
    stray = find_stray_ifd(source)
    if stray is None:
        return None
    next_field, index, tags = stray
    digest = hashlib.sha256(repr(key).encode("utf-8")).hexdigest()[:24]
    base = repair_dir()
    target_dir = base / digest
    target = target_dir / os.path.basename(source)
    if target.is_file():
        return str(target)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        _sweep(base, keep=target_dir)
        fd, tmp_name = tempfile.mkstemp(prefix=".part-", dir=target_dir)
        try:
            with os.fdopen(fd, "r+b") as out, open(source, "rb") as src:
                shutil.copyfileobj(src, out, 1024 * 1024)
                out.flush()
                check = find_stray_ifd(tmp_name)
                if check is None or check[0] != next_field:
                    raise OSError("исходник изменился во время копирования")
                out.seek(next_field)
                width = 8 if _is_bigtiff(tmp_name) else 4
                out.write(b"\0" * width)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError as exc:
        logger.warning("tiff.stray_ifd_copy_failed path=%s error=%s", source, exc)
        return None
    logger.warning(
        "tiff.stray_ifd_stripped path=%s ifd=%s tags=%s copy=%s", source, index, tags, target
    )
    return str(target)


def _is_bigtiff(path: str) -> bool:
    with open(path, "rb") as fh:
        head = fh.read(4)
    e = "<" if head[:2] == b"II" else ">"
    return struct.unpack(e + "H", head[2:4])[0] == 43


def _sweep(base: Path, *, keep: Path) -> None:
    cutoff = time.time() - _COPY_MAX_AGE_SECONDS
    try:
        entries = list(base.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry == keep:
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            continue


def substitute_magick_inputs(command: list[str]) -> list[str]:
    """Заменить входные TIFF с пустыми каталогами на читаемые копии.

    Только для ImageMagick (``magick``, ``identify``, ``convert``). У ``magick``
    без ``identify`` последний аргумент — выходной файл, его не трогаем.
    """
    if not command:
        return command
    tool = os.path.basename(command[0]).lower().removesuffix(".exe")
    if tool not in {"magick", "identify", "convert"}:
        return command
    is_identify = tool == "identify" or (len(command) > 1 and command[1] == "identify")
    last_input = len(command) if is_identify else len(command) - 1
    result = list(command)
    for position in range(1, last_input):
        value = command[position]
        path, frame = _split_frame(value)
        if not path.lower().endswith(TIFF_SUFFIXES) or not os.path.isfile(path):
            continue
        copy = readable_tiff(path)
        if copy is not None:
            result[position] = copy + frame
    return result


def _split_frame(value: str) -> tuple[str, str]:
    if value.endswith("]"):
        start = value.rfind("[")
        if start > 0:
            return value[:start], value[start:]
    return value, ""
