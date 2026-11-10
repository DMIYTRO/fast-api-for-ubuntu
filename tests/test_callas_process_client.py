"""Тесты CallasClient.process на fake HTTP (httpx.MockTransport); живой callas не используется."""

from __future__ import annotations

import threading
from pathlib import Path

import httpx
import pytest

from server.settings import Settings
from services.callas import (
    CallasCancelledError,
    CallasConfigError,
    CallasJobFailedError,
    CallasProtocolError,
    CallasResultError,
    CallasValidationError,
    ProcessResult,
    parse_report_xml,
)
from tests.test_callas_client import FakeCallas, make_client, sha

PROFILE = "Image Magic/size_correction"
REPORT_XML = (
    b'<?xml version="1.0"?><report><profile_info>'
    b'<rule id="RUL1" display_name="Size check"/></profile_info>'
    b'<hits rule_id="RUL1" severity="warning"><hit page="1" llx="0" lly="0" urx="10" ury="20"/></hits></report>'
)
FIXED = b"%PDF-1.7 fixed"


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
    path = tmp_path / "заказ original.pdf"
    path.write_bytes(b"%PDF-1.4 original")
    return path


def done(fake: FakeCallas, job_id: str, *, files=None, exit_code=0, summary=None, **extra):
    """Ответ GET: файлы создаются на шаре при вызове."""
    if files is None:
        files = {"report.xml": REPORT_XML}
    entries = [fake.out_file(job_id, name, data) for name, data in files.items()]
    body = {
        "status": "done",
        "exit_code": exit_code,
        "result_status": "warnings",
        "summary": summary
        if summary is not None
        else {"errors": 0, "warnings": 1, "infos": 2, "fixups_applied": 0, "fixups_failed": 0},
        "files": entries,
        "bad_inputs": [],
    }
    body.update(extra)
    return httpx.Response(200, json=body)


# ----------------------------------------------------------------- успех


def test_analyze_success(share, fake, pdf):
    fake.get_script = [
        httpx.Response(200, json={"status": "queued"}),
        lambda job_id: done(fake, job_id, exit_code=3),
    ]
    client = make_client(share, fake)
    result = client.process(pdf, PROFILE, "analyze")

    assert isinstance(result, ProcessResult)
    assert result.mode == "analyze"
    assert result.exit_code == 3  # 1-8 — найденные проблемы, не ошибка
    assert result.status == "warnings"
    assert result.summary == {
        "errors": 0, "warnings": 1, "infos": 2, "fixups_applied": 0, "fixups_failed": 0,
    }
    assert result.output_pdf is None
    assert result.report_xml == share / ".callas" / result.job_id / "out" / "report.xml"
    assert result.report_xml.read_bytes() == REPORT_XML
    assert parse_report_xml(result.report_xml)[0].rule == "Size check"

    spec = fake.specs[result.job_id]
    assert spec["type"] == "process"
    assert spec["mode"] == "analyze"
    assert spec["profile"] == PROFILE
    assert spec["report"] == ["xml"]
    assert "output_name" not in spec and "variables" not in spec
    assert spec["out_dir"] == f"{share}/.callas/{result.job_id}/out"
    assert spec["input"] == {
        "path": f"{share}/.callas/{result.job_id}/in/0001.pdf",
        "sha256": sha(b"%PDF-1.4 original"),
    }
    assert pdf.read_bytes() == b"%PDF-1.4 original"


def test_fix_success_returns_new_file_and_keeps_source(share, fake, pdf):
    fake.get_script = [
        lambda job_id: done(
            fake,
            job_id,
            files={"fixed.pdf": FIXED, "fixed_report.xml": REPORT_XML},
            exit_code=1,
            summary={"errors": 0, "warnings": 0, "infos": 0, "fixups_applied": 4, "fixups_failed": 0},
            result_status="fixed",
        )
    ]
    client = make_client(share, fake)
    result = client.process(pdf, PROFILE, "fix", variables={"width": "103", "height": "214"})

    assert result.exit_code == 1  # исправления выполнены — не ошибка
    assert result.status == "fixed"
    assert result.summary["fixups_applied"] == 4
    assert result.output_pdf is not None
    assert result.output_pdf.read_bytes() == FIXED
    assert result.output_pdf.resolve() != pdf.resolve()
    assert result.output_pdf.parent == client.job_dir(result.job_id) / "out"
    assert result.report_xml is not None and result.report_xml.name == "fixed_report.xml"
    spec = fake.specs[result.job_id]
    assert spec["mode"] == "fix"
    assert spec["output_name"] == "fixed.pdf"
    assert spec["variables"] == {"width": "103", "height": "214"}
    assert pdf.read_bytes() == b"%PDF-1.4 original"


