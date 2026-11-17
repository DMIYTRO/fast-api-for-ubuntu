"""Режим «Автоматически»: Callas сам дорисовывает поля вылета (``services/callas_auto_bleed.py``).

Без сети: фейковый callas size_correction (``BleedClient`` дорисовывает 2 мм полей или
масштабирует макет), настоящий BatchProcessor и CallasFixService.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

os.environ.setdefault(
    "IMAGE_MAGIC_PASSWORD_HASH", PasswordHasher().hash("test-only-password")
)

import control_panel
from processing.batch_processor import BatchProcessor
from processing.models import FileCheck
from processing.pdf_resize_policy import PdfPagePlan
from processing.resample_policy import ResampleDecision
from server.settings import Settings
from services.batch_adapter import BatchProcessorAdapter, ProcessingOptions
from services.callas_auto_bleed import CallasAutoBleed, candidate_reason
from services.callas_fix import CallasFixService, fix_dir_for, read_state_file
from services.callas_source_fix import fixable_source_path
from services.pitstop.baseline import FixBaselinePolicy
from tests.fake_callas import FakeCallasMerger
from tests.test_callas_bleed import BleedClient, art_pdf
from tests.test_callas_source_fix import page_sizes_mm
from tests.test_callas_source_fix_baseline import FakeSourcePitStop, issue
from tests.test_callas_source_fix_raster import make_tiff
from tests.test_pdf_resize import _fake_resizer
from tests.test_pitstop_fix_baseline_workflow import CLIPPING, LOWRES, TRANSPARENCY, report

FACE = "T_(97x97)_4-4_(5-1)_face.pdf"
BACK = "T_(97x97)_4-4_(5-1)_back.pdf"
RASTER = "T_(97x97)_4-0_(5-1)_face.tif"
SINGLE = "T_(97x97)_4-0_(5-1)_face.pdf"
ORDER = "5:1"
DONE = {"waiting_confirmation", "completed", "failed", "cancelled"}


class CountingClient(BleedClient):
    """``BleedClient`` с подсчётом одновременных заданий и необязательной ошибкой."""

    def __init__(self, root: Path, *, scale: float = 1.0, delay: float = 0.0, error: Exception | None = None):
        super().__init__(root, scale=scale)
        self.delay = delay
        self.error = error
        self.active = 0
        self.peak = 0
        self._lock = threading.Lock()

    def process(self, pdf, profile, mode, *, job_id=None, **kwargs):
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.error is not None:
                self.inputs.append(Path(pdf))
                raise self.error
            return super().process(pdf, profile, mode, job_id=job_id, **kwargs)
        finally:
            with self._lock:
                self.active -= 1


def _processor(root: Path, hook, *, policy: str = "auto", resizer_calls: list | None = None) -> BatchProcessor:
    return BatchProcessor(
        root, root / "PDF", correction_policy=policy, auto_bleed=hook,
        pdf_resizer=_fake_resizer(resizer_calls if resizer_calls is not None else []),
    )


@pytest.fixture()
def env(tmp_path):
    client = CountingClient(tmp_path.parent / f"{tmp_path.name}-jobs")
    service = CallasFixService(client)
    hook = CallasAutoBleed(service, semaphore=threading.BoundedSemaphore(2))
    yield SimpleNamespace(root=tmp_path, client=client, service=service, hook=hook)
    shutil.rmtree(client.root, ignore_errors=True)


def _only_file(processor: BatchProcessor) -> FileCheck:
    orders = processor.inspect_orders()
    assert len(orders) == 1 and len(orders[0].files) == 1
    return orders[0].files[0]


# ------------------------------------------------------------------ processor


def test_bleed_is_drawn_accepted_and_marked_without_waiting(env, caplog):
    source = art_pdf(env.root / SINGLE, 97, 97)
    original = source.read_bytes()
    resizes: list = []
    with caplog.at_level(logging.INFO, logger="image_magic.processing"):
        order = _processor(env.root, env.hook, resizer_calls=resizes).inspect_orders()[0]
    item = order.files[0]

    assert env.client.inputs == [source]
    assert item.path == source and item.parsed.side == "face"
    accepted = fix_dir_for(source) / "accepted.pdf"
    assert item.content_path == accepted and page_sizes_mm(accepted) == [(101.0, 101.0)]
    assert item.resample_decision == "accept" and not item.needs_resample
    assert item.errors == [] and order.passed
    assert round(item.actual_width_mm) == 101
    assert item.dpi == pytest.approx(300, abs=1)  # содержимое — по исходнику клиента
    fix = item.callas_source_fix
    assert fix["status"] == "accepted" and fix["plan"] == "size_correction" and fix["auto"] is True
    assert fix["bleed_only"] is True and fix["baseline_pdf"] == str(source)
    assert item.size_fix["method"] == "bleed" and item.size_fix["auto"] is True
    assert item.size_fix["from_mm"] == [97.0, 97.0] and item.size_fix["to_mm"] == [101.0, 101.0]
    assert "Callas дорисовал поля вылета" in item.size_fix["detail"]
    assert not any("Callas" in warning for warning in item.warnings)
    assert read_state_file(source)["status"] == "accepted"
    assert source.read_bytes() == original
    assert resizes == []
    assert "order.callas_auto_bleed file=%s" % SINGLE in caplog.text and "result=accepted" in caplog.text


def test_ask_policy_never_calls_callas(env):
    art_pdf(env.root / SINGLE, 97, 97)
    item = _only_file(_processor(env.root, env.hook, policy="ask"))
    assert env.client.inputs == []
    assert item.content_path is None and item.resample_decision == "ask_confirmation"


def test_fix_that_changed_the_artwork_is_not_used(env):
    env.client.scale = 0.96
    art_pdf(env.root / SINGLE, 97, 97)
    reference = _only_file(_processor(env.root, None))
    item = _only_file(_processor(env.root, env.hook))

    assert len(env.client.inputs) == 1
    assert item.content_path is None and item.callas_source_fix is None
    assert item.resample_decision == reference.resample_decision
    assert item.size_fix == reference.size_fix
    assert read_state_file(env.root / SINGLE)["status"] == "ready"  # оператор может решить сам


def test_callas_failure_keeps_the_original_check(env):
    env.client.error = RuntimeError("callas недоступен")
    art_pdf(env.root / SINGLE, 97, 97)
    item = _only_file(_processor(env.root, env.hook))
    assert item.content_path is None and item.callas_source_fix is None
    assert read_state_file(env.root / SINGLE)["status"] == "failed"


def test_hook_exception_never_breaks_inspection(env, monkeypatch):
    art_pdf(env.root / SINGLE, 97, 97)
    monkeypatch.setattr(env.service, "read_state", lambda *_a, **_k: 1 / 0)
    item = _only_file(_processor(env.root, env.hook))
    assert item.content_path is None and not any("не удалось" in error for error in item.errors)


def test_busy_fix_folder_is_skipped(env):
    source = art_pdf(env.root / SINGLE, 97, 97)
    env.service.start(source, "size_correction")  # оператор уже запустил «Размер»
    item = _only_file(_processor(env.root, env.hook))
    assert env.client.inputs == [] and item.content_path is None


def test_recheck_reuses_the_fix(env):
    source = art_pdf(env.root / SINGLE, 97, 97)
    first = _only_file(_processor(env.root, env.hook))
    second = _only_file(_processor(env.root, env.hook))
    assert len(env.client.inputs) == 1
    assert second.content_path == first.content_path and second.size_fix["method"] == "bleed"

    # Ready + bleed_only (принятие было снято) — тоже без нового запуска.
    state = read_state_file(source)
    env.service.restore_decision(source, {**state, "status": "ready", "decision": None})
    assert _only_file(_processor(env.root, env.hook)).callas_source_fix["auto"] is True
    assert len(env.client.inputs) == 1

    # Исходник изменился — Callas запускается снова.
    stat = source.stat()
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    _only_file(_processor(env.root, env.hook))
    assert len(env.client.inputs) == 2


def test_cached_not_bleed_only_is_skipped_and_failures_are_retried(env):
    env.client.scale = 0.96
    art_pdf(env.root / SINGLE, 97, 97)
    _only_file(_processor(env.root, env.hook))
    _only_file(_processor(env.root, env.hook))
    assert len(env.client.inputs) == 1

    other = env.root / "other"
    other.mkdir()
    art_pdf(other / SINGLE, 97, 97)
    env.client.error = RuntimeError("callas недоступен")
    _only_file(_processor(other, env.hook))
    _only_file(_processor(other, env.hook))
    # Сбой (например временная недоступность Callas) не запоминается.
    assert len(env.client.inputs) == 3


def test_rejected_by_operator_is_not_rerun(env):
    source = art_pdf(env.root / SINGLE, 97, 97)
    env.service.start(source, "size_correction")
    env.service.execute(source, "size_correction", format_mm=(97, 97), bleed_mm=2.0)
    env.service.decide(source, "reject")
    item = _only_file(_processor(env.root, env.hook))
    assert item.content_path is None and len(env.client.inputs) == 1


@pytest.mark.parametrize(
    ("name", "size", "called"),
    [
        ("T_(97x97)_4-0_(5-1)_face.pdf", (101, 101), False),  # уже формат + вылеты
        ("T_(97x97)_4-0_(5-1)_face.pdf", (110, 110), False),  # больше формата с вылетами
        ("T_(97x97)_4-0_(5-1)_face.pdf", (90, 90), False),  # меньше формата
        ("T_(50x90)_4-0_(5-1)_face.pdf", (90, 50), True),  # 50×90 == 90×50
        ("T_(97x97)_4-0_(5-1)_face.pdf", (98.5, 99), True),  # частичные вылеты
    ],
)
def test_candidates_by_size(env, name, size, called):
    art_pdf(env.root / name, *size, dpi=150)
    _only_file(_processor(env.root, env.hook))
    assert bool(env.client.inputs) is called


def test_reveal_and_rotated_pages_are_not_candidates():
    processor = SimpleNamespace(size_extra_mm=4.0, tolerance_mm=0.5)
    parsed = SimpleNamespace(width_mm=97.0, height_mm=97.0)
    page = SimpleNamespace(width_mm=97.0, height_mm=97.0, rotation=0)
    check = FileCheck(path=Path(SINGLE), parsed=parsed, actual_width_mm=97.0, actual_height_mm=97.0)
    check.pdf_pages = (page,)
    assert candidate_reason(check, processor) is None

    reveal = PdfPagePlan(
        ResampleDecision.AUTO_CORRECT, "reveal", (101.0, 101.0), (1.0, 1.0), (0.0, 0.0),
        (2.0, 2.0), (300.0, 300.0), "",
    )
    check.pdf_resize_plans = {1: reveal}
    assert candidate_reason(check, processor) == "reveal"
    check.pdf_resize_plans = {1: PdfPagePlan(**{**reveal.__dict__, "decision": ResampleDecision.ASK_CONFIRMATION})}
    assert candidate_reason(check, processor) is None

    check.pdf_resize_plans = {}
    check.pdf_pages = (SimpleNamespace(width_mm=97.0, height_mm=97.0, rotation=90),)
    assert candidate_reason(check, processor) == "rotated"
    check.pdf_pages = (page, SimpleNamespace(width_mm=99.0, height_mm=97.0, rotation=0))
    assert candidate_reason(check, processor) == "mixed_page_sizes"


def test_raster_source_gets_bleed_through_source_pdf(env):
    raster = make_tiff(env.root / RASTER, 97, 97)
    item = _only_file(_processor(env.root, env.hook))

    source_pdf = fix_dir_for(raster) / "source.pdf"
    assert env.client.inputs == [source_pdf]
    assert item.content_path == fix_dir_for(raster) / "accepted.pdf"
    assert item.resample_decision == "accept" and item.errors == []
    assert item.actual_format == "TIFF" and item.dpi_x == pytest.approx(300, abs=1)
    assert item.callas_source_fix["baseline_pdf"] == str(source_pdf)
    assert item.size_fix["method"] == "bleed" and item.size_fix["auto"] is True


def test_sides_of_one_order_run_in_parallel_within_the_limit(tmp_path):
    client = CountingClient(tmp_path.parent / f"{tmp_path.name}-jobs", delay=0.3)
    hook = CallasAutoBleed(CallasFixService(client), semaphore=threading.BoundedSemaphore(1))
    art_pdf(tmp_path / FACE, 97, 97)
    art_pdf(tmp_path / BACK, 97, 97)
    try:
        order = _processor(tmp_path, hook).inspect_orders()[0]
    finally:
        shutil.rmtree(client.root, ignore_errors=True)
    assert len(client.inputs) == 2 and client.peak == 1
    assert all(item.size_fix and item.size_fix["method"] == "bleed" for item in order.files)


def test_adapter_passes_the_hook_only_in_auto_mode(tmp_path):
    seen: list = []

    def factory(*args, **kwargs):
        seen.append(kwargs.get("auto_bleed"))
        return BatchProcessor(*args, **kwargs)

    hook = object()
    for policy in ("ask", "reject", "auto"):
        BatchProcessorAdapter(
            ProcessingOptions(input_path=str(tmp_path), direction="digital", correction_policy=policy),
            processor_factory=factory, auto_bleed=hook,
        )
    BatchProcessorAdapter(
        ProcessingOptions(input_path=str(tmp_path), direction="digital", approve_corrections=True),
        processor_factory=factory, auto_bleed=hook,
    )
    assert seen == [None, None, hook, hook]


def test_control_panel_wires_the_fix_service_into_the_adapter(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'x.db'}", password_hash="x", log_dir=tmp_path / "logs")
    service = CallasFixService(BleedClient(tmp_path / "jobs"))
    adapter = control_panel._default_adapter_factory(settings, service)(
        ProcessingOptions(input_path=str(tmp_path), direction="digital", correction_policy="auto")
    )
    assert isinstance(adapter.processor.auto_bleed, CallasAutoBleed)
    assert adapter.processor.auto_bleed.service is service

    manual = control_panel._default_adapter_factory(settings, service)(
        ProcessingOptions(input_path=str(tmp_path), direction="digital", correction_policy="ask")
    )
    assert manual.processor.auto_bleed is None
    disabled = Settings(database_url=f"sqlite:///{tmp_path / 'x.db'}", password_hash="x", log_dir=tmp_path / "logs", callas_auto_bleed_enabled=False)
    assert control_panel._callas_auto_bleed(disabled, service) is None
    assert control_panel._callas_auto_bleed(settings, None) is None


def test_settings_read_auto_bleed_environment(monkeypatch):
    monkeypatch.setenv("IMAGE_MAGIC_CALLAS_AUTO_BLEED_ENABLED", "false")
    monkeypatch.setenv("IMAGE_MAGIC_CALLAS_AUTO_BLEED_MAX_CONCURRENT", "3")
    monkeypatch.setenv("IMAGE_MAGIC_PASSWORD_HASH", "x")
    settings = Settings.from_env()
    assert settings.callas_auto_bleed_enabled is False
    assert settings.callas_auto_bleed_max_concurrent == 3
    monkeypatch.delenv("IMAGE_MAGIC_CALLAS_AUTO_BLEED_ENABLED")
    monkeypatch.delenv("IMAGE_MAGIC_CALLAS_AUTO_BLEED_MAX_CONCURRENT")
    defaults = Settings.from_env()
    assert defaults.callas_auto_bleed_enabled is True
    assert defaults.callas_auto_bleed_max_concurrent == 2


# --------------------------------------------------------------- through the app


class AutoBleedAppCase(unittest.TestCase):
    """Приложение в режиме «Автоматически» с фейковыми callas и (по желанию) PitStop."""

    def adapter_kwargs(self) -> dict:
        return {}

    def make_sources(self) -> None:
        self.face = art_pdf(self.root / FACE, 97, 97)
        self.back = art_pdf(self.root / BACK, 97, 97)

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(dir=control_panel.PROJECT_DIR)
        self.root = Path(self.temporary_directory.name)
        self.make_sources()
        self.fix_client = CountingClient(self.root.parent / f"{self.root.name}-jobs")
        self.merger = FakeCallasMerger()
        self.resize_calls: list = []
        settings = Settings(
            database_url=f"sqlite:///{self.root / 'state.sqlite3'}",
            password_hash=PasswordHasher().hash("correct horse"),
            login_failure_delay_seconds=0,
            log_dir=self.root / "logs",
            log_heartbeat_seconds=10,
        )
        service = CallasFixService(self.fix_client)
        hook = CallasAutoBleed(service, semaphore=threading.BoundedSemaphore(2))

        def processor_factory(*args, **kwargs):
            return BatchProcessor(
                *args, pdf_merger=self.merger, pdf_resizer=_fake_resizer(self.resize_calls), **kwargs
            )

        from unittest import mock

        patcher = mock.patch.object(control_panel, "_source_pdf_path", fixable_source_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        app = control_panel.create_app(
            settings=settings,
            allowed_roots=(self.root,),
            default_input_dir=self.root,
            adapter_factory=lambda options: BatchProcessorAdapter(
                options, processor_factory=processor_factory, auto_bleed=hook, **self.adapter_kwargs()
            ),
            callas_fix_service=service,
            initialize_schema=True,
        )
        self.context = TestClient(app)
        self.client = self.context.__enter__()
        self.client.post("/api/auth/login", json={"password": "correct horse"})

    def tearDown(self) -> None:
        self.context.__exit__(None, None, None)
        shutil.rmtree(self.fix_client.root, ignore_errors=True)
        self.temporary_directory.cleanup()

    def run_check(self, policy: str = "auto") -> dict:
        response = self.client.post("/api/checks", json={
            "input_path": str(self.root), "direction": "digital", "correction_policy": policy,
            "create_pdfs": True, "generate_previews": False, "copy_failures": False,
        })
        self.assertEqual(response.status_code, 202, response.text)
        run_id = response.json()["id"]
        run = self.client.app.state.coordinator.wait_for(run_id, DONE, timeout=30)
        items = self.client.get(f"/api/checks/{run_id}/orders").json()["items"]
        self.assertEqual(len(items), 1)
        return {"run": run, "order": items[0], "run_id": run_id}

    @staticmethod
    def entry(order: dict, name: str) -> dict:
        return next(item for item in order["files"] if item["name"] == name)


class AutoBleedAppTests(AutoBleedAppCase):
    def test_auto_run_completes_from_the_accepted_fixes(self):
        result = self.run_check()
        self.assertEqual(result["run"]["status"], "completed", result["run"])
        order = result["order"]
        self.assertIn(order["status"], {"passed", "warning"}, order)
        self.assertEqual(order["pending_confirmations"], 0)
        self.assertEqual(self.merger.calls[-1][0], ["accepted.pdf", "accepted.pdf"])
        self.assertEqual(page_sizes_mm(Path(order["pdf_path"])), [(101.0, 101.0), (101.0, 101.0)])
        self.assertEqual(self.resize_calls, [])
        for name in (FACE, BACK):
            entry = self.entry(order, name)
            self.assertEqual(entry["size_fix"]["method"], "bleed")
            self.assertIs(entry["size_fix"]["auto"], True)
            self.assertIs(entry["callas_source_fix"]["bleed_only"], True)
            self.assertTrue(entry["callas_source_fix"]["pdf_path"].endswith("accepted.pdf"))
            self.assertEqual(entry["errors"], [])

    def test_manual_mode_still_waits_for_the_operator(self):
        result = self.run_check("ask")
        self.assertEqual(result["run"]["status"], "waiting_confirmation")
        self.assertEqual(self.fix_client.inputs, [])
        self.assertEqual(result["order"]["pending_confirmations"], 2)


class AutoBleedRasterAppTests(AutoBleedAppCase):
    def make_sources(self) -> None:
        self.raster = make_tiff(self.root / RASTER, 97, 97)

    def test_raster_side_is_built_from_the_accepted_pdf(self):
        result = self.run_check()
        self.assertEqual(result["run"]["status"], "completed", result["run"])
        order = result["order"]
        self.assertEqual(self.merger.calls[-1][0], ["accepted.pdf"])
        entry = self.entry(order, RASTER)
        self.assertEqual(entry["callas_source_fix"]["baseline_pdf"], str(fix_dir_for(self.raster) / "source.pdf"))
        self.assertEqual(entry["size_fix"]["method"], "bleed")
        self.assertEqual(entry["errors"], [])


class AutoBleedPitStopCase(AutoBleedAppCase):
    """Всё, что принесли поля, скрыто PitStop-сравнением с исходниками клиента."""

    def adapter_kwargs(self) -> dict:
        return {"pitstop_service": self.pitstop, "pitstop_fix_baseline": FixBaselinePolicy()}

    def setUp(self) -> None:
        self.pitstop = FakeSourcePitStop()
        super().setUp()


class AutoBleedPitStopTests(AutoBleedPitStopCase):
    def test_production_pdf_hides_what_the_bleed_added(self):
        self.pitstop.by_path[self.face.resolve()] = report((TRANSPARENCY, 5))
        self.pitstop.by_path[self.back.resolve()] = report()
        self.pitstop.built = report((TRANSPARENCY, 5), (CLIPPING, 8), (LOWRES, 8))
        result = self.run_check()
        order = result["order"]
        self.assertEqual(order["pdf_kind"], "production")
        pitstop = order["pitstop"]
        self.assertEqual(pitstop["baseline_status"], "applied")
        self.assertTrue(issue(pitstop, 2079)["hidden_by_baseline"])
        self.assertTrue(issue(pitstop, 1150)["hidden_by_baseline"])
        self.assertNotIn("hidden_by_baseline", issue(pitstop, 3004))
        self.assertEqual(pitstop["verdict"], "error")  # прозрачность клиента осталась
        self.assertEqual(
            [Path(item["path"]) for item in order["pitstop_baseline"]["sources"]],
            [self.face.resolve(), self.back.resolve()],
        )


class AutoBleedDiagnosticPitStopTests(AutoBleedPitStopCase):
    """Оборот неисправим (слишком мал): заказ — ошибка, PDF диагностический."""

    def make_sources(self) -> None:
        self.face = art_pdf(self.root / FACE, 97, 97)
        self.back = art_pdf(self.root / BACK, 60, 60)

    def test_diagnostic_pdf_hides_what_the_bleed_added(self):
        self.pitstop.by_path[self.face.resolve()] = report()
        self.pitstop.by_path[self.back.resolve()] = report()
        self.pitstop.built = report((CLIPPING, 8))
        result = self.run_check()
        order = result["order"]
        self.assertEqual(order["pdf_kind"], "diagnostic", order)
        self.assertEqual(len(self.fix_client.inputs), 1)  # только лицо
        self.assertEqual(self.entry(order, FACE)["size_fix"]["method"], "bleed")
        pitstop = order["pitstop"]
        self.assertEqual(pitstop["baseline_status"], "applied", pitstop)
        self.assertTrue(issue(pitstop, 2079)["hidden_by_baseline"])


if __name__ == "__main__":
    unittest.main()


def test_orders_are_fixed_in_parallel_and_yielded_in_order(env):
    for number in range(1, 8):
        art_pdf(env.root / f"T_(97x97)_4-0_(5-{number})_face.pdf", 97, 97)
    orders = list(_processor(env.root, env.hook).iter_inspect_orders())
    ids = [order.order_id for order in orders]
    assert ids == sorted(ids) and len(ids) == 7
    assert all(order.files[0].size_fix and order.files[0].size_fix["method"] == "bleed" for order in orders)
    assert len(env.client.inputs) == 7
