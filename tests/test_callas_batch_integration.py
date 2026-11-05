"""BatchProcessor использует callas только когда он включён (fake-клиент, без сети)."""
from pathlib import Path
import shutil

import pymupdf
import pytest

from processing.batch_processor import BatchProcessor


class FakeCallas:
    def __init__(self, root: Path):
        self.root = root
        self.calls: list[str] = []
        self.deleted: list[str] = []

    def job_dir(self, job_id: str) -> Path:
        return self.root / job_id

    def delete(self, job_id: str) -> None:
        self.deleted.append(job_id)

    def merge(self, inputs, output_name, *, job_id, **_):
        self.calls.append("merge")
        out = self.job_dir(job_id) / "out"
        out.mkdir(parents=True)
        doc = pymupdf.open()
        for item in inputs:
            src = pymupdf.open(item)
            doc.insert_pdf(src)
            src.close()
        doc.save(out / output_name)
        doc.close()
        return out / output_name

    def render(self, input, dpi, *, job_id, **_):
        self.calls.append("render")
        out = self.job_dir(job_id) / "out"
        out.mkdir(parents=True)
        pages = []
        for number in range(pymupdf.open(input).page_count):
            page = out / f"page-{number + 1:03d}.png"
            pymupdf.open(input)[number].get_pixmap(dpi=36).save(page)
            pages.append(page)
        return pages


@pytest.mark.skipif(not shutil.which("magick"), reason="нужен ImageMagick")
def test_merge_and_render_go_through_callas_when_enabled(tmp_path):
    import subprocess

    inp = tmp_path / "in"
    inp.mkdir()
    for side in ("face", "back"):
        subprocess.run(
            ["magick", "-size", "1111x638", "xc:#b7c5d8", "-colorspace", "CMYK",
             "-units", "PixelsPerInch", "-density", "300",
             str(inp / f"T_(90x50)_4-4_(5-1)_{side}.tif")],
            check=True, capture_output=True,
        )
    fake = FakeCallas(tmp_path / "share")
    proc = BatchProcessor(inp, inp / "PDF", callas_client=fake, callas_merge=True, callas_render=True)
    orders = proc.inspect_orders()
    results = proc.create_pdfs(orders)
    assert [err for _, _, err in results] == [None]
    pdf = results[0][1]
    assert pymupdf.open(pdf).page_count == 2
    previews = proc.generate_previews_for_all([pdf], inp / "Previews")
    assert previews[0][2] is None and len(previews[0][1]) == 2
    assert fake.calls == ["merge", "render"]
    assert len(fake.deleted) == 2
    assert not any(fake.root.glob("*"))  # папки заданий очищены


def test_callas_is_off_by_default():
    proc = BatchProcessor(Path("."), Path("."), callas_client=object())
    assert not proc.callas_merge and not proc.callas_render
