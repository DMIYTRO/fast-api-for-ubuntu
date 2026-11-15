"""The PDF revision digest is streamed and computed at most once per revision."""
from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

from processing.models import FileCheck, OrderCheck
from services import batch_adapter
from services.batch_adapter import BatchProcessorAdapter, ProcessingOptions
from tests.test_batch_adapter import FakePitStopService, PitStopPipelineProcessor


def _adapter(tmp_path: Path, processor, pitstop_service=None) -> BatchProcessorAdapter:
    return BatchProcessorAdapter(
        ProcessingOptions(input_path=str(tmp_path), direction="digital"),
        processor_factory=lambda *_a, **_k: processor,
        pitstop_service=pitstop_service,
    )


def _order(tmp_path: Path) -> OrderCheck:
    return OrderCheck("100", "42", files=[FileCheck(path=tmp_path / "order-face.jpg")])


class FailingPitStop:
    def check_pdf(self, pdf_path, *, profile_id):
        raise RuntimeError("pitstop down")


def test_sha256_file_streams_in_chunks_and_matches_hashlib(tmp_path):
    data = bytes(range(256)) * 20_000  # ~5 MB, several 1 MiB chunks
    path = tmp_path / "big.pdf"
    path.write_bytes(data)
    with patch.object(Path, "read_bytes", side_effect=AssertionError("whole file read")):
        assert batch_adapter._sha256_file(path) == hashlib.sha256(data).hexdigest()
        assert batch_adapter._sha256_file(path, chunk_size=7) == hashlib.sha256(data).hexdigest()
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    assert batch_adapter._sha256_file(empty) == hashlib.sha256(b"").hexdigest()


def test_pitstop_digest_is_used_without_hashing_the_pdf_again(tmp_path):
    processor = PitStopPipelineProcessor()
    adapter = _adapter(tmp_path, processor, FakePitStopService(processor))
    with patch.object(batch_adapter, "_sha256_file", side_effect=AssertionError("hashed twice")):
        artifacts = adapter.process_order(_order(tmp_path))
    assert artifacts.current_pdf_sha256 == "a" * 64


def test_without_pitstop_the_pdf_is_hashed_once(tmp_path):
    processor = PitStopPipelineProcessor()
    adapter = _adapter(tmp_path, processor)
    with patch.object(batch_adapter, "_sha256_file", wraps=batch_adapter._sha256_file) as spy:
        artifacts = adapter.process_order(_order(tmp_path))
    assert spy.call_count == 1
    assert artifacts.current_pdf_sha256 == hashlib.sha256(b"%PDF-test").hexdigest()
    assert artifacts.current_pdf_revision == 1


def test_failed_pitstop_still_records_the_digest_of_the_revision(tmp_path):
    processor = PitStopPipelineProcessor()
    adapter = _adapter(tmp_path, processor, FailingPitStop())
    artifacts = adapter.process_order(_order(tmp_path))
    assert artifacts.pdf_path is not None
    assert artifacts.current_pdf_sha256 == hashlib.sha256(b"%PDF-test").hexdigest()
    assert any("pitstop down" in error for error in artifacts.errors)


class DiagnosticProcessor(PitStopPipelineProcessor):
    def create_diagnostic_pdf(self, order, target_dir):
        target_dir.mkdir(parents=True, exist_ok=True)
        pdf = target_dir / "diagnostic.pdf"
        pdf.write_bytes(b"%PDF-diagnostic")
        return pdf, None


def _failed_order(tmp_path: Path) -> OrderCheck:
    order = _order(tmp_path)
    order.files[0].errors.append("недостаточное разрешение")
    return order


def test_diagnostic_pdf_digest_comes_from_pitstop_without_a_second_read(tmp_path):
    processor = DiagnosticProcessor()
    adapter = _adapter(tmp_path, processor, FakePitStopService(processor))
    with patch.object(batch_adapter, "_sha256_file", side_effect=AssertionError("hashed twice")):
        artifacts = adapter.process_order(_failed_order(tmp_path))
    assert artifacts.pdf_kind == "diagnostic"
    assert artifacts.current_pdf_sha256 == "a" * 64


def test_diagnostic_pdf_is_hashed_once_when_pitstop_is_absent_or_fails(tmp_path):
    expected = hashlib.sha256(b"%PDF-diagnostic").hexdigest()
    for service in (None, FailingPitStop()):
        adapter = _adapter(tmp_path, DiagnosticProcessor(), service)
        with patch.object(batch_adapter, "_sha256_file", wraps=batch_adapter._sha256_file) as spy:
            artifacts = adapter.process_order(_failed_order(tmp_path))
        assert spy.call_count == 1
        assert artifacts.current_pdf_sha256 == expected
        assert artifacts.current_pdf_revision == 1
