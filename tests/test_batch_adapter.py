from pathlib import Path
from datetime import UTC, datetime
import threading
import time

from processing.models import FileCheck, OrderCheck
from services.batch_adapter import BatchProcessorAdapter, ProcessingOptions
from processing.batch_processor import BatchProcessor
from tests.fake_callas import FakeCallasMerger


def _fake_callas_processor(*args, **kwargs):
    return BatchProcessor(*args, pdf_merger=FakeCallasMerger(), **kwargs)
from services.pitstop import (
    PitStopCheckResult,
    PitStopCounts,
    PitStopExecutionStatus,
    PitStopReport,
)


class PreviewOnlyProcessor:
    def __init__(self, *_args, **_kwargs):
        self.preview_calls = []

    def generate_previews_for_files(self, files, preview_dir):
        self.preview_calls.append((list(files), preview_dir))
        return [
            (
                file_check,
                [preview_dir / f"{file_check.path.stem}_preview.png"],
                None,
            )
            for file_check in files
        ]

    def copy_failed_to_troubles(self, *_args):
        return []


def test_invalid_order_still_generates_preview_for_every_source_file(tmp_path: Path):
    first = FileCheck(path=tmp_path / "first-face.jpg")
    second = FileCheck(path=tmp_path / "second-back.pdf")
    first.errors.append("недостаточное разрешение")
    order = OrderCheck("100", "42", files=[first, second])
    adapter = BatchProcessorAdapter(
        ProcessingOptions(input_path=str(tmp_path), direction="digital"),
        processor_factory=PreviewOnlyProcessor,
    )

    artifacts = adapter.process_order(order)

    assert [path.name for path in artifacts.preview_paths] == [
        "first-face_preview.png",
        "second-back_preview.png",
    ]
    assert adapter.processor.preview_calls == [([first, second], tmp_path / "Previews")]


class PitStopPipelineProcessor(PreviewOnlyProcessor):
    def __init__(self, *_args, **_kwargs):
        super().__init__()
        self.calls = []

    def generate_previews_for_files(self, files, preview_dir):
        self.calls.append("source_preview")
        return super().generate_previews_for_files(files, preview_dir)

    def create_pdfs(self, orders):
        self.calls.append("pdf")
        pdf_path = Path(orders[0].files[0].path.parent) / "PDF" / "order.pdf"
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.write_bytes(b"%PDF-test")
        return [(orders[0], pdf_path, None)]

    def generate_previews_for_all(self, pdf_paths, preview_dir, **_kwargs):
        self.calls.append("production_preview")
        preview = preview_dir / "order_preview.png"
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_bytes(b"preview")
        return [(pdf_paths[0], [preview], None)]


class FakePitStopService:
    def __init__(self, processor):
        self.processor = processor

    def check_pdf(self, pdf_path, *, profile_id):
        self.processor.calls.append("pitstop")
        return PitStopCheckResult(
            status=PitStopExecutionStatus.COMPLETED,
            profile_id=profile_id,
            input_pdf=pdf_path,
            checked_at=datetime.now(UTC),
            input_sha256="a" * 64,
            report=PitStopReport(counts=PitStopCounts(warnings=1), pages=1),
            job_id="check-1",
        )


def test_valid_order_runs_pdf_then_pitstop_then_production_preview(tmp_path: Path):
    item = FileCheck(path=tmp_path / "order-face.jpg")
    order = OrderCheck("100", "42", files=[item])
    processor = PitStopPipelineProcessor()
    adapter = BatchProcessorAdapter(
        ProcessingOptions(input_path=str(tmp_path), direction="digital"),
        processor_factory=lambda *_args, **_kwargs: processor,
        pitstop_service=FakePitStopService(processor),
    )

    artifacts = adapter.process_order(order)

    assert processor.calls == ["pdf", "pitstop", "production_preview"]
    assert artifacts.pitstop["verdict"] == "warning"
    assert artifacts.current_pdf_revision == 1
    assert artifacts.current_pdf_sha256 == "a" * 64
    assert "Final" in str(artifacts.preview_paths[0])


def test_ink_coverage_runs_after_pitstop_and_is_attached_to_artifacts(tmp_path: Path):
    item = FileCheck(path=tmp_path / "order-face.jpg")
    order = OrderCheck("100", "42", files=[item])
    processor = PitStopPipelineProcessor()
    seen = []

    def checker(pdf_path, direction):
        processor.calls.append("ink_coverage")
        seen.append((pdf_path, direction))
        return {
            "status": "completed",
            "flags": [{"kind": "overflow", "page": 2, "percent": 262.0, "threshold": 250.0}],
        }

    adapter = BatchProcessorAdapter(
        ProcessingOptions(input_path=str(tmp_path), direction="offset"),
        processor_factory=lambda *_args, **_kwargs: processor,
        pitstop_service=FakePitStopService(processor),
        ink_coverage_checker=checker,
    )

    artifacts = adapter.process_order(order)

    assert processor.calls == ["pdf", "pitstop", "production_preview", "ink_coverage"]
    assert seen == [(tmp_path / "PDF" / "order.pdf", "offset")]
    assert artifacts.ink_coverage["flags"][0]["kind"] == "overflow"
    assert artifacts.errors == []
    assert artifacts.pitstop["verdict"] == "warning"


