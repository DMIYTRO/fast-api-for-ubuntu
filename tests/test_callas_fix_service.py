"""CallasFixService: исправление PDF планом Callas, состояние, решение оператора (без живого сервера)."""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest

from config.callas_plans import CALLAS_PLANS, CallasPlan
from services.callas.errors import CallasValidationError
from services.callas_fix import (
    CallasFixError,
    CallasFixService,
    comparison_payload,
    fix_dir_for,
    read_state_file,
)

MM = 72 / 25.4


def make_pdf(path: Path, width_mm: float, height_mm: float, *, scale_y: float = 1.0) -> Path:
    document = fitz.open()
    page = document.new_page(width=width_mm * MM, height=height_mm * MM)
    for index in range(6):
        top = (8 + index * 12) * MM * scale_y
        page.draw_rect(fitz.Rect(10 * MM, top, (width_mm - 10) * MM, top + 6 * MM * scale_y), color=(0, 0, 0), fill=(0.1, 0.4, 0.7))
    document.save(path)
    document.close()
    return path


class FakeClient:
    def __init__(self, root: Path, fixed_source: Path, error: Exception | None = None) -> None:
        self.root = root
        self.fixed_source = fixed_source
        self.error = error
        self.calls: list[tuple[str, str, str, str]] = []

    def process(self, pdf, profile, mode, *, job_id=None, report=("xml",), **_kwargs):
        self.calls.append((Path(pdf).name, profile, mode, job_id))
        if self.error is not None:
            raise self.error
        out = self.root / job_id / "out"
        out.mkdir(parents=True)
        shutil.copyfile(self.fixed_source, out / "fixed.pdf")
        return SimpleNamespace(
            job_id=job_id,
            mode=mode,
            status="warnings",
            exit_code=1,
            summary={"errors": 0, "warnings": 1, "infos": 0, "fixups_applied": 2, "fixups_failed": 0},
            output_pdf=out / "fixed.pdf",
            report_xml=None,
        )

    def job_dir(self, job_id: str) -> Path:
        return self.root / job_id


@pytest.fixture()
def env(tmp_path):
    pdf_dir = tmp_path / "PDF"
    pdf_dir.mkdir()
    source = make_pdf(pdf_dir / "order.pdf", 104, 204)
    fixed = make_pdf(tmp_path / "fixed_source.pdf", 103, 214, scale_y=1.05)
    client = FakeClient(tmp_path / "jobs", fixed)
    return SimpleNamespace(tmp=tmp_path, source=source, fixed=fixed, client=client, service=CallasFixService(client))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(service, pdf, key="size_correction"):
    service.start(pdf, key)
    return service.execute(pdf, key)


def test_fix_produces_result_and_three_images_and_keeps_source(env):
    before = sha(env.source)
    state = run(env.service, env.source)

    assert state["status"] == "ready"
    assert state["error"] is None
    assert env.client.calls[0][1:3] == (CALLAS_PLANS["size_correction"].profile, "fix")
    directory = fix_dir_for(env.source)
    assert (directory / "fixed.pdf").is_file()
    for kind in ("original", "result", "overlay"):
        assert env.service.file_path(env.source, kind).is_file()
    assert env.service.file_path(env.source, "pdf") == directory / "fixed.pdf"
    assert state["summary"]["original_size_mm"] == pytest.approx([104, 204], abs=0.1)
    assert state["summary"]["result_size_mm"] == pytest.approx([103, 214], abs=0.1)
    assert state["summary"]["callas"]["fixups_applied"] == 2
    assert sha(env.source) == before


def test_job_folder_is_cleaned_and_state_survives_reread(env):
    run(env.service, env.source)
    assert not list((env.tmp / "jobs").glob("*"))
    persisted = read_state_file(env.source)
    assert persisted["status"] == "ready"
    payload = comparison_payload(persisted, "/base")
    assert payload["original_url"] == "/base/files/original"
    assert payload["pdf_url"] == "/base/files/pdf"
    assert payload["plan"]["key"] == "size_correction"


