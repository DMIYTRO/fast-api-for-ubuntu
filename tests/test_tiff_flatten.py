"""Real TIFF Photoshop layers and IFD pages are deliberately different fixtures."""
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from core.inspector import inspect_tiff_structure
from core.tiff_flatten import flatten_visible_tiff
from processing.batch_processor import BatchProcessor
from tests.fake_callas import FakeCallasMerger
from processing.models import FileCheck, ParsedFilename
from services.dto import file_check_to_dto

MAGICK = shutil.which("magick")
pytestmark = pytest.mark.skipif(not MAGICK, reason="requires ImageMagick")


def magick(*args):
    return subprocess.run([MAGICK, *map(str, args)], check=True, capture_output=True, text=True).stdout


def layered_tiff(path: Path):
    # Composite is cyan, exactly the visible layer. The distinct magenta layer
    # is hidden by flipping Photoshop's actual layer flag, not by mocking IM.
    magick("-size", "100x80", "xc:cyan", "-set", "label", "composite",
           "(", "-size", "100x80", "xc:cyan", "-set", "label", "visible", ")",
           "(", "-size", "100x80", "xc:magenta", "-set", "label", "hidden", ")",
           "-colorspace", "sRGB", "-type", "TrueColor", "-depth", "8",
           "-units", "PixelsPerInch", "-density", "300",
           "-define", "tiff:write-layers=true", path)
    data = bytearray(path.read_bytes())
    records = list(re.finditer(rb"(?:8BIMnorm|MIB8mron)", data))
    assert len(records) == 2
    data[records[-1].start() + 10] |= 2
    path.write_bytes(data)


def test_saved_visible_composite_is_flat_and_source_untouched(tmp_path):
    source = tmp_path / "layered.tif"
    layered_tiff(source)
    original = source.read_bytes()
    structure = inspect_tiff_structure(str(source))
    assert structure.has_unflattened_layers and structure.layer_count == 2
    result = flatten_visible_tiff(source, tmp_path / "flat.tif")
    assert result.visible_layer_count == result.hidden_layer_count == 1
    assert source.read_bytes() == original
    flat = inspect_tiff_structure(str(tmp_path / "flat.tif"))
    assert flat.page_count == 1 and not flat.has_unflattened_layers
    assert magick(tmp_path / "flat.tif", "-format", "%[pixel:p{10,10}]", "info:") == "srgb(0,255,255)"
    assert magick(tmp_path / "flat.tif", "-format", "%x %y %[units]", "info:") == "300 300 PixelsPerInch"


def test_real_multipage_is_not_flattened(tmp_path):
    source = tmp_path / "pages.tif"
    magick("-size", "20x20", "xc:cyan", "xc:magenta", source)
    original = source.read_bytes()
    assert inspect_tiff_structure(str(source)).page_count == 2
    with pytest.raises(ValueError, match="Многостраничный"):
        flatten_visible_tiff(source, tmp_path / "flat.tif")
    assert not (tmp_path / "flat.tif").exists()
    assert source.read_bytes() == original


def test_preparation_uses_flattened_copy_and_exposes_success(tmp_path):
    source = tmp_path / "job_(5-1)_face.tif"
    layered_tiff(source)
    item = FileCheck(path=source, parsed=ParsedFilename("1", "5", 4, 3, 4, 0, "face"), has_unflattened_layers=True, tiff_layer_count=2,
                     actual_width_mm=8.4667, actual_height_mm=6.7733, dpi_x=300, dpi_y=300)
    processor = BatchProcessor(tmp_path, tmp_path / "PDF", pdf_merger=FakeCallasMerger())
    refs = processor._build_page_refs([item])
    work = tmp_path / "PDF" / "temp"
    work.mkdir(parents=True)
    outputs = processor._prepare_page_pdfs(refs, work)
    assert Path(outputs[0]).is_file()
    dto = file_check_to_dto(item)
    assert dto["tiff_flattened"] is True
    assert dto["tiff_visible_layer_count"] == dto["tiff_hidden_layer_count"] == 1
    assert (work / "flattened_1.tif").is_file()


def test_failed_flatten_does_not_report_success(tmp_path):
    source = tmp_path / "broken.tif"
    source.write_bytes(b"bad")
    item = FileCheck(path=source, parsed=ParsedFilename("1", "5", 4, 3, 4, 0, "face"), has_unflattened_layers=True, tiff_layer_count=2,
                     actual_width_mm=10, actual_height_mm=10)
    processor = BatchProcessor(tmp_path, tmp_path / "PDF", pdf_merger=FakeCallasMerger())
    with pytest.raises(Exception):
        processor._prepare_page_pdfs(processor._build_page_refs([item]), tmp_path)
    assert not item.tiff_flattened


def test_flatten_preserves_cmyk_depth_and_icc(tmp_path):
    profile = Path('/usr/share/color/icc/colord/GRACoL_TR006_coated.icc')
    if not profile.is_file():
        pytest.skip('optional system CMYK ICC profile unavailable')
    source, target = tmp_path / 'cmyk.tif', tmp_path / 'flat.tif'
    magick('-size', '20x20', 'xc:cmyk(40%,20%,10%,5%)', '-colorspace', 'CMYK',
           '(', '-size', '20x20', 'xc:cmyk(40%,20%,10%,5%)', '-colorspace', 'CMYK', ')',
           '-profile', profile, '-depth', '16', '-units', 'PixelsPerInch', '-density', '300',
           '-define', 'tiff:write-layers=true', source)
    flatten_visible_tiff(source, target)
    assert magick(target, '-format', '%[colorspace] %[depth] %x %y', 'info:') == 'CMYK 16 300 300'
    source_icc = subprocess.run([MAGICK, f'{source}[0]', 'icc:-'], check=True, capture_output=True).stdout
    target_icc = subprocess.run([MAGICK, str(target), 'icc:-'], check=True, capture_output=True).stdout
    assert source_icc and source_icc == target_icc


def test_diagnostic_pdf_uses_visible_tiff_composite_despite_local_findings(tmp_path):
    from processing.models import OrderCheck
    source = tmp_path / "face.tif"
    layered_tiff(source)
    original = source.read_bytes()
    item = FileCheck(path=source, parsed=ParsedFilename("1", "5", 4, 3, 4, 0, "face"), has_unflattened_layers=True, tiff_layer_count=2, dpi_x=300, dpi_y=300, errors=["Низкое разрешение"])
    processor = BatchProcessor(tmp_path, tmp_path / "PDF", pdf_merger=FakeCallasMerger())
    pdf, note = processor.create_diagnostic_pdf(OrderCheck("5", "1", [item]), tmp_path / "PDF" / "Failed")
    assert pdf.is_file() and note is None
    assert item.tiff_flattened
    assert item.tiff_hidden_layer_count == 1
    assert source.read_bytes() == original
