"""
Ядро извлечения метаданных графических файлов через ImageMagick.
"""

import os
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from .tool_runner import run_command

@dataclass
class ImageMetadata:
    file_path: str
    file_name: str
    format: str
    width_px: int
    height_px: int
    dpi: float
    dpi_x: float
    dpi_y: float
    width_mm: float
    height_mm: float
    colorspace: str
    icc_profile: str
    image_type: str
    depth_bits: str
    size_mb: float


@dataclass(frozen=True)
class TiffStructure:
    """Structural properties of a TIFF, separated from its decoded scenes.

    ImageMagick exposes Photoshop layers as images in the same sequence as
    real TIFF pages.  ``page_count`` therefore deliberately ignores those
    layers, while the remaining fields describe the Photoshop document and
    its flattened composite independently.
    """

    page_count: int
    has_unflattened_layers: bool
    has_alpha: bool
    channels: str
    layer_count: int | None = 0


def _identify_command() -> list[str]:
    magick_cmd = shutil.which("magick")
    if magick_cmd:
        return [magick_cmd, "identify"]
    identify_cmd = shutil.which("identify")
    if identify_cmd:
        return [identify_cmd]
    raise FileNotFoundError("ImageMagick CLI (magick / identify) не найден в системе.")


# ---------------------------------------------------------------------------
# Cheap inspection (``-ping``)
#
# ``magick identify`` without ``-ping`` decodes every pixel (about a second for
# a 6000x8000 TIFF); ``-ping`` only reads the headers (a few milliseconds).  The
# batch path needs dimensions, density/units, colorspace, the number of frames,
# alpha presence and Photoshop-layer presence, and all of them are identical
# under ``-ping`` except one: ``%[channels]`` loses the alpha channel of a TIFF
# (``srgba`` is reported as ``srgb``).  Alpha is therefore decided from the TIFF
# header (see ``_read_tiff_ifd0``) and, whenever the header cannot *prove* the
# absence of an alpha channel, from a full-decode identify call as before.
# Not available under ``-ping`` and not needed by the batch path: ``%[type]``,
# ``%[depth]`` and ``%[icc:description]``; ``ImageMetadata`` loads them lazily.
# ---------------------------------------------------------------------------

_SCENE_FIELDS = (
    "%p\t%w\t%h\t%x\t%y\t%[units]\t%[colorspace]\t%[channels]\t%[tiff:has-layers]\t%m\t%f"
)
_LAYER_TRUE = {"true", "1", "yes"}


class _Scene(NamedTuple):
    width: str
    height: str
    res_x: str
    res_y: str
    units: str
    colorspace: str
    channels: str
    has_layers: str
    fmt: str
    name: str


def _identify_output(args: list[str], image_path: str) -> str:
    cmd = _identify_command() + args + [image_path]
    result = run_command(cmd, capture_output=True, text=True, errors="replace", check=True)
    return result.stdout


def _parse_scenes(stdout: str) -> list[_Scene]:
    scenes: list[_Scene] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t", maxsplit=10)
        if len(fields) < 11:
            fields += [""] * (11 - len(fields))
        scenes.append(_Scene(*(value.strip() for value in fields[1:])))
    return scenes


def _scenes(args: list[str], image_path: str, *, ping: bool) -> list[_Scene]:
    flags = ["-ping"] if ping else []
    return _parse_scenes(
        _identify_output(flags + args + ["-format", _SCENE_FIELDS + "\n"], image_path)
    )


class _TiffIfd0(NamedTuple):
    photometric: int | None
    samples_per_pixel: int
    extra_samples: tuple[int, ...]
    ink_set: int | None
    number_of_inks: int | None
    has_image_source_data: bool

    def alpha_is_excluded(self) -> bool:
        """True only when the header proves the image has no alpha channel."""
        if self.extra_samples or self.photometric is None:
            return False
        base = {0: 1, 1: 1, 2: 3, 3: 1, 6: 3, 8: 3, 9: 3, 10: 3}.get(self.photometric)
        if self.photometric == 5:  # Separated: CMYK only
            if self.ink_set not in (None, 1) or self.number_of_inks not in (None, 4):
                return False
            base = 4
        return base is not None and self.samples_per_pixel == base


_TIFF_TYPE_SIZES = {1: 1, 3: 2, 4: 4}  # BYTE, SHORT, LONG
_TIFF_TYPE_CODES = {1: "B", 3: "H", 4: "I"}