def test_ink_coverage_failure_never_fails_the_order(tmp_path: Path):
    item = FileCheck(path=tmp_path / "order-face.jpg")
    order = OrderCheck("100", "42", files=[item])
    processor = PitStopPipelineProcessor()

    def checker(_pdf_path, _direction):
        raise RuntimeError("callas недоступен")

    adapter = BatchProcessorAdapter(
        ProcessingOptions(input_path=str(tmp_path), direction="digital"),
        processor_factory=lambda *_args, **_kwargs: processor,
        ink_coverage_checker=checker,
    )

    artifacts = adapter.process_order(order)

    assert artifacts.pdf_path is not None
    assert artifacts.errors == []
    assert artifacts.ink_coverage["status"] == "failed"
    assert "callas недоступен" in artifacts.ink_coverage["error"]
    assert artifacts.ink_coverage["flags"] == []


def test_without_checker_ink_coverage_is_absent(tmp_path: Path):
    item = FileCheck(path=tmp_path / "order-face.jpg")
    order = OrderCheck("100", "42", files=[item])
    processor = PitStopPipelineProcessor()
    adapter = BatchProcessorAdapter(
        ProcessingOptions(input_path=str(tmp_path), direction="digital"),
        processor_factory=lambda *_args, **_kwargs: processor,
    )

    artifacts = adapter.process_order(order)

    assert artifacts.ink_coverage is None
    assert "ink_coverage" not in processor.calls


class IncrementalProcessor:
    """Разбирает имена заранее, затем отдаёт заказы по одному."""

    def __init__(self, *_args, wait_for=None, **_kwargs):
        self.on_orders_grouped = None
        self.order_ids = ["100", "101"]
        self.wait_for = wait_for
        self.seen_postpress = {}

    def iter_inspect_orders(self):
        if self.on_orders_grouped is not None:
            self.on_orders_grouped(list(self.order_ids))
        if self.wait_for is not None:
            # Заказы выдаются только после старта запроса к Sborka.
            assert self.wait_for.wait(2)
        for order_id in self.order_ids:
            yield OrderCheck(order_id, "42")

    def create_pdfs(self, orders):
        self.seen_postpress[orders[0].order_id] = orders[0].postpress
        return [(orders[0], None, "stop")]


def _incremental_adapter(tmp_path: Path, fetcher, processor=None, **kwargs):
    processor = processor or IncrementalProcessor()
    adapter = BatchProcessorAdapter(
        ProcessingOptions(
            input_path=str(tmp_path),
            direction="digital",
            generate_previews=False,
            copy_failures=False,
        ),
        processor_factory=lambda *_a, **_k: processor,
        order_info_fetcher=fetcher,
        **kwargs,
    )
    return adapter, processor


def test_first_order_is_published_before_slow_sborka_fetch_finishes(tmp_path: Path):
    started, release = threading.Event(), threading.Event()
    requested = []

    def fetcher(order_ids):
        requested.append(list(order_ids))
        started.set()
        assert release.wait(5)
        return [{"id": "100", "post_text": "Сгиб: 1 пополам"}]

    adapter, processor = _incremental_adapter(
        tmp_path, fetcher, IncrementalProcessor(wait_for=started)
    )
    iterator = adapter.iter_inspect_orders()
    try:
        first = next(iterator)  # fetcher is still blocked here
        assert first.order_id == "100" and first.postpress is None
        assert not release.is_set()
        threading.Timer(0.2, release.set).start()
        began = time.monotonic()
        adapter.process_order(first)  # waits for Sborka, then sees postpress
        assert time.monotonic() - began >= 0.15
        assert processor.seen_postpress["100"]["fold"]["type"] == "half-fold"
        second = next(iterator)
        adapter.process_order(second)
        assert processor.seen_postpress["101"] is None
        # One request for all order ids, issued before inspection.
        assert requested == [["100", "101"]]
    finally:
        release.set()


def test_postpress_already_loaded_is_set_on_published_order(tmp_path: Path):
    adapter, _ = _incremental_adapter(
        tmp_path,
        lambda order_ids: [{"id": "100", "post_text": "Сгиб: 2 намотка"}],
    )
    # The processor inspects "slowly": the fetch is complete before the first order.
    adapter.processor.wait_for = threading.Event()
    adapter.processor.wait_for.set()
    original = adapter.processor.iter_inspect_orders

    def slow_inspect():
        for order in original():
            adapter._order_info_future.result(timeout=2)
            yield order

    adapter.processor.iter_inspect_orders = slow_inspect
    iterator = adapter.iter_inspect_orders()
    first = next(iterator)
    assert first.postpress["fold"]["type"] == "c-fold"  # applied without process_order


