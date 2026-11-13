"""Допечатные проверки callas: ICC off, шрифты, ppi > 500, плашка K100 (фейковый callas, без сети)."""
from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from processing.batch_processor import BatchProcessor
from processing.models import OrderCheck
from services.callas.analysis import run_analysis
from services.callas.black_solid import analyze_separations
from services.callas.color_conversion import ICC_PROFILE, CallasRgbToCmykConverter, convert_rgb_to_cmyk
from services.callas.prepress import PPI_PROFILE, WARN_FONTS_PROFILE, PrepressAnalyzer
from services.callas.prepress_reports import fonts_report, high_ppi_report, icc_pages
from services.callas.report import parse_report_root
from services.dto import order_check_to_dto
from services.issue_groups import build_issue_groups
from tests.fake_callas import (
    FakeCallasProcessClient,
    fonts_report_xml,
    icc_report_xml,
    make_separations,
    ppi_report_xml,
)
from tests.test_rgb_to_cmyk import make_pdf


def root(xml: str):
    return parse_report_root(xml.encode())


# ----------------------------------------------------------------------------- parsers
def test_icc_pages_from_hits():
    assert icc_pages(root(icc_report_xml([2, 1, 2]))) == [1, 2]
    assert icc_pages(root(icc_report_xml([]))) == []


def test_fonts_are_counted_by_unique_font_not_by_hits():
    xml = fonts_report_xml(
        [("AAAAAA+DejaVuSans", "TrueType", True), ("Helvetica", "Type1", False), ("Courier", "Type1", False)],
        hits_per_font=5, invisible=("Courier",),
    )
    report = fonts_report(root(xml))
    assert report["count"] == 3  # 15 срабатываний, но 3 шрифта
    assert report["not_embedded"] == 2
    by_name = {item["name"]: item for item in report["fonts"]}
    assert by_name["DejaVuSans"]["embedded"] is True and by_name["DejaVuSans"]["type"] == "TrueType"
    assert by_name["Helvetica"]["embedded"] is False
    assert by_name["Courier"]["invisible_only"] is True
    assert fonts_report(root(fonts_report_xml([])))["count"] == 0


def test_high_ppi_filter_tolerance_and_one_bit():
    xml = ppi_report_xml([(1, 600.0, 8), (2, 500.4, 8), (2, 501.0, 8), (1, 1200.0, 1), (3, 300.0, 8), (1, 460.0, 8)])
    report = high_ppi_report(root(xml), 500)
    assert report["count"] == 2 and report["pages"] == [1, 2]  # 600 и 501; 500.4 в допуске
    assert report["max_ppi"] == 600.0
    assert report["one_bit"] == {"count": 1, "pages": [1], "max_ppi": 1200.0}


# ----------------------------------------------------------------------------- ICC off
class IccClient(FakeCallasProcessClient):
    pass


def icc_converter(tmp_path, icc_pages_list):
    def convert(pdf, mode):
        return {"xml": icc_report_xml(icc_pages_list), "summary": {"fixups_applied": 1}, "fixed": make_pdf(tmp_path / "cmyk.pdf", (0, 0, 0, 1))}

    client = IccClient(tmp_path / "jobs", {ICC_PROFILE: lambda pdf, mode: {"xml": icc_report_xml(icc_pages_list)},
                                           "Image Magic/NEW": convert})
    return client, CallasRgbToCmykConverter(client, "Image Magic/NEW")


def test_icc_off_is_recorded_when_icc_was_present(tmp_path):
    client, converter = icc_converter(tmp_path, [1])
    pdf = make_pdf(tmp_path / "a.pdf", (1, 0, 0))
    record = convert_rgb_to_cmyk(pdf, converter, "a")
    assert record["status"] == "converted"
    assert record["icc_present"] is True and record["icc_removed"] is True and record["icc_pages"] == [1]
    assert client.calls[0] == (ICC_PROFILE, "analyze")  # ICC ищем ДО конвертации
    assert client.calls[1] == ("Image Magic/NEW", "fix")


def test_no_icc_off_for_plain_rgb(tmp_path):
    _client, converter = icc_converter(tmp_path, [])
    record = convert_rgb_to_cmyk(make_pdf(tmp_path / "a.pdf", (1, 0, 0)), converter, "a")
    assert record["status"] == "converted" and record["icc_removed"] is False