def _read_tiff_ifd0(image_path: str) -> _TiffIfd0 | None:
    """Read the structural tags of the first IFD; ``None`` if anything is unusual.

    Handles classic TIFF and BigTIFF in both byte orders and reads a few hundred
    bytes, never pixel data.  Every doubt (bad magic, truncated directory, tag of
    an unexpected type) returns ``None`` so that callers use the full identify.
    """
    try:
        with open(image_path, "rb") as fh:
            head = fh.read(16)
            if len(head) < 8 or head[:2] not in (b"II", b"MM"):
                return None
            e = "<" if head[:2] == b"II" else ">"
            magic = struct.unpack(e + "H", head[2:4])[0]
            if magic == 42:
                big = False
                offset = struct.unpack(e + "I", head[4:8])[0]
            elif magic == 43:
                if len(head) < 16 or struct.unpack(e + "HH", head[4:8]) != (8, 0):
                    return None
                big = True
                offset = struct.unpack(e + "Q", head[8:16])[0]
            else:
                return None
            fh.seek(offset)
            count_raw = fh.read(8 if big else 2)
            if len(count_raw) != (8 if big else 2):
                return None
            n = struct.unpack(e + ("Q" if big else "H"), count_raw)[0]
            entry_size = 20 if big else 12
            if not 0 < n <= 2048:
                return None
            raw = fh.read(n * entry_size)
            if len(raw) != n * entry_size:
                return None

            entries: dict[int, tuple[int, int, bytes]] = {}
            for i in range(n):
                entry = raw[i * entry_size : (i + 1) * entry_size]
                if big:
                    tag, typ, cnt = struct.unpack(e + "HHQ", entry[:12])
                    inline = entry[12:20]
                else:
                    tag, typ, cnt = struct.unpack(e + "HHI", entry[:8])
                    inline = entry[8:12]
                entries[tag] = (typ, cnt, inline)

            def values(tag: int) -> tuple[int, ...] | None:
                typ, cnt, inline = entries[tag]
                if typ not in _TIFF_TYPE_SIZES or not 0 < cnt <= 16:
                    raise ValueError("unsupported tag layout")
                size = _TIFF_TYPE_SIZES[typ] * cnt
                if size <= len(inline):
                    data = inline[:size]
                else:
                    ptr = struct.unpack(e + ("Q" if big else "I"), inline)[0]
                    fh.seek(ptr)
                    data = fh.read(size)
                    if len(data) != size:
                        raise ValueError("truncated tag value")
                return struct.unpack(e + _TIFF_TYPE_CODES[typ] * cnt, data)

            def single(tag: int, default: int | None) -> int | None:
                if tag not in entries:
                    return default
                found = values(tag)
                if found is None or len(found) != 1:
                    raise ValueError("expected a single value")
                return found[0]

            return _TiffIfd0(
                photometric=single(262, None),
                samples_per_pixel=single(277, 1) or 1,
                extra_samples=tuple(values(338) or ()) if 338 in entries else (),
                ink_set=single(332, None),
                number_of_inks=single(334, None),
                has_image_source_data=37724 in entries,
            )
    except (OSError, ValueError, struct.error):
        return None


def _layer_flags(scenes: list[_Scene]) -> bool:
    return any(scene.has_layers.lower() in _LAYER_TRUE for scene in scenes)


def _structure_from_scenes(
    composite: list[_Scene], all_scenes: list[_Scene], *, channels: str, alpha: bool
) -> TiffStructure:
    has_layers = _layer_flags(all_scenes)
    layer_count = 0
    if has_layers:
        decoded_layer_count = len(all_scenes) - len(composite)
        layer_count = decoded_layer_count if decoded_layer_count > 0 else None
    return TiffStructure(
        page_count=len(composite),
        has_unflattened_layers=has_layers,
        has_alpha=alpha,
        channels=channels,
        layer_count=layer_count,
    )


def _channel_mnemonic(channels: str) -> str:
    return channels.split(maxsplit=1)[0].lower() if channels else ""


