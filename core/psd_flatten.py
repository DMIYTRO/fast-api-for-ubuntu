"""Прототип чтения и сведения PSD через ImageMagick (исследование, вне конвейера).

Модуль намеренно не подключён к ``processing/batch_processor.py``: он нужен,
чтобы оценить, как Image Magic мог бы принимать PSD. Два источника правды:

* заголовок и блок image resources PSD разбираются на чистом Python
  (режим цвета, глубина, DPI из ресурса 1005, флаг «есть настоящий
  сведённый композит» из ресурса 1057, наличие ICC в ресурсе 1039);
* ImageMagick ``identify`` даёт сцены: сцена 0 — сохранённый Photoshop
  композит, сцены 1..n — слои с ``psd:layer.visible`` и ``compose``.

Инварианты: цветовое пространство не меняется, DPI сохраняется, скрытые
слои в результат не попадают, ширина и высота никогда не масштабируются.
"""

from __future__ import annotations

import os
import shutil
import struct
from dataclasses import dataclass, field
from pathlib import Path

from .tool_runner import run_command

PSD_SIGNATURE = b"8BPS"
# Режимы цвета из спецификации Adobe: Color Mode в заголовке.
COLOR_MODES = {
    0: "Bitmap",
    1: "Grayscale",
    2: "Indexed",
    3: "RGB",
    4: "CMYK",
    7: "Multichannel",
    8: "Duotone",
    9: "Lab",
}
RESOURCE_RESOLUTION_INFO = 1005
RESOURCE_ICC_PROFILE = 1039
RESOURCE_VERSION_INFO = 1057

STRATEGY_COMPOSITE = "composite"
STRATEGY_MERGE_VISIBLE = "merge_visible"


class PsdError(ValueError):
    """Файл не является PSD/PSB или не может быть прочитан."""


@dataclass(frozen=True)
class PsdLayer:
    index: int
    name: str
    visible: bool
    compose: str
    width_px: int
    height_px: int
    has_alpha: bool


