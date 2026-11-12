import shutil
import subprocess
from pathlib import Path

import pymupdf
import pytest

from core.pdf_exporter import convert_image_to_pdf

CMYK_PROFILE = Path("/usr/share/color/icc/ghostscript/default_cmyk.icc")

pytestmark = pytest.mark.skipif(
    shutil.which("magick") is None or not CMYK_PROFILE.is_file(),
    reason="нужны ImageMagick и CMYK ICC-профиль Ghostscript",
)


def _image_colorspace(pdf_path: Path) -> str:
    doc = pymupdf.open(pdf_path)
    try:
        return doc[0].get_images(full=True)[0][5]
    finally:
        doc.close()


def test_strip_icc_removes_iccbased_tag_and_keeps_default(tmp_path):
    tagged = tmp_path / "tagged.jpg"
    subprocess.run(
        [
            "magick", "-size", "40x40", "xc:rgb(10,200,30)", "-colorspace", "CMYK",
            "-profile", str(CMYK_PROFILE), str(tagged),
        ],
        check=True,
    )
    kept = tmp_path / "kept.pdf"
    stripped = tmp_path / "stripped.pdf"

    convert_image_to_pdf(str(tagged), str(kept), dpi="300")
    convert_image_to_pdf(str(tagged), str(stripped), dpi="300", strip_icc=True)

    assert _image_colorspace(kept) == "ICCBased"
    assert _image_colorspace(stripped) == "DeviceCMYK"