def _inspect_tiff(image_path: str) -> tuple[TiffStructure, _Scene, int]:
    """Return the structure, the first (composite) scene and the launch count.

    Cheap path (header proves no alpha): one ``-ping`` launch, plus one more
    ``-ping`` launch only when Photoshop layers are present (to count layers).
    Otherwise: one full-decode launch with ``tiff:ignore-layers`` (the only
    source of the true channel mnemonic) and one ``-ping`` launch for layers.
    """
    ifd0 = _read_tiff_ifd0(image_path)
    ignore_layers = ["-define", "tiff:ignore-layers=true"]
    if ifd0 is not None and ifd0.alpha_is_excluded():
        all_scenes = _scenes([], image_path, ping=True)
        if not all_scenes:
            raise ValueError("TIFF не содержит доступных изображений")
        if not _layer_flags(all_scenes):
            first = all_scenes[0]
            structure = _structure_from_scenes(
                all_scenes, all_scenes, channels=first.channels, alpha=False
            )
            return structure, first, 1
        composite = _scenes(ignore_layers, image_path, ping=True)
        if not composite:
            raise ValueError("TIFF не содержит доступных изображений")
        # Alpha is excluded by the header, so ``-ping`` channels are exact.
        structure = _structure_from_scenes(
            composite, all_scenes, channels=composite[0].channels, alpha=False
        )
        return structure, composite[0], 2

    composite = _scenes(ignore_layers, image_path, ping=False)
    if not composite:
        raise ValueError("TIFF не содержит доступных изображений")
    all_scenes = _scenes([], image_path, ping=True)
    first = composite[0]
    structure = _structure_from_scenes(
        composite,
        all_scenes,
        channels=first.channels,
        alpha=_channel_mnemonic(first.channels).endswith("a"),
    )
    return structure, first, 2


def count_frames(image_path: str) -> int:
    """Return the number of images/pages stored in a raster file."""
    stdout = _identify_output(["-ping", "-format", "%p\n"], image_path)
    return len([line for line in stdout.splitlines() if line.strip()])


def inspect_tiff_structure(image_path: str) -> TiffStructure:
    """Inspect TIFF pages, Photoshop layers, and composite alpha separately.

    The TIFF coder's ``ignore-layers`` define prevents embedded Photoshop
    layers from inflating the number of decoded images.  The channel mnemonic
    for the first resulting image describes the flattened composite: e.g.
    ``srgb``/``cmyk`` without alpha and ``srgba``/``cmyka`` with alpha.
    """
    return _inspect_tiff(image_path)[0]


@dataclass(frozen=True)
class RasterInspection:
    """Everything the batch path needs from one raster file.

    ``tiff`` is set for TIFF files only.  ``metadata`` describes the first
    frame and is ``None`` when the file reports no frames at all.
    """

    frame_count: int
    metadata: "ImageMetadata | None"
    tiff: TiffStructure | None = None


TIFF_SUFFIXES = {".tif", ".tiff"}


def inspect_raster(image_path: str) -> RasterInspection:
    """Inspect a raster file with the fewest ImageMagick launches.

    TIFF: one ``-ping`` launch when the header proves there is no alpha channel
    and no Photoshop layers, two otherwise.  JPEG/PNG: one ``-ping`` launch.
    """
    if Path(image_path).suffix.lower() in TIFF_SUFFIXES:
        structure, first, _launches = _inspect_tiff(image_path)
        return RasterInspection(
            frame_count=structure.page_count,
            metadata=_lazy_metadata(image_path, first),
            tiff=structure,
        )
    scenes = _scenes([], image_path, ping=True)
    return RasterInspection(
        frame_count=len(scenes),
        metadata=_lazy_metadata(image_path, scenes[0]) if scenes else None,
    )


_LAZY_FIELDS = ("icc_profile", "image_type", "depth_bits")


def _build_metadata_values(
    image_path: str,
    *,
    file_name: str,
    file_fmt: str,
    w_px: float,
    h_px: float,
    res_x: float,
    res_y: float,
    units: str,
    colorspace: str,
) -> dict:
    if "Centimeter" in units:
        dpi_x = res_x * 2.54
        dpi_y = res_y * 2.54
    else:
        dpi_x = res_x if res_x > 0 else 72.0
        dpi_y = res_y if res_y > 0 else 72.0

    dpi = min(dpi_x, dpi_y)

    width_mm = round((w_px / dpi_x) * 25.4, 1)
    height_mm = round((h_px / dpi_y) * 25.4, 1)

    size_bytes = os.path.getsize(image_path)
    size_mb = round(size_bytes / (1024 * 1024), 2)

    return dict(
        file_path=image_path,
        file_name=file_name,
        format=file_fmt,
        width_px=int(w_px),
        height_px=int(h_px),
        dpi=round(dpi, 1),
        dpi_x=round(dpi_x, 1),
        dpi_y=round(dpi_y, 1),
        width_mm=width_mm,
        height_mm=height_mm,
        colorspace=colorspace,
        size_mb=size_mb,
    )


