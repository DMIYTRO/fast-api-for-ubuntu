"""``CallasClient.ink_coverage`` на fake HTTP (httpx.MockTransport); живая служба не используется."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from services.callas import CallasConfigError, CallasJobFailedError, CallasProtocolError
from tests.test_callas_client import FakeCallas, make_client
from tests.test_ink_coverage import SERVICE_RESPONSE


@pytest.fixture()
def share(tmp_path: Path) -> Path:
    root = tmp_path / "share"
    root.mkdir()
    return root


@pytest.fixture()
def pdf(tmp_path: Path) -> Path:
    path = tmp_path / "order (99x210).pdf"
    path.write_bytes(b"%PDF-1.4 synthetic")
    return path


def test_ink_coverage_stages_input_sends_spec_and_parses_block(share: Path, pdf: Path):
    fake = FakeCallas(share)
    fake.get_script = [
        httpx.Response(200, json={"status": "running", "files": []}),
        httpx.Response(200, json=SERVICE_RESPONSE),
    ]
    client = make_client(share, fake)

    result = client.ink_coverage(pdf, job_id="ink-1", pages="1-2", resolution=72, box="trimbox")

    spec = fake.specs["ink-1"]
    assert spec["type"] == "inkcoverage"
    assert spec["pages"] == "1-2" and spec["resolution"] == 72 and spec["box"] == "TrimBox"
    assert spec["input"]["path"] == f"{fake.server_share}/.callas/ink-1/in/0001.pdf"
    assert (share / ".callas" / "ink-1" / "in" / "0001.pdf").read_bytes() == pdf.read_bytes()
    assert "out_dir" not in spec and "keep_report" not in spec
    assert fake.methods() == ["PUT", "GET", "GET"]
    assert result.status == "completed"
    assert (result.document_min_percent, result.min_page) == (11.092, 1)
    assert (result.document_max_percent, result.max_page) == (400.0, 2)
    assert result.resolution == 72 and result.box == "TrimBox"


def test_ink_coverage_default_job_id_is_deterministic(share: Path, pdf: Path):
    fake = FakeCallas(share)
    fake.get_script = [httpx.Response(200, json=SERVICE_RESPONSE)] * 2
    client = make_client(share, fake)

    client.ink_coverage(pdf)
    client.ink_coverage(pdf)

    ids = [r.url.path.rsplit("/", 1)[-1] for r in fake.requests if r.method == "PUT"]
    assert len(ids) == 2 and ids[0] == ids[1] and ids[0].startswith("callas-ink-")


def test_ink_coverage_rejects_bad_parameters(share: Path, pdf: Path):
    client = make_client(share, FakeCallas(share))
    with pytest.raises(CallasConfigError):
        client.ink_coverage(pdf, resolution=0, job_id="ink-1")
    with pytest.raises(CallasConfigError):
        client.ink_coverage(pdf, box="PageBox", job_id="ink-1")
    with pytest.raises(CallasConfigError):
        client.ink_coverage(pdf, pages="all;drop", job_id="ink-1")
    with pytest.raises(CallasConfigError):
        client.ink_coverage(share / "missing.pdf", job_id="ink-1")


def test_ink_coverage_missing_block_is_protocol_error(share: Path, pdf: Path):
    fake = FakeCallas(share)
    fake.get_script = [httpx.Response(200, json={"status": "done", "files": []})]
    with pytest.raises(CallasProtocolError):
        make_client(share, fake).ink_coverage(pdf, job_id="ink-1")


def test_ink_coverage_failed_job_maps_to_job_failed_error(share: Path, pdf: Path):
    fake = FakeCallas(share)
    fake.get_script = [
        httpx.Response(
            200,
            json={"status": "failed", "files": [], "error": "range_invalid", "error_class": "ValueError"},
        )
    ]
    with pytest.raises(CallasJobFailedError) as info:
        make_client(share, fake).ink_coverage(pdf, job_id="ink-1", pages="9-12")
    assert "range_invalid" in str(info.value)
    assert json.loads(fake.requests[0].content)["pages"] == "9-12"