@dataclass(frozen=True)
class PsdStructure:
    layer_count: int
    visible_layer_count: int
    hidden_layer_count: int
    colorspace: str
    has_alpha: bool
    width_px: int
    height_px: int
    dpi: float
    depth_bits: int = 8
    channels: int = 0
    is_psb: bool = False
    has_icc_profile: bool = False
    # None: ресурс 1057 отсутствует (файл записан не Photoshop), композит
    # есть, но его достоверность неизвестна. False: Photoshop сохранил файл
    # без «Maximize Compatibility», сцена 0 не отражает документ.
    has_merged_composite: bool | None = None
    layers: tuple[PsdLayer, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class FlattenResult:
    source: str
    target: str
    strategy: str
    width_px: int
    height_px: int
    colorspace: str
    has_alpha: bool
    dpi: float
    hidden_layers_skipped: int
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _magick() -> str:
    magick_cmd = shutil.which("magick")
    if not magick_cmd:
        raise FileNotFoundError("ImageMagick (`magick`) не найден.")
    return magick_cmd


# --- разбор заголовка и image resources -------------------------------------

def _read_header(path: Path) -> dict:
    with path.open("rb") as handle:
        head = handle.read(26)
        if len(head) < 26 or head[:4] != PSD_SIGNATURE:
            raise PsdError(f"Файл не является PSD: {path.name}")
        _sig, version, _reserved, channels, height, width, depth, mode = struct.unpack(
            ">4sH6sHIIHH", head
        )
        if version not in (1, 2):
            raise PsdError(f"Неизвестная версия PSD ({version}): {path.name}")
        (color_mode_len,) = struct.unpack(">I", handle.read(4))
        handle.seek(color_mode_len, os.SEEK_CUR)
        (resources_len,) = struct.unpack(">I", handle.read(4))
        resources = handle.read(resources_len)
    return {
        "is_psb": version == 2,
        "channels": channels,
        "height": height,
        "width": width,
        "depth": depth,
        "mode": mode,
        "resources": _parse_resources(resources),
    }


def _parse_resources(blob: bytes) -> dict[int, bytes]:
    """Return ``{resource_id: data}`` for the 8BIM image resource blocks."""
    resources: dict[int, bytes] = {}
    pos = 0
    end = len(blob)
    while pos + 11 <= end:
        if blob[pos:pos + 4] != b"8BIM":
            break
        (resource_id,) = struct.unpack(">H", blob[pos + 4:pos + 6])
        pos += 6
        name_len = blob[pos]
        pos += 1 + name_len
        if pos % 2:
            pos += 1
        if pos + 4 > end:
            break
        (size,) = struct.unpack(">I", blob[pos:pos + 4])
        pos += 4
        resources[resource_id] = blob[pos:pos + size]
        pos += size + (size % 2)
    return resources


def _dpi_from_resources(resources: dict[int, bytes]) -> float:
    data = resources.get(RESOURCE_RESOLUTION_INFO)
    if not data or len(data) < 16:
        return 0.0
    h_res_fixed, h_unit, _w_unit, v_res_fixed, v_unit, _h_unit = struct.unpack(">IHHIHH", data[:16])
    h_res = h_res_fixed / 65536.0
    v_res = v_res_fixed / 65536.0
    # unit 1 = pixels per inch, 2 = pixels per cm
    if h_unit == 2:
        h_res *= 2.54
    if v_unit == 2:
        v_res *= 2.54
    return round(max(h_res, v_res), 3)


def _merged_flag(resources: dict[int, bytes]) -> bool | None:
    data = resources.get(RESOURCE_VERSION_INFO)
    if data is None or len(data) < 5:
        return None
    return bool(data[4])


# --- ImageMagick ------------------------------------------------------------

_IDENTIFY_FORMAT = (
    "%s\t%w\t%h\t%[colorspace]\t%[channels]\t%[resolution.x]\t%[units]"
    "\t%[psd:layer.visible]\t%[compose]\t%[label]\n"
)


def _identify_scenes(path: Path) -> list[dict]:
    cmd = [_magick(), "identify", "-quiet", "-format", _IDENTIFY_FORMAT, str(path)]
    result = run_command(cmd, capture_output=True, text=True, errors="replace", check=True)
    scenes = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 10:
            continue
        scene, width, height, colorspace, channels, res_x, units, visible, compose, label = parts[:10]
        channel_name = channels.split()[0] if channels.split() else ""
        scenes.append({
            "scene": int(scene),
            "width": int(width),
            "height": int(height),
            "colorspace": colorspace,
            "has_alpha": channel_name.endswith("a") and len(channel_name) > 1,
            "resolution": float(res_x or 0.0),
            "units": units,
            "visible": visible.strip(),
            "compose": compose,
            "label": label,
        })
    if not scenes:
        raise PsdError(f"ImageMagick не распознал ни одной сцены в {path.name}")
    return scenes


def _dpi_from_scene(scene: dict) -> float:
    res = scene["resolution"]
    if res <= 0:
        return 0.0
    if scene["units"] == "PixelsPerCentimeter":
        return round(res * 2.54, 3)
    return round(res, 3)


def inspect_psd(path: str | os.PathLike[str]) -> PsdStructure:
    """Describe PSD header/resources and decoded ImageMagick layer scenes."""
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"PSD не найден: {source}")
    header = _read_header(source)
    scenes = _identify_scenes(source)
    composite = scenes[0]
    layers = tuple(
        PsdLayer(
            index=scene["scene"],
            name=scene["label"],
            visible=scene["visible"] != "0",
            compose=scene["compose"],
            width_px=scene["width"],
            height_px=scene["height"],
            has_alpha=scene["has_alpha"],
        )
        for scene in scenes[1:]
    )
    visible = sum(1 for layer in layers if layer.visible)
    warnings: list[str] = []
    dpi = _dpi_from_resources(header["resources"]) or _dpi_from_scene(composite)
    if dpi <= 0:
        warnings.append("PSD не содержит разрешения (ресурс 1005); DPI неизвестен")
    merged = _merged_flag(header["resources"])
    if merged is False:
        warnings.append(
            "PSD сохранён без Maximize Compatibility: сведённый композит отсутствует, "
            "результат будет собран из слоёв без эффектов и корректирующих слоёв"
        )
    mode_name = COLOR_MODES.get(header["mode"], f"mode{header['mode']}")
    if mode_name in ("Lab", "Duotone", "Multichannel", "Indexed", "Bitmap"):
        warnings.append(f"Режим цвета {mode_name} не поддерживается печатным конвейером")
    expected_channels = {"RGB": 3, "CMYK": 4, "Grayscale": 1}.get(mode_name)
    if expected_channels is not None and header["channels"] > expected_channels + 1:
        warnings.append(
            f"PSD содержит {header['channels'] - expected_channels} дополнительных каналов "
            "(spot/alpha); ImageMagick читает их как альфа"
        )
    return PsdStructure(
        layer_count=len(layers),
        visible_layer_count=visible,
        hidden_layer_count=len(layers) - visible,
        colorspace=composite["colorspace"],
        has_alpha=composite["has_alpha"],
        width_px=header["width"],
        height_px=header["height"],
        dpi=dpi,
        depth_bits=header["depth"],
        channels=header["channels"],
        is_psb=header["is_psb"],
        has_icc_profile=RESOURCE_ICC_PROFILE in header["resources"],
        has_merged_composite=merged,
        layers=layers,
        warnings=tuple(warnings),
    )


