from pathlib import Path

from processing.models import FileCheck, OrderCheck
from services.batch_adapter import BatchProcessorAdapter, ProcessingOptions


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