def test_icc_detection_failure_does_not_block_conversion(tmp_path):
    client, converter = icc_converter(tmp_path, [1])
    client.profiles[ICC_PROFILE] = RuntimeError("callas недоступен")
    record = convert_rgb_to_cmyk(make_pdf(tmp_path / "a.pdf", (1, 0, 0)), converter, "a")
    assert record["status"] == "converted" and record["icc_removed"] is False and "icc_error" in record


def test_cmyk_pdf_skips_conversion_and_icc_analysis(tmp_path):
    client, converter = icc_converter(tmp_path, [1])
    assert convert_rgb_to_cmyk(make_pdf(tmp_path / "a.pdf", (0, 0, 0, 1)), converter, "a") is None
    assert client.calls == []


def test_default_profile_is_new_4c():
    from server.settings import DEFAULT_RGB_TO_CMYK_PROFILE, Settings

    assert DEFAULT_RGB_TO_CMYK_PROFILE == "Image Magic/NEW_Convert color to 4c for digital printing"
    assert Settings.from_env().callas_rgb_to_cmyk_profile  # настройка читается и без env


def test_rgb_presence_decides_not_fixups_counter(tmp_path):
    """fixups_applied > 0 без RGB (blend CS) не означает конвертацию: PDF без RGB не отправляется."""
    client, converter = icc_converter(tmp_path, [])
    assert convert_rgb_to_cmyk(make_pdf(tmp_path / "t.pdf", (0.2, 0.2, 0.2, 0.2)), converter, "t") is None


# ----------------------------------------------------------------------------- analyzer
def analyzer_with(tmp_path, *, fonts=None, ppi=None, k100=None, limit=500.0, threshold=0.25):
    profiles = {
        WARN_FONTS_PROFILE: lambda pdf, mode: {"xml": fonts_report_xml(*fonts) if fonts else fonts_report_xml([])},
        PPI_PROFILE: lambda pdf, mode: {"xml": ppi_report_xml(ppi or [])},
    }
    pages = k100 or [{"k100": 0.0}]
    client = FakeCallasProcessClient(
        tmp_path / "jobs", profiles,
        separations_factory=lambda pdf, out: make_separations(out, pages),
    )
    return client, PrepressAnalyzer(client, high_ppi_limit=limit, black_solid_threshold=threshold)


def pdf_of(tmp_path):
    return make_pdf(tmp_path / "a.pdf", (0, 0, 0, 1))


def test_fonts_block_lists_unique_fonts_with_embedded_flag(tmp_path):
    _c, analyzer = analyzer_with(tmp_path, fonts=([("BirchCTT", "TrueType", True), ("Helvetica", "Type1", False)],))
    result = analyzer(pdf_of(tmp_path), "o1")["fonts"]
    assert result["status"] == "ok" and result["count"] == 2 and result["not_embedded"] == 1
    assert "BirchCTT" in result["message"] and "не встроен" in result["message"]
    assert "Шрифты не встроены, перевод невозможен" in result["message"]


def test_no_fonts_no_message_and_outlined_flag_after_fix(tmp_path):
    _c, analyzer = analyzer_with(tmp_path)
    assert analyzer.check_fonts(pdf_of(tmp_path), "o")["message"] is None
    again = analyzer.check_fonts(pdf_of(tmp_path), "o", {"count": 2})
    assert again["count"] == 0 and again["converted"] is True
    assert again["message"] == "Шрифты переведены в кривые"
    # флаг сохраняется при следующих перепроверках
    assert analyzer.check_fonts(pdf_of(tmp_path), "o", again)["converted"] is True


def test_high_ppi_grouped_warning_and_one_bit_separately(tmp_path):
    _c, analyzer = analyzer_with(tmp_path, ppi=[(1, 600.0, 8), (2, 720.0, 8), (2, 1200.0, 1)])
    result = analyzer(pdf_of(tmp_path), "o")["high_ppi"]
    assert result["count"] == 2
    assert result["message"].startswith("Растры выше 500 ppi: 2 объекта, стр. 1, 2")
    assert "Однобитные растры выше 500 ppi: 1 объект, стр. 2" in result["one_bit_message"]


def test_high_ppi_clean_has_no_message(tmp_path):
    _c, analyzer = analyzer_with(tmp_path, ppi=[(1, 300.0, 8)])
    result = analyzer(pdf_of(tmp_path), "o")["high_ppi"]
    assert result["message"] is None and result["one_bit_message"] is None


