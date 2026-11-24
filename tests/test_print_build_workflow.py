"""«Провести в печать» жёлтого заказа с диагностическим PDF: сборка итогового PDF при печати.

Заказ с одним лишь низким разрешением, у которого PitStop диагностического PDF не нашёл
ошибок, получает статус «предупреждение», но его PDF — диагностический «как есть» (без
RGB → CMYK, снятия ICC и полных допечатных проверок). Такой файл в печать не идёт: при печати
собирается итоговый PDF производственным путём (``print_build``), без красного подтверждения.
Сквозные сценарии через create_app с настоящим BatchProcessorAdapter; callas, PitStop и
Sborka — фейки (см. ``tests/test_forced_print_workflow.py``).
"""
from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

import pymupdf
from sqlalchemy import select

from server.models import OrderAction, PdfRevision
from services.pitstop import PitStopSeverity
import tests.test_forced_print_workflow as forced
from tests.test_forced_print_workflow import GOOD_A, LOW, MM, FakePitStop, make_pdf
from tests.test_pitstop_fix_baseline_workflow import check_result, report

SIZE = "22_NP(P)_Bezlam_150_mel_(160x160)_4-0_L25_T50_(36201-26252243)_falc1-face.pdf"


def make_rgb_pdf(path: Path, *, dpi: float, size_mm: float = 164.0) -> Path:
    """Страница 164×164 мм с RGB-растром на всю страницу (диагностический PDF его сохранит)."""
    document = pymupdf.open()
    page = document.new_page(width=size_mm * MM, height=size_mm * MM)
    side = int(size_mm / 25.4 * dpi)
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, side, side), False)
    pixmap.set_rect(pixmap.irect, (200, 30, 30))
    page.insert_image(page.rect, pixmap=pixmap)
    document.save(path)
    document.close()
    return path


class FakeCmykConverter:
    """callas RGB → CMYK: пересобирает страницы с CMYK-растром того же размера."""

    profile = "Image Magic/NEW_Convert color to 4c for digital printing"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, source, target, label):
        self.calls.append(str(label))
        with pymupdf.open(source) as original:
            sizes = [(page.rect.width, page.rect.height) for page in original]
        document = pymupdf.open()
        for width, height in sizes:
            page = document.new_page(width=width, height=height)
            side = int(width / 72 * 200)
            pixmap = pymupdf.Pixmap(pymupdf.csCMYK, pymupdf.IRect(0, 0, side, side), False)
            pixmap.set_rect(pixmap.irect, (0, 80, 80, 0))
            page.insert_image(page.rect, pixmap=pixmap)
        document.save(target)
        document.close()
        return {"fixups_applied": 1}


class WarningPitStop(FakePitStop):
    """PitStop: предупреждение для заказов из ``warn`` (жёлтый производственный заказ)."""

    def __init__(self) -> None:
        super().__init__()
        self.warn: set[str] = set()

    def check_pdf(self, pdf, *, profile_id):
        if any(order_id in Path(pdf).name for order_id in self.warn):
            self.checked.append(str(pdf))
            issue = ((1150, PitStopSeverity.WARNING, "Image resolution below 275 ppi"), 1)
            return check_result(Path(pdf), report(issue), f"job-{len(self.checked)}", profile_id)
        return super().check_pdf(pdf, profile_id=profile_id)


