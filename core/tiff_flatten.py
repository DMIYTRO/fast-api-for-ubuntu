"""Materialize Photoshop's saved visible composite without changing the source.

TIFF IFDs are pages; embedded Photoshop scenes are layers. Re-applying decoded
layers to the composite would apply blending twice. The saved composite is the
rendered visible appearance (including masks/effects) and is the print source.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil

from .inspector import inspect_tiff_structure
from .tool_runner import run_command


@dataclass(frozen=True)
class TiffFlattenResult:
    layer_count: int
    visible_layer_count: int
    hidden_layer_count: int
    strategy: str = "saved_visible_composite"


def flatten_visible_tiff(source: Path, target: Path) -> TiffFlattenResult:
    """Write one flat TIFF from a layered single-page TIFF's saved composite."""
    if source.resolve() == target.resolve():
        raise ValueError("Сведение TIFF не должно заменять исходный файл")
    structure = inspect_tiff_structure(str(source))
    if structure.page_count != 1:
        raise ValueError("Многостраничный TIFF нельзя сводить как слои")
    if not structure.has_unflattened_layers or not structure.layer_count:
        raise ValueError("В TIFF не найдены читаемые слои Photoshop")
    magick = shutil.which("magick")
    if not magick:
        raise FileNotFoundError("ImageMagick (`magick`) не найден")
    scenes = run_command(
        [magick, "identify", "-quiet", "-format", "%s\t%[psd:layer.visible]\n", str(source)],
        capture_output=True, text=True, errors="replace", check=True,
    ).stdout.splitlines()[1:]
    if len(scenes) != structure.layer_count:
        raise ValueError("Не удалось подтвердить число слоёв TIFF")
    visibility = [line.split("\t")[-1].strip() for line in scenes]
    if any(value not in {"0", "1"} for value in visibility):
        raise ValueError("ImageMagick не сообщил видимость слоёв TIFF")
    hidden = visibility.count("0")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        run_command([
            magick, "-quiet", "-define", "tiff:ignore-layers=true", f"{source}[0]",
            "+profile", "tiff:37724", "-define", "tiff:write-layers=false",
            "-compress", "zip", str(target),
        ], check=True)
        flattened = inspect_tiff_structure(str(target))
        if flattened.page_count != 1 or flattened.has_unflattened_layers:
            raise ValueError("ImageMagick не создал одностраничный сведённый TIFF")
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return TiffFlattenResult(structure.layer_count, len(scenes) - hidden, hidden)
