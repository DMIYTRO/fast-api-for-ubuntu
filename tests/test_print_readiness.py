"""Правила «В печать»: зелёные и жёлтые заказы, красные только с подтверждением и готовым PDF."""
from pathlib import Path

from services.print_readiness import is_forced_print, pitstop_ready, print_blockers, print_mode


def pitstop(verdict="passed", execution="completed", revision=1):
    return {"execution_status": execution, "verdict": verdict, "checked_revision": revision}


def codes(order):
    return [item["code"] for item in print_blockers(order)]


def test_green_and_yellow_orders_have_no_blockers():
    for status in ("passed", "warning"):
        order = {"status": status, "current_pdf_revision": 1, "pitstop": pitstop()}
        assert print_blockers(order) == []
        assert print_mode([]) == "normal"


def test_yellow_order_with_pitstop_error_verdict_is_printable_once_pitstop_finished():
    order = {"status": "warning", "current_pdf_revision": 1, "pitstop": pitstop("error")}
    assert print_blockers(order) == []
    assert pitstop_ready(order)


def test_unfinished_or_stale_pitstop_blocks_every_status():
    for status in ("passed", "warning"):
        assert codes({"status": status, "current_pdf_revision": 1, "pitstop": pitstop(execution="failed")}) == ["pitstop_not_passed"]
        assert codes({"status": status, "current_pdf_revision": 2, "pitstop": pitstop(revision=1)}) == ["pitstop_stale"]


def test_red_order_with_ready_final_pdf_requires_confirmation(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF")
    order = {"status": "error", "pdf_path": str(pdf), "pdf_kind": "production", "current_pdf_revision": 1, "pitstop": pitstop()}
    blockers = print_blockers(order)
    assert [(b["kind"], b["code"]) for b in blockers] == [("confirm", "forced_print_confirmation")]
    assert "Отправить в печать" in blockers[0]["message"]
    assert print_mode(blockers) == "forced"
    assert is_forced_print(order)


def test_red_order_without_final_pdf_is_blocked_with_actionable_message(tmp_path):
    diagnostic = tmp_path / "diag.pdf"
    diagnostic.write_bytes(b"%PDF")
    for order in ({"status": "error"}, {"status": "error", "pdf_path": str(diagnostic), "pdf_kind": "diagnostic"}):
        blockers = print_blockers(order)
        assert blockers[0]["code"] == "no_final_pdf"
        assert "Готового итогового PDF нет" in blockers[0]["message"]
        assert print_mode(blockers) == "blocked"
    missing = {"status": "error", "pdf_path": str(tmp_path / "gone.pdf"), "current_pdf_revision": 1}
    assert print_mode(print_blockers(missing)) == "blocked"


def test_red_order_with_pending_pitstop_or_approval_stays_blocked(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF")
    base = {"status": "error", "pdf_path": str(pdf), "current_pdf_revision": 1, "pitstop": pitstop()}
    assert not is_forced_print({**base, "pitstop": pitstop(execution="failed")})
    assert not is_forced_print({**base, "current_pdf_revision": 2})
    assert not is_forced_print({**base, "pitstop": None, "pitstop_skipped_reason": "не запускался"})
    assert not is_forced_print({**base, "pending_confirmations": 1})
    assert not is_forced_print(base, {"status": "ready"})


def test_processing_order_is_not_printable():
    assert codes({"status": "processing"}) == ["check_failed"]