def choose_strategy(structure: PsdStructure) -> str:
    """Photoshop composite when it is trustworthy, otherwise merge visible layers."""
    if structure.has_merged_composite is True:
        return STRATEGY_COMPOSITE
    return STRATEGY_MERGE_VISIBLE


def flatten_psd(
    source: str | os.PathLike[str],
    target_tiff: str | os.PathLike[str],
    *,
    strategy: str | None = None,
    compression: str = "zip",
) -> FlattenResult:
    """Write a flat TIFF with the visible content of a PSD.

    ``composite`` copies scene 0 (the composite Photoshop itself rendered:
    honours hidden groups, clipping masks, adjustment layers and effects).
    ``merge_visible`` lets ImageMagick composite scenes 1..n over scene 0,
    skipping layers Photoshop marked hidden (``compose=None``).  Colorspace
    and ICC are passed through untouched; DPI is re-asserted from the PSD.
    """
    source_path = Path(source)
    target_path = Path(target_tiff)
    if source_path.resolve() == target_path.resolve():
        raise ValueError("Сведение PSD не должно заменять исходный файл")
    if target_path.suffix.lower() not in (".tif", ".tiff"):
        raise ValueError("Результат сведения PSD должен быть TIFF (.tif/.tiff)")
    structure = inspect_psd(source_path)
    chosen = strategy or choose_strategy(structure)
    if chosen not in (STRATEGY_COMPOSITE, STRATEGY_MERGE_VISIBLE):
        raise ValueError(f"Неизвестная стратегия сведения: {chosen}")
    target_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [_magick(), "-quiet"]
    if chosen == STRATEGY_COMPOSITE:
        cmd.append(f"{source_path}[0]")
    else:
        cmd += [str(source_path), "-flatten"]
        if not structure.has_alpha:
            # Холст непрозрачен: альфа после -flatten всюду равна 1, канал лишний.
            cmd += ["-alpha", "off"]
    if structure.dpi > 0:
        cmd += ["-units", "PixelsPerInch", "-density", f"{structure.dpi:g}"]
    cmd += ["-compress", compression, str(target_path)]
    run_command(cmd, check=True)

    verify = run_command(
        [_magick(), "identify", "-quiet", "-format", "%w\t%h\t%[colorspace]\t%[channels]\t%[resolution.x]\t%[units]", str(target_path)],
        capture_output=True, text=True, errors="replace", check=True,
    )
    width, height, colorspace, channels, res_x, units = verify.stdout.strip().split("\t")[:6]
    channel_name = channels.split()[0] if channels.split() else ""
    out_dpi = float(res_x or 0.0)
    if units == "PixelsPerCentimeter":
        out_dpi *= 2.54
    warnings = list(structure.warnings)
    if chosen == STRATEGY_MERGE_VISIBLE:
        warnings.append(
            "Экспериментальное сведение ImageMagick: группы, маски, эффекты и "
            "корректирующие слои требуют визуальной проверки"
        )
    if colorspace != structure.colorspace:
        raise PsdError(
            f"Цветовое пространство изменилось при сведении: {structure.colorspace} → {colorspace}"
        )
    if (int(width), int(height)) != (structure.width_px, structure.height_px):
        raise PsdError("Размер сведённого TIFF не совпадает с холстом PSD")
    return FlattenResult(
        source=str(source_path),
        target=str(target_path),
        strategy=chosen,
        width_px=int(width),
        height_px=int(height),
        colorspace=colorspace,
        has_alpha=channel_name.endswith("a") and len(channel_name) > 1,
        dpi=round(out_dpi, 3),
        hidden_layers_skipped=structure.hidden_layer_count,
        warnings=tuple(warnings),
    )