def test_exit_codes_1_to_8_are_findings_not_errors(share, fake, pdf):
    for code in range(1, 9):
        fake.get_script = [lambda job_id, code=code: done(fake, job_id, exit_code=code)]
        result = make_client(share, fake).process(
            pdf, PROFILE, "analyze", job_id=f"code-{code}"
        )
        assert result.exit_code == code


def test_status_is_derived_when_service_omits_it(share, fake, pdf):
    def reply(job_id):
        response = done(fake, job_id, summary={"errors": 2})
        body = response.json()
        body.pop("result_status")
        return httpx.Response(200, json=body)

    fake.get_script = [reply]
    result = make_client(share, fake).process(pdf, PROFILE, "analyze")
    assert result.status == "errors"
    assert result.summary["fixups_applied"] == 0


# ----------------------------------------------------------------- job_id


def test_job_id_is_deterministic_and_depends_on_inputs(share, fake, pdf, tmp_path):
    ids = []
    for kwargs in (
        {"mode": "analyze"},
        {"mode": "analyze"},
        {"mode": "analyze", "variables": {"a": "1"}},
        {"mode": "analyze", "profile": "Другой"},
    ):
        fake.get_script = [lambda job_id: done(fake, job_id)]
        fake.specs.clear()
        client = make_client(share, fake)
        params = {"profile": PROFILE, **kwargs}
        ids.append(client.process(pdf, params.pop("profile"), params.pop("mode"), **params).job_id)
    assert ids[0] == ids[1]
    assert len({ids[0], ids[2], ids[3]}) == 3

    other = tmp_path / "other.pdf"
    other.write_bytes(b"%PDF-1.4 changed")
    fake.get_script = [lambda job_id: done(fake, job_id)]
    assert make_client(share, fake).process(other, PROFILE, "analyze").job_id != ids[0]


# ----------------------------------------------------------------- ошибки


def test_unknown_profile_rejected_by_422(share, fake, pdf):
    fake.put_script = [
        httpx.Response(
            422, json={"detail": [{"loc": ["body", "profile"], "msg": "unknown profile 'X'"}]}
        )
    ]
    with pytest.raises(CallasValidationError) as caught:
        make_client(share, fake).process(pdf, "X", "analyze")
    assert "профил" in caught.value.message.lower()
    assert "X" in caught.value.message


def test_unknown_profile_reported_by_failed_job(share, fake, pdf):
    fake.get_script = [
        httpx.Response(
            200,
            json={
                "status": "failed",
                "error": "profile not found: Nope",
                "error_class": "profile_not_found",
            },
        )
    ]
    with pytest.raises(CallasJobFailedError) as caught:
        make_client(share, fake).process(pdf, "Nope", "analyze")
    assert "Nope" in caught.value.message
    assert "не найден" in caught.value.message
    assert caught.value.error_class == "profile_not_found"


@pytest.mark.parametrize("code", [100, 101, 255])
def test_exit_code_100_plus_is_failure(share, fake, pdf, code):
    fake.get_script = [
        lambda job_id: done(fake, job_id, exit_code=code, error="license expired")
    ]
    with pytest.raises(CallasJobFailedError) as caught:
        make_client(share, fake).process(pdf, PROFILE, "analyze")
    assert str(code) in caught.value.message
    assert "license expired" in caught.value.message


def test_missing_or_invalid_exit_code_is_protocol_error(share, fake, pdf):
    def reply(job_id):
        body = done(fake, job_id).json()
        body.pop("exit_code")
        return httpx.Response(200, json=body)

    fake.get_script = [reply]
    with pytest.raises(CallasProtocolError):
        make_client(share, fake).process(pdf, PROFILE, "analyze")


def test_sha256_mismatch_of_result_is_rejected(share, fake, pdf):
    def reply(job_id):
        response = done(fake, job_id, files={"fixed.pdf": FIXED, "r.xml": REPORT_XML})
        body = response.json()
        for entry in body["files"]:
            if entry["name"] == "fixed.pdf":
                entry["sha256"] = "0" * 64
        return httpx.Response(200, json=body)

    fake.get_script = [reply]
    with pytest.raises(CallasResultError, match="Контрольная сумма"):
        make_client(share, fake).process(pdf, PROFILE, "fix")


def test_fix_without_output_pdf_is_error(share, fake, pdf):
    fake.get_script = [lambda job_id: done(fake, job_id, files={"report.xml": REPORT_XML})]
    with pytest.raises(CallasResultError, match="исправленный PDF"):
        make_client(share, fake).process(pdf, PROFILE, "fix")
    assert pdf.read_bytes() == b"%PDF-1.4 original"