class PrintBuildWorkflowTests(forced.ForcedPrintWorkflowTests):
    def setUp(self) -> None:
        self.converter = FakeCmykConverter()
        super().setUp()
        # Overprint нет: заказ с низким разрешением жёлтый, а не красный.
        self.prepress.blocking = 0

    def make_pitstop(self) -> "WarningPitStop":
        return WarningPitStop()

    def processor_kwargs(self) -> dict:
        # Тот же стенд, что и у сборки «под ответственность», плюс перевод RGB → CMYK.
        return {"pdf_color_converter": self.converter}

    def yellow_run(self) -> tuple[str, dict]:
        make_rgb_pdf(self.root / LOW, dpi=200)
        run_id = self.make_run()
        order = self.orders(run_id)["26252239"]
        self.assertEqual((order["status"], order["pdf_kind"]), ("warning", "diagnostic"), order)
        # Жёлтый диагностический PDF лежит в PDF/Rework, но в печать не годится.
        self.assertEqual(Path(order["pdf_path"]).parent.name, "Rework")
        return run_id, order

    def test_yellow_diagnostic_order_is_built_converted_checked_and_printed(self):
        run_id, order = self.yellow_run()
        diagnostic = Path(order["pdf_path"])
        diagnostic_sha = hashlib.sha256(diagnostic.read_bytes()).hexdigest()
        self.assertEqual(order["print_blockers"], [])
        self.assertEqual(order["print_mode"], "normal")
        self.assertTrue(order["print_build"])
        self.assertEqual(self.converter.calls, [])  # диагностический PDF «как есть»

        # Без подтверждения «под ответственность»: заказ жёлтый.
        item = self.print_orders(run_id, "26252239", confirm=False)["26252239"]

        self.assertEqual(item["status"], "prepared", item)
        build = item["print_build"]
        self.assertEqual((build["pdf_revision"], build["created_by_action"]), (2, "print_build"))
        self.assertIn(build["status_after_build"], {"passed", "warning"})
        self.assertEqual(build["color_conversion"], "converted")
        self.assertNotIn("forced_print_build", item)
        self.assertEqual(self.converter.calls, ["26252239"])

        order = self.orders(run_id)["26252239"]
        printed = self.root / "PDF" / "Print" / diagnostic.name
        self.assertEqual(order["status"], "accepted_for_print")
        self.assertEqual(order["pdf_path"], str(printed))
        self.assertEqual((order["pdf_kind"], order["current_pdf_revision"]), ("production", 2))
        self.assertEqual(order["color_conversion"]["status"], "converted")
        # В PDF/Print лежит собранный PDF, а не диагностический файл.
        self.assertNotEqual(hashlib.sha256(printed.read_bytes()).hexdigest(), diagnostic_sha)
        self.assertEqual(list(self.root.glob(f"PDF/*/{diagnostic.name}")), [printed])
        with pymupdf.open(printed) as document:
            spaces = {info["colorspace"] for info in document[0].get_image_info()}
        self.assertEqual(spaces, {4})
        self.assertTrue(any(kind == "full" and ".forced-build-" in path for kind, path in self.prepress.calls))
        self.assertTrue(any(".forced-build-" in path for path in self.pitstop.checked))
        self.assertEqual(order["pitstop"]["checked_revision"], 2)
        self.assertEqual(self.sent, ["26252239"])

        with self.client.app.state.database.session_factory() as session:
            revisions = session.scalars(select(PdfRevision).order_by(PdfRevision.revision_number)).all()
            self.assertEqual(
                [(item.revision_number, item.created_by_action) for item in revisions],
                [(1, None), (2, "print_build")],
            )
            action = session.scalar(select(OrderAction))
            self.assertEqual(action.status, "prepared")
            self.assertEqual(json.loads(action.cms_response_json)["print_build"]["pdf_revision"], 2)

        journal = self.client.get(f"/api/checks/{run_id}/orders/36201:26252239/journal").json()
        codes = {entry["code"]: entry for entry in journal["entries"]}
        self.assertIn("print_build", codes)
        self.assertNotIn("forced_print_build", codes)
        self.assertNotIn("forced_print", codes)
        self.assertEqual(codes["print_build"]["data"]["pdf_revision"], 2)
        self.assertEqual(codes["print_build"]["data"]["status_before_build"], "warning")
        self.assertIn("от 200 DPI при норме 250", codes["print_build"]["detail"])
        self.assertEqual(codes["pdf_revision"]["data"]["created_by_action"], "print_build")
        decision = codes["action:print"]["data"]["print_build"]
        self.assertEqual(decision["pdf_revision"], 2)

    def test_build_failure_keeps_order_unchanged_and_out_of_print(self):
        run_id, order = self.yellow_run()
        diagnostic = Path(order["pdf_path"])
        self.pitstop.fail_forced = True

        item = self.print_orders(run_id, "26252239", confirm=False)["26252239"]

        self.assertEqual((item["status"], item["code"]), ("error", "print_build_failed"), item)
        self.assertIn("PitStop не проверил собранный итоговый PDF", item["message"])
        order = self.orders(run_id)["26252239"]
        self.assertEqual(
            (order["status"], order["pdf_kind"], order["current_pdf_revision"]),
            ("warning", "diagnostic", 1),
        )
        self.assertEqual(order["pdf_path"], str(diagnostic))
        self.assertTrue(diagnostic.is_file())
        print_dir = self.root / "PDF" / "Print"
        self.assertFalse(print_dir.exists() and any(print_dir.iterdir()))
        self.assertTrue((self.root / LOW).is_file())
        self.assertEqual(self.sent, [])
        with self.client.app.state.database.session_factory() as session:
            action = session.scalar(select(OrderAction))
            self.assertEqual(action.status, "failed")
            self.assertEqual(json.loads(action.cms_response_json)["print_build"]["status"], "failed")
            self.assertEqual(len(session.scalars(select(PdfRevision)).all()), 1)

        self.pitstop.fail_forced = False
        self.assertEqual(self.print_orders(run_id, "26252239", confirm=False)["26252239"]["status"], "prepared")
        self.assertEqual(self.orders(run_id)["26252239"]["status"], "accepted_for_print")

    def test_built_pdf_with_errors_needs_the_red_confirmation(self):
        run_id, _order = self.yellow_run()
        self.prepress.blocking = 4  # собранный PDF содержит overprint → «ошибка»

        item = self.print_orders(run_id, "26252239", confirm=False)["26252239"]

        self.assertEqual((item["status"], item["code"]), ("rejected", "forced_print_confirmation"), item)
        self.assertIn("overprint", item["message"])
        self.assertEqual([blocker["code"] for blocker in item["blockers"]], ["forced_print_confirmation"])
        order = self.orders(run_id)["26252239"]
        self.assertEqual(
            (order["status"], order["pdf_kind"], order["current_pdf_revision"]),
            ("error", "production", 2),
        )
        self.assertEqual(order["print_mode"], "forced")
        self.assertFalse((self.root / "PDF" / "Print").exists() and any((self.root / "PDF" / "Print").iterdir()))
        self.assertEqual(self.sent, [])

        # Дальше — обычная печать красного заказа с готовым PDF по подтверждению.
        item = self.print_orders(run_id, "26252239", confirm=True)["26252239"]
        self.assertEqual(item["status"], "prepared", item)
        order = self.orders(run_id)["26252239"]
        self.assertEqual((order["status"], order["current_pdf_revision"]), ("accepted_for_print", 2))
        self.assertEqual(len(self.converter.calls), 1)  # повторной сборки не было

    def test_diagnostic_order_with_wrong_pdf_size_is_red_and_blocked(self):
        make_pdf(self.root / SIZE, dpi=None, size_mm=120.0)
        run_id = self.make_run()
        order = self.orders(run_id)["26252243"]
        self.assertEqual((order["status"], order["pdf_kind"]), ("error", "diagnostic"), order)
        self.assertEqual(order["pitstop"]["verdict"], "passed")
        self.assertEqual(Path(order["pdf_path"]).parent.name, "Failed")
        self.assertEqual(order["print_mode"], "blocked")
        self.assertEqual([blocker["code"] for blocker in order["print_blockers"]], ["no_final_pdf"])
        self.assertFalse(order["print_build"])

        item = self.print_orders(run_id, "26252243", confirm=True)["26252243"]

        self.assertEqual(item["status"], "rejected", item)
        self.assertEqual(self.orders(run_id)["26252243"]["status"], "error")
        self.assertFalse((self.root / "PDF" / "Print").exists() and any((self.root / "PDF" / "Print").iterdir()))
        self.assertEqual(self.sent, [])

    def test_yellow_production_order_prints_without_a_build(self):
        make_pdf(self.root / GOOD_A, dpi=None)
        self.pitstop.warn.add("26252240")
        run_id = self.make_run()
        order = self.orders(run_id)["26252240"]
        self.assertEqual((order["status"], order["pdf_kind"]), ("warning", "production"), order)
        self.assertFalse(order["print_build"])
        merges = len(self.merger.calls)

        item = self.print_orders(run_id, "26252240", confirm=False)["26252240"]

        self.assertEqual(item["status"], "prepared", item)
        self.assertNotIn("print_build", item)
        self.assertEqual(len(self.merger.calls), merges)
        order = self.orders(run_id)["26252240"]
        self.assertEqual((order["status"], order["current_pdf_revision"]), ("accepted_for_print", 1))

    def test_bulk_print_builds_yellow_order_separately(self):
        make_rgb_pdf(self.root / LOW, dpi=200)
        make_pdf(self.root / GOOD_A, dpi=None)
        make_pdf(self.root / SIZE.replace("26252243", "26252241"), dpi=None)
        run_id = self.make_run()
        self.assertEqual(self.orders(run_id)["26252239"]["status"], "warning")
        self.pitstop.fail_forced = True

        items = self.print_orders(run_id, "26252240", "26252239", "26252241", confirm=False)

        self.assertEqual(items["26252240"]["status"], "prepared", items)
        self.assertEqual(items["26252241"]["status"], "prepared", items)
        self.assertEqual(items["26252239"].get("code"), "print_build_failed", items["26252239"])
        # Пакетный запрос Sborka — без заказа, который собирается при печати.
        self.assertEqual(self.sent, [["26252240", "26252241"]])

        self.pitstop.fail_forced = False
        self.assertEqual(self.print_orders(run_id, "26252239", confirm=False)["26252239"]["status"], "prepared")
        self.assertEqual(self.sent[-1], "26252239")


# Унаследованные сценарии красной сборки здесь не повторяем.
for _name in [name for name in vars(forced.ForcedPrintWorkflowTests) if name.startswith("test_")]:
    setattr(PrintBuildWorkflowTests, _name, None)


if __name__ == "__main__":
    unittest.main()