def test_sborka_failure_is_logged_and_does_not_block_processing(tmp_path: Path, caplog):
    def fetcher(order_ids):
        raise RuntimeError("sborka down")

    adapter, processor = _incremental_adapter(tmp_path, fetcher)
    with caplog.at_level("WARNING", logger="image_magic.processing"):
        orders = list(adapter.iter_inspect_orders())
        for order in orders:
            artifacts = adapter.process_order(order)
            assert artifacts.errors == ["PDF: stop"]
    assert [order.postpress for order in orders] == [None, None]
    assert processor.seen_postpress == {"100": None, "101": None}
    assert [r.getMessage() for r in caplog.records].count("sborka.orderinfo_failed error=sborka down") == 1


def test_sborka_timeout_does_not_block_processing(tmp_path: Path, caplog):
    release = threading.Event()

    def fetcher(order_ids):
        release.wait(5)
        return [{"id": "100", "post_text": "Сгиб: 1 пополам"}]

    adapter, processor = _incremental_adapter(
        tmp_path, fetcher, order_info_timeout_seconds=0.1
    )
    try:
        with caplog.at_level("WARNING", logger="image_magic.processing"):
            began = time.monotonic()
            for order in adapter.iter_inspect_orders():
                adapter.process_order(order)
            assert time.monotonic() - began < 2
    finally:
        release.set()
    assert processor.seen_postpress == {"100": None, "101": None}
    assert sum("sborka.orderinfo_failed" in r.getMessage() for r in caplog.records) == 1


def test_without_fetcher_orders_pass_through_without_background_work(tmp_path: Path):
    adapter, processor = _incremental_adapter(tmp_path, None)
    orders = list(adapter.iter_inspect_orders())
    for order in orders:
        adapter.process_order(order)
    assert [order.order_id for order in orders] == ["100", "101"]
    assert adapter._order_info_future is None
    assert processor.seen_postpress == {"100": None, "101": None}


def test_first_stage_findings_still_reach_pitstop_and_rework(tmp_path):
    import pymupdf
    source = tmp_path / "job_(90x50)_4-0_(1-987)_input.pdf"
    with pymupdf.open() as document:
        page = document.new_page(width=100, height=100)  # Wrong physical size.
        page.insert_text((10, 30), "diagnostic")
        document.save(source)
    class Checker:
        calls = []
        def check_pdf(self, path, *, profile_id):
            self.calls.append(path)
            return PitStopCheckResult(
                status=PitStopExecutionStatus.COMPLETED, profile_id=profile_id,
                input_pdf=path, input_sha256="a" * 64, checked_at=datetime.now(UTC),
                report=PitStopReport(counts=PitStopCounts(errors=1), pages=1),
            )
    checker = Checker()
    adapter = BatchProcessorAdapter(
        ProcessingOptions(str(tmp_path), "digital", generate_previews=False, copy_failures=False),
        pitstop_service=checker,
        processor_factory=_fake_callas_processor,
    )
    order = adapter.scan_and_inspect()[0]
    assert not order.passed
    findings = list(order.files[0].errors)
    result = adapter.process_order(order)
    assert checker.calls
    assert result.pitstop["counts"]["errors"] == 1
    assert order.files[0].errors == findings
    assert result.pdf_path.parent.name == "Rework"
    assert not list((tmp_path / "PDF" / "Print").glob("*.pdf"))
    from services.coordinator import _completed_order_status
    assert _completed_order_status(order, result) == "warning"


def test_corrupt_first_stage_pdf_explains_why_pitstop_skipped(tmp_path):
    source = tmp_path / "job_(90x50)_4-0_(1-988)_input.pdf"
    source.write_bytes(b"not a PDF")
    adapter = BatchProcessorAdapter(
        ProcessingOptions(str(tmp_path), "digital", generate_previews=False, copy_failures=False),
    )
    order = adapter.scan_and_inspect()[0]
    result = adapter.process_order(order)
    assert result.pdf_path is None
    assert "не удалось прочитать" in result.pitstop_skipped_reason
    assert result.pitstop is None


def test_readable_raster_with_local_findings_builds_diagnostic_with_real_tools(tmp_path):
    import subprocess
    source = tmp_path / "job_(90x50)_4-0_(1-989)_face.tif"
    subprocess.run(["magick", "-size", "100x100", "xc:cyan", "-colorspace", "CMYK", "-units", "PixelsPerInch", "-density", "72", str(source)], check=True)
    before = source.read_bytes()
    adapter = BatchProcessorAdapter(
        ProcessingOptions(str(tmp_path), "digital", generate_previews=False, copy_failures=False),
        processor_factory=_fake_callas_processor,
    )
    order = adapter.scan_and_inspect()[0]
    assert not order.passed
    result = adapter.process_order(order)
    assert result.pdf_path is not None, result.pitstop_skipped_reason
    assert result.pdf_path.parent.name == "Rework"
    assert source.read_bytes() == before
    assert "не настроена" in result.pitstop_skipped_reason