class _LazyImageMetadata(ImageMetadata):
    """``ImageMetadata`` whose decode-only fields are read on first access.

    ``icc_profile``, ``image_type`` and ``depth_bits`` need a full pixel decode
    (``%[type]``, ``%[depth]``, ``%[icc:description]``) and are not used by the
    batch path; legacy callers (validators, reports) still get real values,
    they only pay for the decode when they touch these attributes.
    """

    def __init__(self, **values) -> None:  # noqa: D107 - dataclass-compatible
        for name, value in values.items():
            object.__setattr__(self, name, value)

    def __getattr__(self, name: str):
        if name not in _LAZY_FIELDS:
            raise AttributeError(name)
        full = inspect_file_full(self.__dict__["file_path"])
        for field_name in _LAZY_FIELDS:
            object.__setattr__(self, field_name, getattr(full, field_name))
        return self.__dict__[name]


def _lazy_metadata(image_path: str, scene: _Scene) -> ImageMetadata:
    w_px = float(scene.width) if scene.width else 0.0
    h_px = float(scene.height) if scene.height else 0.0
    res_x = float(scene.res_x) if scene.res_x else 72.0
    res_y = float(scene.res_y) if scene.res_y else res_x
    return _LazyImageMetadata(
        **_build_metadata_values(
            image_path,
            file_name=scene.name or os.path.basename(image_path),
            file_fmt=scene.fmt,
            w_px=w_px,
            h_px=h_px,
            res_x=res_x,
            res_y=res_y,
            units=scene.units or "PixelsPerInch",
            colorspace=scene.colorspace or "sRGB",
        )
    )


def inspect_file(image_path: str) -> ImageMetadata:
    """Extract metadata with a single cheap ``-ping`` launch.

    ``icc_profile``, ``image_type`` and ``depth_bits`` are loaded lazily with a
    full decode (see ``inspect_file_full``) the first time they are read.
    """
    scenes = _scenes([], image_path, ping=True)
    if not scenes:
        raise ValueError("файл не содержит доступных изображений")
    return _lazy_metadata(image_path, scenes[0])


def inspect_file_full(image_path: str) -> ImageMetadata:
    """Извлекает метаданные из графического файла, включая ICC-профиль.

    Полное декодирование пикселей: нужно только для ``icc_profile``,
    ``image_type`` и ``depth_bits``.
    """
    cmd = _identify_command() + [
        # Never print %[profile:icc] here: it is binary profile data and cannot
        # safely be decoded as subprocess text output.
        "-format", "%f\n%m\n%w\n%h\n%x\n%y\n%[units]\n%[colorspace]\n%[type]\n%[depth]\n%[icc:description]",
        image_path
    ]
    result = run_command(cmd, capture_output=True, text=True, errors="replace", check=True)
    lines = [line.strip() for line in result.stdout.strip().split("\n")]

    file_name = lines[0] if len(lines) > 0 else os.path.basename(image_path)
    file_fmt = lines[1] if len(lines) > 1 else ""
    w_px = float(lines[2]) if len(lines) > 2 else 0.0
    h_px = float(lines[3]) if len(lines) > 3 else 0.0
    res_x = float(lines[4]) if len(lines) > 4 else 72.0
    res_y = float(lines[5]) if len(lines) > 5 else res_x
    units = lines[6] if len(lines) > 6 else "PixelsPerInch"
    colorspace = lines[7] if len(lines) > 7 else "sRGB"
    img_type = lines[8] if len(lines) > 8 else ""
    depth = lines[9] if len(lines) > 9 else "8"
    
    icc_desc = lines[10] if len(lines) > 10 and lines[10] else ""
    icc_profile = icc_desc or "Не внедрен"

    if "Centimeter" in units:
        dpi_x = res_x * 2.54
        dpi_y = res_y * 2.54
    else:
        dpi_x = res_x if res_x > 0 else 72.0
        dpi_y = res_y if res_y > 0 else 72.0

    dpi = min(dpi_x, dpi_y)

    width_mm = round((w_px / dpi_x) * 25.4, 1)
    height_mm = round((h_px / dpi_y) * 25.4, 1)

    size_bytes = os.path.getsize(image_path)
    size_mb = round(size_bytes / (1024 * 1024), 2)

    return ImageMetadata(
        file_path=image_path,
        file_name=file_name,
        format=file_fmt,
        width_px=int(w_px),
        height_px=int(h_px),
        dpi=round(dpi, 1),
        dpi_x=round(dpi_x, 1),
        dpi_y=round(dpi_y, 1),
        width_mm=width_mm,
        height_mm=height_mm,
        colorspace=colorspace,
        icc_profile=icc_profile,
        image_type=img_type,
        depth_bits=depth,
        size_mb=size_mb
    )