def test_callas_error_is_stored_as_failed_with_operator_text(env):
    env.client.error = CallasValidationError("Профиль «Image Magic/size_correction» не найден на сервере callas.")
    state = run(env.service, env.source)
    assert state["status"] == "failed"
    assert "не найден на сервере" in state["error"]
    assert env.service.file_path(env.source, "pdf") is None
    # Повтор после сбоя разрешён.
    env.client.error = None
    assert run(env.service, env.source)["status"] == "ready"


def test_unexpected_error_gets_russian_prefix(env):
    env.client.error = RuntimeError("boom")
    state = run(env.service, env.source)
    assert state["status"] == "failed"
    assert state["error"].startswith("Исправление не выполнено:")


def test_start_validations(env):
    with pytest.raises(CallasFixError) as unknown:
        env.service.start(env.source, "nope")
    assert unknown.value.code == "unknown_callas_plan"
    with pytest.raises(CallasFixError) as missing:
        env.service.start(env.tmp / "absent.pdf", "size_correction")
    assert missing.value.status_code == 404
    plans = {"x": CallasPlan(key="x", title="X", profile="P/X", directions=("offset",))}
    restricted = CallasFixService(env.client, plans)
    with pytest.raises(CallasFixError) as direction:
        restricted.start(env.source, "x", direction="digital")
    assert direction.value.code == "callas_plan_direction"
    analyze = CallasFixService(env.client, {"a": CallasPlan(key="a", title="A", profile="P/A", mode="analyze")})
    with pytest.raises(CallasFixError):
        analyze.start(env.source, "a")


def test_second_start_while_running_is_busy(env):
    env.service.start(env.source, "size_correction")
    with pytest.raises(CallasFixError) as busy:
        env.service.start(env.source, "size_correction")
    assert busy.value.status_code == 409
    env.service.execute(env.source, "size_correction")
    env.service.start(env.source, "size_correction")  # после завершения снова можно


def test_processing_without_live_job_becomes_failed_after_restart(env):
    env.service.start(env.source, "size_correction")
    fresh = CallasFixService(env.client)  # «перезапуск»: активных задач нет
    state = fresh.read_state(env.source)
    assert state["status"] == "failed"
    assert "перезапуском" in state["error"]
    assert read_state_file(env.source)["status"] == "failed"


def test_accept_and_reject(env):
    run(env.service, env.source)
    accepted = env.service.decide(env.source, "accept")
    assert accepted["status"] == "accepted" and accepted["decision"] == "accepted"
    assert env.service.accepted_pdf(env.source) == fix_dir_for(env.source) / "fixed.pdf"
    with pytest.raises(CallasFixError) as again:
        env.service.decide(env.source, "reject")
    assert again.value.status_code == 409

    run(env.service, env.source)
    rejected = env.service.decide(env.source, "reject")
    assert rejected["status"] == "rejected"
    assert env.service.accepted_pdf(env.source) is None


def test_decide_requires_ready_result_and_valid_value(env):
    with pytest.raises(CallasFixError) as none:
        env.service.decide(env.source, "accept")
    assert none.value.status_code == 409
    run(env.service, env.source)
    with pytest.raises(CallasFixError) as invalid:
        env.service.decide(env.source, "maybe")
    assert invalid.value.code == "invalid_decision"


def test_decision_refused_when_source_changed_after_fix(env):
    run(env.service, env.source)
    make_pdf(env.source, 99, 210)  # исходный PDF пересобран
    with pytest.raises(CallasFixError) as stale:
        env.service.decide(env.source, "accept")
    assert stale.value.code == "callas_fix_stale"
    assert env.service.accepted_pdf(env.source) is None


def test_file_path_rejects_unknown_kind(env):
    run(env.service, env.source)
    assert env.service.file_path(env.source, "../state.json") is None
    assert env.service.file_path(env.tmp / "PDF" / "other.pdf", "overlay") is None
