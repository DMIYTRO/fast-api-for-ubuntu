from pathlib import Path
from datetime import UTC, datetime

from processing.models import FileCheck, OrderCheck
from services.batch_adapter import BatchProcessorAdapter, ProcessingOptions
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
