"""Задания callas info/split и адаптер извлечения страниц на fake HTTP; живая служба не используется."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from services.callas import (
    CallasConfigError,
    CallasProtocolError,
    CallasResultError,
    make_callas_page_extractor,
)
from tests.test_callas_client import FakeCallas, make_client, sha


@pytest.fixture()
def share(tmp_path: Path) -> Path:
    root = tmp_path / "share"
    root.mkdir()
    return root


@pytest.fixture()
def fake(share: Path) -> FakeCallas:
    return FakeCallas(share)


@pytest.fixture()
def pdf(tmp_path: Path) -> Path:
    path = tmp_path / "заказ.pdf"
    path.write_bytes(b"%PDF-1.4 two pages")
    return path


def test_info_returns_page_count_and_sends_info_spec(share, fake, pdf):
    fake.get_script = [
        httpx.Response(200, json={"status": "running"}),
        httpx.Response(200, json={"status": "done", "pages": 3}),
    ]
    client = make_client(share, fake)
    assert client.info(pdf, job_id="info-1") == 3
    assert fake.specs["info-1"] == {
        "type": "info",
        "input": {
            "path": f"{fake.server_share}/.callas/info-1/in/0001.pdf",
            "sha256": sha(pdf.read_bytes()),
        },
    }


def test_info_default_job_id_is_deterministic(share, fake, pdf):
    fake.get_script = [httpx.Response(200, json={"status": "done", "page_count": 2})] * 2
    client = make_client(share, fake)
    assert client.info(pdf) == 2
    assert client.info(pdf) == 2
    assert len(fake.specs) == 1


@pytest.mark.parametrize("body", [{}, {"pages": "2"}, {"pages": 0}, {"pages": True}])
def test_info_without_page_count_is_protocol_error(share, fake, pdf, body):
    fake.get_script = [httpx.Response(200, json={"status": "done", **body})]
    with pytest.raises(CallasProtocolError):
        make_client(share, fake).info(pdf, job_id="info-bad")


def test_split_returns_verified_parts_in_requested_order(share, fake, pdf):
    def done(job_id):
        return httpx.Response(200, json={"status": "done", "files": [
            fake.out_file(job_id, "b.pdf", b"%PDF-b"),
            fake.out_file(job_id, "a.pdf", b"%PDF-a"),
        ]})

    fake.get_script = [done]
    client = make_client(share, fake)
    result = client.split(pdf, [("a.pdf", [1]), ("b.pdf", [2, 1])], expect_pages=2, job_id="split-1")
    assert list(result) == ["a.pdf", "b.pdf"]
    assert result["a.pdf"].read_bytes() == b"%PDF-a"
    spec = fake.specs["split-1"]
    assert spec["type"] == "split"
    assert spec["parts"] == [{"name": "a.pdf", "pages": [1]}, {"name": "b.pdf", "pages": [2, 1]}]
    assert spec["out_dir"] == f"{fake.server_share}/.callas/split-1/out"
    assert spec["expect_pages"] == 2


def test_split_rejects_unexpected_file_set(share, fake, pdf):
    fake.get_script = [lambda job_id: httpx.Response(200, json={"status": "done", "files": [
        fake.out_file(job_id, "other.pdf", b"%PDF-x")]})]
    with pytest.raises(CallasResultError):
        make_client(share, fake).split(pdf, [("a.pdf", [1])], job_id="split-2")


def test_split_rejects_sha_mismatch(share, fake, pdf):
    def done(job_id):
        entry = fake.out_file(job_id, "a.pdf", b"%PDF-a")
        entry["sha256"] = "0" * 64
        return httpx.Response(200, json={"status": "done", "files": [entry]})

    fake.get_script = [done]
    with pytest.raises(CallasResultError):
        make_client(share, fake).split(pdf, [("a.pdf", [1])], job_id="split-3")


@pytest.mark.parametrize(
    "parts, expect",
    [
        ([], None),
        ([("../a.pdf", [1])], None),
        ([("a.txt", [1])], None),
        ([("a.pdf", [])], None),
        ([("a.pdf", [0])], None),
        ([("a.pdf", [1]), ("a.pdf", [2])], None),
        ([("a.pdf", [3])], 2),
    ],
)
def test_split_validates_arguments_before_any_request(share, fake, pdf, parts, expect):
    with pytest.raises(CallasConfigError):
        make_client(share, fake).split(pdf, parts, expect_pages=expect, job_id="split-bad")
    assert fake.requests == []


def test_page_extractor_copies_part_and_cleans_job(share, fake, pdf, tmp_path):
    fake.get_script = [lambda job_id: httpx.Response(200, json={"status": "done", "files": [
        fake.out_file(job_id, "page.pdf", b"%PDF-page")]})]
    client = make_client(share, fake)
    output = tmp_path / "out" / "part.pdf"
    output.parent.mkdir()
    make_callas_page_extractor(client)(pdf, 2, output, "5")
    assert output.read_bytes() == b"%PDF-page"
    ((job_id, spec),) = fake.specs.items()
    assert spec["parts"] == [{"name": "page.pdf", "pages": [2]}]
    assert "DELETE" in fake.methods()
    assert not client.job_dir(job_id).exists()
    assert not list(output.parent.glob(".*"))


def test_page_extractor_failure_leaves_no_output(share, fake, pdf, tmp_path):
    fake.get_script = [httpx.Response(200, json={"status": "failed", "error": "boom"})]
    client = make_client(share, fake)
    output = tmp_path / "part.pdf"
    with pytest.raises(Exception):
        make_callas_page_extractor(client)(pdf, 1, output, "5")
    assert not output.exists()
