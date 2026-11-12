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
            pymupdf.open(input)[number].get_pixmap(dpi=int(dpi)).save(page)
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
    # merge, контрольный рендер итогового PDF, рендер превью — всё через callas.
    assert fake.calls == ["merge", "render", "render"]
    assert len(fake.deleted) == 3
    assert not any(fake.root.glob("*"))  # папки заданий очищены


def test_callas_client_always_provides_merger_even_without_merge_flag():
    proc = BatchProcessor(Path("."), Path("."), callas_client=FakeCallas(Path(".")))
    assert proc.pdf_merger is not None and proc.callas_render


def _one_page_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    doc.new_page(width=200, height=100)
    doc.save(path)
    doc.close()
    return path


def test_merge_without_callas_fails_with_actionable_error_and_no_pymupdf(tmp_path, monkeypatch):
    import core.pdf_exporter
    from processing.batch_processor import CALLAS_MERGE_UNAVAILABLE_MESSAGE
    from processing.models import OrderCheck

    def forbidden(*_a, **_k):
        raise AssertionError("PyMuPDF merge must not be used")

    monkeypatch.setattr(core.pdf_exporter, "merge_pdfs_with_pymupdf", forbidden)
    proc = BatchProcessor(tmp_path, tmp_path / "PDF")
    order = OrderCheck(order_id="1", customer_id="c", files=[])
    with pytest.raises(RuntimeError) as error:
        proc._merge_page_pdfs([_one_page_pdf(tmp_path / "a.pdf")], tmp_path / "out.pdf", order)
    assert str(error.value) == CALLAS_MERGE_UNAVAILABLE_MESSAGE
    assert "IMAGE_MAGIC_CALLAS_URL" in str(error.value)
    assert not (tmp_path / "out.pdf").exists()


def test_create_pdfs_without_callas_reports_error_per_order(tmp_path, monkeypatch):
    import core.pdf_exporter

    monkeypatch.setattr(
        core.pdf_exporter, "merge_pdfs_with_pymupdf",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("PyMuPDF merge used")),
    )
    inp = tmp_path / "in"
    inp.mkdir()
    _one_page_pdf(inp / "T_(70x30)_4-0_(5-2)_face.pdf")
    proc = BatchProcessor(inp, inp / "PDF")
    orders = proc.inspect_orders()
    results = proc.create_pdfs(orders)
    for _order, path, error in results:
        assert error and "callas pdfToolbox не настроен" in error
        assert not path.exists()


def test_merger_returning_nothing_is_rejected(tmp_path):
    from processing.models import OrderCheck

    proc = BatchProcessor(tmp_path, tmp_path / "PDF", pdf_merger=lambda parts, out, label: None)
    order = OrderCheck(order_id="1", customer_id="c", files=[])
    with pytest.raises(RuntimeError, match="не вернул объединённый PDF"):
        proc._merge_page_pdfs([_one_page_pdf(tmp_path / "a.pdf")], tmp_path / "out.pdf", order)


class StickyJobCallas(FakeCallas):
    """Как боевая служба: повторный job_id возвращает «done», но файлов уже нет."""

    def __init__(self, root: Path):
        super().__init__(root)
        self.seen_job_ids: list[str] = []

    def _register(self, job_id: str) -> None:
        if job_id in self.seen_job_ids:
            raise RuntimeError(f"Файл результата не появился в общей папке: {job_id}")
        self.seen_job_ids.append(job_id)

    def merge(self, inputs, output_name, *, job_id, **kwargs):
        self._register(job_id)
        return super().merge(inputs, output_name, job_id=job_id, **kwargs)

    def render(self, input, dpi, *, job_id, **kwargs):
        self._register(job_id)
        return super().render(input, dpi, job_id=job_id, **kwargs)


def _single_page_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    doc.new_page(width=100, height=100)
    doc.save(path)
    doc.close()
    return path


def test_repeated_callas_operations_use_unique_valid_job_ids(tmp_path):
    from types import SimpleNamespace
    import re

    fake = StickyJobCallas(tmp_path / "share")
    proc = BatchProcessor(
        tmp_path, tmp_path / "PDF", callas_client=fake, callas_merge=True, callas_render=True
    )
    pdf = _single_page_pdf(tmp_path / "order.pdf")
    order = SimpleNamespace(order_id="100")

    for index in range(2):
        proc._merge_page_pdfs([pdf], tmp_path / f"merged-{index}.pdf", order)
        pages_dir = tmp_path / f"pages-{index}"
        pages_dir.mkdir()
        proc._render_pdf_with_callas(pdf, 36, pages_dir)
        assert (pages_dir / "page-001.png").is_file()

    merge_ids = fake.seen_job_ids[0::2]
    render_ids = fake.seen_job_ids[1::2]
    assert len(set(fake.seen_job_ids)) == 4
    assert merge_ids[0] != merge_ids[1] and render_ids[0] != render_ids[1]
    assert all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", item) for item in fake.seen_job_ids)
    assert fake.deleted == fake.seen_job_ids
