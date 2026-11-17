"""Принятие исправления Callas исходного PDF заказа, который ждёт решения по коррекции.

Сценарий из жизни: лицо и оборот 97×97 мм, нужно 101×101 (97 + 2×2 мм вылеты). Заказ
останавливается до сборки PDF; план Callas «Коррекция размера» дорисовывает поля на
копии исходника, оператор принимает результат по каждой стороне. Без сети: фейковые
callas (исправление, склейка, рендер, QuickFix), настоящий BatchProcessor.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import pymupdf
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import select

os.environ.setdefault(
    "IMAGE_MAGIC_PASSWORD_HASH", PasswordHasher().hash("test-only-password")
)

import control_panel
from processing.batch_processor import BatchProcessor
from server.models import CorrectionDecision
from server.settings import Settings
from services.batch_adapter import BatchProcessorAdapter
from services.callas_fix import CallasFixService
from services.callas_source_fix import STALE_SOURCE_WARNING
from tests.fake_callas import FakeCallasMerger
from tests.test_callas_fix_service import FakeClient, make_pdf
from tests.test_pdf_resize import _fake_resizer

# Принятое исправление — значок в интерфейсе, а не строка в warnings.
NOTE = "Размер исправлен Callas (поля дорисованы), использован исправленный PDF"
FACE = "T_(97x97)_4-4_(5-1)_face.pdf"
BACK = "T_(97x97)_4-4_(5-1)_back.pdf"
ORDER = "5:1"
WAITING = {"waiting_confirmation", "completed", "failed", "cancelled"}


def page_sizes_mm(path: Path) -> list[tuple[float, float]]:
    with pymupdf.open(path) as document:
        return [
            (round(page.rect.width * 25.4 / 72, 1), round(page.rect.height * 25.4 / 72, 1))
            for page in document
        ]


class SourceFixCase(unittest.TestCase):
    """Заказ 97×97 мм, ждущий решения, и приложение с фейковыми callas (без тестов)."""

    def adapter_kwargs(self) -> dict:
        """Дополнительные аргументы BatchProcessorAdapter (PitStop и т. п.)."""
        return {}

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(dir=control_panel.PROJECT_DIR)
        self.root = Path(self.temporary_directory.name)
        self.face = make_pdf(self.root / FACE, 97, 97)
        self.back = make_pdf(self.root / BACK, 97, 97, scale_y=0.9)
        self.originals = {path: path.read_bytes() for path in (self.face, self.back)}
        # Результат callas: страница 101×101 (поля дорисованы), как в реальном случае.
        fixed = make_pdf(self.root.parent / f"{self.root.name}-fixed.pdf", 101, 101)
        self.addCleanup(fixed.unlink, missing_ok=True)
        self.fix_client = FakeClient(self.root.parent / f"{self.root.name}-jobs", fixed)
        self.merger = FakeCallasMerger()
        self.resize_calls: list = []
        self.settings = Settings(
            database_url=f"sqlite:///{self.root / 'state.sqlite3'}",
            password_hash=PasswordHasher().hash("correct horse"),
            login_failure_delay_seconds=0,
            log_dir=self.root / "logs",
            log_heartbeat_seconds=10,
        )
        self.context = None
        self.file_ids: dict[tuple[str, str], str] = {}
        self.start_app()

    def start_app(self) -> None:
        def processor_factory(*args, **kwargs):
            return BatchProcessor(
                *args, pdf_merger=self.merger, pdf_resizer=_fake_resizer(self.resize_calls), **kwargs
            )

        app = control_panel.create_app(
            settings=self.settings,
            allowed_roots=(self.root,),
            default_input_dir=self.root,
            adapter_factory=lambda options: BatchProcessorAdapter(
                options, processor_factory=processor_factory, **self.adapter_kwargs()
            ),
            callas_fix_service=CallasFixService(self.fix_client),
            initialize_schema=True,
        )
        self.context = TestClient(app)
        self.client = self.context.__enter__()
        self.client.post("/api/auth/login", json={"password": "correct horse"})

    def stop_app(self) -> None:
        if self.context is not None:
            self.context.__exit__(None, None, None)
            self.context = None

    def tearDown(self) -> None:
        self.stop_app()
        import shutil

        shutil.rmtree(self.fix_client.root, ignore_errors=True)
        self.temporary_directory.cleanup()

    # ----------------------------------------------------------------- helpers
    @property
    def coordinator(self):
        return self.client.app.state.coordinator

    def make_run(self) -> str:
        response = self.client.post("/api/checks", json={
            "input_path": str(self.root), "direction": "digital",
            "create_pdfs": True, "generate_previews": False, "copy_failures": False,
        })
        self.assertEqual(response.status_code, 202, response.text)
        run_id = response.json()["id"]
        run = self.coordinator.wait_for(run_id, WAITING, timeout=10)
        self.assertEqual(run["status"], "waiting_confirmation")
        return run_id

    def wait_done(self, run_id: str) -> dict:
        run = self.coordinator.wait_for(run_id, timeout=10)
        self.assertEqual(run["status"], "completed", run)
        return run

    def order(self, run_id: str) -> dict:
        items = self.client.get(f"/api/checks/{run_id}/orders").json()["items"]
        self.assertEqual(len(items), 1)
        return items[0]

    def entry(self, order: dict, name: str) -> dict:
        return next(item for item in order["files"] if item["name"] == name)

    def fix_url(self, run_id: str, name: str, suffix: str = "") -> str:
        # Идентификатор запоминается: у собранного заказа блоков исходников уже нет.
        key = (run_id, name)
        if key not in self.file_ids:
            self.file_ids[key] = self.entry(self.order(run_id), name)["callas_fix_file_id"]
        file_id = self.file_ids[key]
        return f"/api/checks/{run_id}/orders/{ORDER}/callas/fix/source/{file_id}{suffix}"

    def run_fix(self, run_id: str, name: str) -> dict:
        url = self.fix_url(run_id, name)
        started = self.client.post(url, json={"plan": "size_correction"})
        self.assertEqual(started.status_code, 202, started.text)
        # Фоновая задача TestClient завершается до возврата управления.
        comparison = self.client.get(url).json()["comparison"]
        self.assertEqual(comparison["status"], "ready", comparison)
        return comparison

    def decide(self, run_id: str, name: str, decision: str = "accept"):
        return self.client.post(self.fix_url(run_id, name, "/decision"), json={"decision": decision})

    def accept(self, run_id: str, name: str) -> dict:
        self.run_fix(run_id, name)
        response = self.decide(run_id, name)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["comparison"]

    def merged_parts(self) -> list[str]:
        self.assertTrue(self.merger.calls)
        return self.merger.calls[-1][0]

    def assert_sources_untouched(self) -> None:
        for path, content in self.originals.items():
            self.assertEqual(path.read_bytes(), content, path.name)



class SourceFixTests(SourceFixCase):
    def test_ready_source_fix_of_waiting_order_can_be_accepted(self):
        run_id = self.make_run()
        order = self.order(run_id)
        self.assertEqual(order["pending_confirmations"], 2)
        self.assertIsNone(self.entry(order, FACE)["callas_fix"]["comparison"])

        comparison = self.run_fix(run_id, FACE)
        self.assertTrue(comparison["source_only"])
        self.assertTrue(comparison["can_accept"])
        self.assertFalse(comparison["applied"])
        listed = self.entry(self.order(run_id), FACE)["callas_fix"]["comparison"]
        self.assertTrue(listed["can_accept"])

    def test_accepting_one_side_keeps_the_order_waiting(self):
        run_id = self.make_run()
        comparison = self.accept(run_id, FACE)
        self.assertEqual(comparison["status"], "accepted")
        self.assertTrue(comparison["applied"])
        self.assertFalse(comparison["can_accept"])

        order = self.order(run_id)
        self.assertEqual(order["status"], "waiting_confirmation")
        self.assertEqual(order["pending_confirmations"], 1)
        face, back = self.entry(order, FACE), self.entry(order, BACK)
        fix = face["callas_source_fix"]
        self.assertEqual(fix["status"], "accepted")
        self.assertEqual(fix["plan"], "size_correction")
        self.assertEqual(Path(fix["pdf_path"]), self.root / "PDF" / "CallasFix" / f"{self.face.stem}.source" / "accepted.pdf")
        # Файл проверен заново по исправленному PDF: формат совпал, решения не нужно.
        self.assertEqual(face["resample_decision"], "accept")
        self.assertAlmostEqual(face["actual_width_mm"], 101, places=1)
        self.assertNotIn(NOTE, face["warnings"])
        self.assertEqual(face["path"], str(self.face))
        self.assertEqual(back["resample_decision"], "ask_confirmation")
        self.assertNotIn("callas_source_fix", back)
        self.assert_sources_untouched()

        actions = self.client.get(f"/api/checks/{run_id}/orders/{ORDER}/actions").json()["items"]
        accepted = [item for item in actions if item["kind"] == "order.callas_fix.accepted"]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["details"]["file"], FACE)

    def test_accepting_both_sides_resumes_and_builds_from_fixed_pdfs(self):
        run_id = self.make_run()
        self.accept(run_id, FACE)
        last = self.accept(run_id, BACK)
        self.assertTrue(last["applied"])
        self.wait_done(run_id)

        order = self.order(run_id)
        self.assertIn(order["status"], {"passed", "warning"}, order)
        self.assertTrue(order["pdf_path"])
        self.assertEqual(page_sizes_mm(Path(order["pdf_path"])), [(101.0, 101.0), (101.0, 101.0)])
        self.assertEqual(self.merged_parts(), ["accepted.pdf", "accepted.pdf"])
        self.assertEqual(self.resize_calls, [], "наш QuickFix не применялся")
        self.assertTrue(Path(order["pdf_path"]).name.startswith(self.face.stem))
        self.assert_sources_untouched()

        # Заказ продолжился сам, решение записано в журнал коррекций по обоим файлам.
        actions = self.client.get(f"/api/checks/{run_id}/orders/{ORDER}/actions").json()["items"]
        resumed = [item for item in actions if item["kind"] == "order.correction_confirmed"]
        self.assertEqual([item["label"] for item in resumed], ["Заказ продолжен: размер исправлен Callas"])
        with self.client.app.state.database.session_factory() as session:
            decisions = session.scalars(select(CorrectionDecision.decision)).all()
        self.assertEqual(sorted(decisions), ["callas_source_fix", "callas_source_fix"])

        # PDF собран: принять исправление исходника больше нельзя.
        refused = self.decide(run_id, FACE)
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.json()["error"]["code"], "callas_fix_source_only")

    def test_one_accepted_side_and_apply_resizes_only_the_other_side(self):
        run_id = self.make_run()
        self.accept(run_id, FACE)
        confirmed = self.client.post(f"/api/checks/{run_id}/orders/{ORDER}/correction", json={"decision": "confirm"})
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.wait_done(run_id)

        order = self.order(run_id)
        self.assertTrue(order["pdf_path"], order)
        self.assertEqual([call[0] for call in self.resize_calls], [BACK])
        parts = self.merged_parts()
        self.assertEqual(parts[0], "accepted.pdf")
        self.assertTrue(parts[1].startswith("resized_2_"), parts)
        face, back = self.entry(order, FACE), self.entry(order, BACK)
        self.assertNotIn(NOTE, face["warnings"])
        self.assertNotIn("коррекция размера подтверждена пользователем", face["warnings"])
        self.assertIn("коррекция размера подтверждена пользователем", back["warnings"])
        with self.client.app.state.database.session_factory() as session:
            decisions = session.scalars(select(CorrectionDecision.decision)).all()
        self.assertEqual(sorted(decisions), ["callas_source_fix", "confirmed"])

    def test_one_accepted_side_and_keep_as_is_rejects_only_the_other_side(self):
        run_id = self.make_run()
        self.accept(run_id, FACE)
        rejected = self.client.post(f"/api/checks/{run_id}/orders/{ORDER}/correction", json={"decision": "reject"})
        self.assertEqual(rejected.status_code, 200, rejected.text)
        self.wait_done(run_id)

        order = self.order(run_id)
        # Как при обычном отказе: заказ не печатается, PDF только диагностический.
        self.assertEqual(order["pdf_kind"], "diagnostic")
        self.assertNotEqual(order["status"], "passed")
        face, back = self.entry(order, FACE), self.entry(order, BACK)
        refusal = "пользователь отказался от предложенной коррекции размера"
        self.assertNotIn(refusal, face["errors"])
        self.assertEqual(face["resample_decision"], "accept")
        self.assertIn(refusal, back["errors"])
        self.assertEqual(self.resize_calls, [])
        # Диагностический PDF тоже собран из исправленного лица.
        self.assertIn("accepted.pdf", self.merged_parts())

    def test_source_changed_before_accept_is_refused(self):
        run_id = self.make_run()
        self.run_fix(run_id, FACE)
        make_pdf(self.face, 97, 97, scale_y=0.8)
        refused = self.decide(run_id, FACE)
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.json()["error"]["code"], "callas_fix_stale")
        self.assertEqual(self.order(run_id)["pending_confirmations"], 2)

    def test_source_changed_after_accept_ignores_the_fix_with_warning(self):
        run_id = self.make_run()
        self.accept(run_id, FACE)
        stat = self.face.stat()
        os.utime(self.face, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
        self.client.post(f"/api/checks/{run_id}/orders/{ORDER}/correction", json={"decision": "confirm"})
        self.wait_done(run_id)

        order = self.order(run_id)
        face = self.entry(order, FACE)
        self.assertIn(STALE_SOURCE_WARNING, face["warnings"])
        self.assertNotIn("callas_source_fix", face)
        self.assertNotIn(NOTE, face["warnings"])
        # Исправление не использовано; исходник снова требует решения, PDF не собран в печать.
        self.assertEqual(face["resample_decision"], "ask_confirmation")
        self.assertEqual(order["pdf_kind"], "diagnostic")
        self.assertNotEqual(order["status"], "passed")
        self.assertNotIn("accepted.pdf", self.merged_parts())

    def test_rejecting_source_fix_only_marks_it_rejected(self):
        run_id = self.make_run()
        self.run_fix(run_id, FACE)
        response = self.decide(run_id, FACE, "reject")
        self.assertEqual(response.status_code, 200, response.text)
        comparison = response.json()["comparison"]
        self.assertEqual(comparison["status"], "rejected")
        self.assertFalse(comparison["can_accept"])
        order = self.order(run_id)
        self.assertEqual(order["status"], "waiting_confirmation")
        self.assertEqual(order["pending_confirmations"], 2)
        self.assertNotIn("callas_source_fix", self.entry(order, FACE))

    def test_rerunning_size_fix_resets_the_accepted_decision(self):
        run_id = self.make_run()
        self.accept(run_id, FACE)
        comparison = self.run_fix(run_id, FACE)
        self.assertTrue(comparison["can_accept"])
        self.assertFalse(comparison["applied"])
        order = self.order(run_id)
        face = self.entry(order, FACE)
        self.assertNotIn("callas_source_fix", face)
        self.assertEqual(face["resample_decision"], "ask_confirmation")
        self.assertEqual(order["pending_confirmations"], 2)
        # Новый результат снова можно принять.
        self.assertEqual(self.decide(run_id, FACE).status_code, 200)
        self.assertEqual(self.order(run_id)["pending_confirmations"], 1)

    def test_restart_while_waiting_keeps_the_accepted_decision(self):
        run_id = self.make_run()
        self.accept(run_id, FACE)
        self.stop_app()
        self.start_app()

        run = self.client.get(f"/api/checks/{run_id}").json()
        self.assertEqual(run["status"], "failed")  # прерванная перезапуском проверка
        face = self.entry(self.order(run_id), FACE)
        self.assertEqual(face["callas_source_fix"]["status"], "accepted")
        self.assertTrue(face["callas_source_fix"]["pdf_path"].endswith("accepted.pdf"))
        self.assertNotIn(NOTE, face["warnings"])
        comparison = face["callas_fix"]["comparison"]
        self.assertEqual(comparison["status"], "accepted")
        self.assertTrue(comparison["applied"])
        self.assertFalse(comparison["can_accept"])
        refused = self.decide(run_id, BACK)
        self.assertEqual(refused.status_code, 409)

    def test_accepted_fix_of_another_check_must_be_accepted_again(self):
        first = self.make_run()
        self.accept(first, FACE)
        self.client.post(f"/api/checks/{first}/cancel")
        self.coordinator.wait_for(first, timeout=10)

        second = self.make_run()
        face = self.entry(self.order(second), FACE)
        self.assertNotIn("callas_source_fix", face)
        comparison = face["callas_fix"]["comparison"]
        # Принятое в прошлой проверке исправление этим заказом не используется.
        self.assertEqual(comparison["status"], "ready")
        self.assertIsNone(comparison["decision"])
        self.assertTrue(comparison["can_accept"])
        self.assertEqual(self.decide(second, FACE).status_code, 200)
        self.assertEqual(self.order(second)["pending_confirmations"], 1)


def test_callas_fix_folder_is_never_discovered_as_order_input(tmp_path):
    make_pdf(tmp_path / FACE, 97, 97)
    fix_dir = tmp_path / "PDF" / "CallasFix" / f"{Path(FACE).stem}.source"
    fix_dir.mkdir(parents=True)
    for name in ("fixed.pdf", "accepted.pdf", BACK):
        make_pdf(fix_dir / name, 101, 101)
    make_pdf(tmp_path / "PDF" / "CallasFix" / BACK, 101, 101)

    processor = BatchProcessor(tmp_path, tmp_path / "PDF")
    assert processor.scan() == [tmp_path / FACE]
    orders = processor.inspect_orders()
    assert [[item.path for item in order.files] for order in orders] == [[tmp_path / FACE]]


def test_reinspection_reads_content_but_keeps_the_source_identity(tmp_path):
    source = make_pdf(tmp_path / FACE, 97, 97)
    fixed = make_pdf(tmp_path / "fixed.pdf", 101, 101)
    processor = BatchProcessor(
        tmp_path, tmp_path / "PDF", correction_policy="ask", pdf_resizer=_fake_resizer([])
    )
    item = processor.inspect_orders()[0].files[0]
    assert item.resample_decision == "ask_confirmation"

    replacement = processor.reinspect_pdf_file(item, fixed)
    assert replacement is not item and replacement.path == source
    assert replacement.parsed == item.parsed and replacement.input_path == fixed
    assert replacement.resample_decision == "accept" and not replacement.errors
    assert round(replacement.actual_width_mm) == 101
    assert item.resample_decision == "ask_confirmation"  # исходная проверка не тронута

    again = processor.reinspect_pdf_file(replacement, None)
    assert again.input_path == source and again.resample_decision == "ask_confirmation"


if __name__ == "__main__":
    unittest.main()