def test_fix_with_report_pdf_only_is_not_taken_as_result(share, fake, pdf):
    fake.get_script = [
        lambda job_id: done(
            fake, job_id, files={"x_report.pdf": b"%PDF-report", "report.xml": REPORT_XML}
        )
    ]
    with pytest.raises(CallasResultError, match="исправленный PDF"):
        make_client(share, fake).process(pdf, PROFILE, "fix", report=("xml", "pdf"))


def test_missing_xml_report_is_error_when_requested(share, fake, pdf):
    fake.get_script = [lambda job_id: done(fake, job_id, files={"fixed.pdf": FIXED})]
    with pytest.raises(CallasResultError, match="XML-отчёт"):
        make_client(share, fake).process(pdf, PROFILE, "analyze")


def test_cancel_before_start_sends_nothing(share, fake, pdf):
    event = threading.Event()
    event.set()
    with pytest.raises(CallasCancelledError):
        make_client(share, fake).process(pdf, PROFILE, "analyze", cancel=event)
    assert fake.requests == []


def test_cancel_during_poll_deletes_remote_job(share, fake, pdf):
    event = threading.Event()

    def running(job_id):
        event.set()
        return httpx.Response(200, json={"status": "running"})

    fake.get_script = [running]
    with pytest.raises(CallasCancelledError):
        make_client(share, fake).process(pdf, PROFILE, "fix", cancel=event)
    assert fake.methods()[-1] == "DELETE"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": "repair"},
        {"mode": "FIX"},
        {"profile": ""},
        {"profile": "../etc/passwd"},
        {"profile": "/abs/profile"},
        {"profile": "a\\b"},
        {"profile": "x" * 301},
        {"report": ()},
        {"report": ("html",)},
        {"variables": {"k": 1}},
        {"variables": {str(i): "v" for i in range(51)}},
        {"job_id": "../bad"},
    ],
)
def test_invalid_arguments(share, fake, pdf, kwargs):
    params = {"profile": PROFILE, "mode": "analyze", **kwargs}
    with pytest.raises(CallasConfigError):
        make_client(share, fake).process(pdf, params.pop("profile"), params.pop("mode"), **params)
    assert fake.requests == []


def test_missing_source_pdf(share, fake, tmp_path):
    with pytest.raises(CallasConfigError, match="не найден"):
        make_client(share, fake).process(tmp_path / "nope.pdf", PROFILE, "analyze")
    assert fake.requests == []


def test_source_changed_during_job_is_rejected(share, fake, pdf):
    def reply(job_id):
        pdf.write_bytes(b"%PDF-1.4 original but modified")
        return done(fake, job_id)

    fake.get_script = [reply]
    with pytest.raises(CallasResultError, match="изменился"):
        make_client(share, fake).process(pdf, PROFILE, "analyze")


def test_process_flag_defaults_off_and_reads_env(monkeypatch):
    monkeypatch.delenv("IMAGE_MAGIC_CALLAS_PROCESS_ENABLED", raising=False)
    assert Settings.from_env().callas_process_enabled is False
    monkeypatch.setenv("IMAGE_MAGIC_CALLAS_PROCESS_ENABLED", "true")
    assert Settings.from_env().callas_process_enabled is True


# ------------------------------------------------- имя входного файла (keep_name)


def test_keep_name_stages_original_name_so_plans_can_read_size(share, fake, tmp_path):
    named = tmp_path / "09_NP_Bezlam_(99x210)_4-4_T1000_(34250-26146648)_offset-face.pdf"
    named.write_bytes(b"%PDF-1.4 original")
    fake.get_script = [lambda job_id: done(fake, job_id)]
    client = make_client(share, fake)
    result = client.process(named, PROFILE, "analyze", keep_name=True)

    staged = fake.specs[result.job_id]["input"]["path"]
    assert staged == f"{share}/.callas/{result.job_id}/in/{named.name}"
    assert (client.job_dir(result.job_id) / "in" / named.name).read_bytes() == b"%PDF-1.4 original"


def test_keep_name_makes_name_safe_and_keeps_size_token(share, fake, tmp_path):
    odd = tmp_path / "заказ (99х210) №1.pdf"  # кириллические «з…», «х», «№»
    odd.write_bytes(b"%PDF-1.4 original")
    fake.get_script = [lambda job_id: done(fake, job_id)]
    client = make_client(share, fake)
    result = client.process(odd, PROFILE, "analyze", keep_name=True)

    name = Path(fake.specs[result.job_id]["input"]["path"]).name
    assert "(99x210)" in name  # кириллическая «х» приведена к латинской «x»
    assert name.endswith(".pdf") and name.isascii()


def test_default_staging_still_uses_index_name(share, fake, pdf):
    fake.get_script = [lambda job_id: done(fake, job_id)]
    result = make_client(share, fake).process(pdf, PROFILE, "analyze")
    assert fake.specs[result.job_id]["input"]["path"].endswith("/in/0001.pdf")