@pytest.mark.parametrize("share,detected", [(0.30, True), (0.25, True), (0.20, False)])
def test_black_solid_threshold(tmp_path, share, detected):
    _c, analyzer = analyzer_with(tmp_path, k100=[{"k100": share}])
    result = analyzer(pdf_of(tmp_path), "o")["black_solid"]
    assert result["detected"] is detected
    if detected:
        assert result["message"].startswith("Плашка 100% K:") and "стр. 1" in result["message"]
    else:
        assert result["message"] is None


def test_black_solid_threshold_is_configurable(tmp_path):
    _c, analyzer = analyzer_with(tmp_path, k100=[{"k100": 0.10}], threshold=0.05)
    assert analyzer(pdf_of(tmp_path), "o")["black_solid"]["detected"] is True


def test_rich_or_raster_black_field_asks_for_manual_check(tmp_path):
    _c, analyzer = analyzer_with(tmp_path, k100=[{"k100": 0.0, "rich": 0.6}])
    result = analyzer(pdf_of(tmp_path), "o")["black_solid"]
    assert result["detected"] is False and result["manual_check"] is True
    assert "скорректируйте вручную" in result["message"]


def test_black_solid_pages_listed():
    files_dir = None  # noqa: F841
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        files = make_separations(Path(tmp), [{"k100": 0.5}, {"k100": 0.0}, {"k100": 0.3}])
        result = analyze_separations(files, 0.25)
    assert result["pages"] == [1, 3] and result["share"] == 0.5


def test_failed_check_is_isolated_per_block(tmp_path):
    client, analyzer = analyzer_with(tmp_path)
    client.profiles[WARN_FONTS_PROFILE] = RuntimeError("callas недоступен")
    result = analyzer(pdf_of(tmp_path), "o")
    assert result["fonts"]["status"] == "failed" and "недоступен" in result["fonts"]["error"]
    assert result["high_ppi"]["status"] == "ok" and result["black_solid"]["status"] == "ok"


def test_missing_pdf_is_a_failed_block_not_an_exception(tmp_path):
    _c, analyzer = analyzer_with(tmp_path)
    result = analyzer(tmp_path / "nope.pdf", "o")
    assert result["fonts"]["status"] == "failed" and result["high_ppi"]["status"] == "failed"


def test_jobs_are_cleaned_up(tmp_path):
    client, analyzer = analyzer_with(tmp_path)
    analyzer(pdf_of(tmp_path), "o")
    assert len(client.deleted) == 3


# ----------------------------------------------------------------------------- integration with order
def test_batch_processor_stores_prepress_on_order_dto(tmp_path):
    _c, analyzer = analyzer_with(tmp_path, fonts=([("BirchCTT", "TrueType", True)],))
    order = OrderCheck("1", "2")
    BatchProcessor(tmp_path, tmp_path, pdf_prepress_checker=analyzer)._check_prepress(pdf_of(tmp_path), order)
    dto = order_check_to_dto(order)
    assert dto["prepress"]["fonts"]["count"] == 1


def test_batch_processor_checker_exception_is_isolated(tmp_path):
    def boom(*_a, **_k):
        raise RuntimeError("сломано")

    order = OrderCheck("1", "2")
    BatchProcessor(tmp_path, tmp_path, pdf_prepress_checker=boom)._check_prepress(pdf_of(tmp_path), order)
    assert order.prepress["status"] == "failed"


def test_run_analysis_returns_parsed_root(tmp_path):
    client = FakeCallasProcessClient(tmp_path / "j", {"p": lambda pdf, mode: {"xml": icc_report_xml([1])}})
    result, report_root = run_analysis(client, pdf_of(tmp_path), "p", "x")
    assert result.exit_code == 0 and icc_pages(report_root) == [1]


# ----------------------------------------------------------------------------- clipping group
def test_pitstop_clipping_issues_form_one_group_with_count_pages_and_fix_plan():
    def issue(page):
        return {
            "message": "Clipping objects found (2x on pages 1-2)", "severity": "warning", "action_id": 2079,
            "locations": [{"page": page, "bbox": {"min_x": 0, "min_y": 0, "max_x": 9 + page, "max_y": 9}}],
        }

    groups = build_issue_groups(pitstop_issues=[issue(1), issue(2)])
    assert len(groups) == 1
    group = groups[0]
    assert group["code"] == "action:2079" and group["fix_plan"] == "remove_clipping"
    assert group["objects"] == 2 and group["pages"] == [1, 2]
    assert "Clipping objects found" in group["message"] and "на страницах 1, 2" in group["message"]
