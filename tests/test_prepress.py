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
from services.callas.tac import TAC_LIMIT_PERCENT, analyze_tac, measure_tac
from services.dto import order_check_to_dto
from services.issue_groups import build_issue_groups
from tests.fake_callas import (
    FakeCallasProcessClient,
    fonts_report_xml,
    icc_report_xml,
    make_separations,
    make_tac_separations,
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


def icc_converter(tmp_path, icc_pages_list, after_pages=()):
    """ICC-анализ вызывается дважды: до конвертации (``icc_pages_list``) и по результату (``after_pages``)."""
    analyses = []

    def analyze(pdf, mode):
        analyses.append(pdf)
        return {"xml": icc_report_xml(icc_pages_list if len(analyses) == 1 else after_pages)}

    def convert(pdf, mode):
        return {"xml": icc_report_xml(icc_pages_list), "summary": {"fixups_applied": 1}, "fixed": make_pdf(tmp_path / "cmyk.pdf", (0, 0, 0, 1))}

    client = IccClient(tmp_path / "jobs", {ICC_PROFILE: analyze, "Image Magic/NEW": convert})
    return client, CallasRgbToCmykConverter(client, "Image Magic/NEW")


def test_icc_off_is_recorded_when_icc_was_present(tmp_path):
    client, converter = icc_converter(tmp_path, [1])
    pdf = make_pdf(tmp_path / "a.pdf", (1, 0, 0))
    record = convert_rgb_to_cmyk(pdf, converter, "a")
    assert record["status"] == "converted"
    assert record["icc_present"] is True and record["icc_removed"] is True and record["icc_pages"] == [1]
    assert client.calls[0] == (ICC_PROFILE, "analyze")  # ICC ищем ДО конвертации
    assert client.calls[1] == ("Image Magic/NEW", "fix")
    assert client.calls[2] == (ICC_PROFILE, "analyze")  # и в результате: значок только если ICC исчез
    assert record["icc_verified"] is True and record["icc_remaining_pages"] == []


def test_icc_off_is_not_claimed_when_icc_is_still_in_the_result(tmp_path):
    _client, converter = icc_converter(tmp_path, [1], after_pages=[1])
    record = convert_rgb_to_cmyk(make_pdf(tmp_path / "a.pdf", (1, 0, 0)), converter, "a")
    assert record["status"] == "converted" and record["icc_present"] is True
    assert record["icc_removed"] is False and record["icc_remaining_pages"] == [1]


def test_icc_off_counts_pages_where_icc_disappeared(tmp_path):
    _client, converter = icc_converter(tmp_path, [1, 2], after_pages=[2])
    record = convert_rgb_to_cmyk(make_pdf(tmp_path / "a.pdf", (1, 0, 0)), converter, "a")
    assert record["icc_removed"] is True and record["icc_pages"] == [1, 2] and record["icc_remaining_pages"] == [2]


def test_icc_off_when_result_check_fails_falls_back_to_profile_behaviour(tmp_path):
    calls = {"n": 0}

    def analyze(pdf, mode):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("callas недоступен")
        return {"xml": icc_report_xml([1])}

    cmyk = make_pdf(tmp_path / "cmyk.pdf", (0, 0, 0, 1))
    client = IccClient(tmp_path / "jobs", {ICC_PROFILE: analyze, "Image Magic/NEW": lambda pdf, mode: {"fixed": cmyk}})
    record = convert_rgb_to_cmyk(
        make_pdf(tmp_path / "a.pdf", (1, 0, 0)), CallasRgbToCmykConverter(client, "Image Magic/NEW"), "a"
    )
    assert record["status"] == "converted" and record["icc_removed"] is True and record["icc_verified"] is False


def _with_output_intent(path):
    import pymupdf

    with pymupdf.open(str(path)) as document:
        document.xref_set_key(
            document.pdf_catalog(), "OutputIntents",
            "[<< /Type /OutputIntent /S /GTS_PDFX /OutputConditionIdentifier (ISO Coated) >>]",
        )
        document.saveIncr()
    return path


def test_output_intent_removal_shows_icc_off_even_without_icc_objects(tmp_path):
    from services.callas.color_conversion import has_output_intent

    _client, converter = icc_converter(tmp_path, [])
    pdf = _with_output_intent(make_pdf(tmp_path / "a.pdf", (1, 0, 0)))
    assert has_output_intent(pdf) is True
    record = convert_rgb_to_cmyk(pdf, converter, "a")
    assert record["status"] == "converted" and record["icc_present"] is False
    assert record["output_intent_removed"] is True and record["icc_removed"] is True
    assert has_output_intent(pdf) is False


def test_output_intent_kept_by_conversion_is_not_reported_as_removed(tmp_path):
    cmyk = _with_output_intent(make_pdf(tmp_path / "cmyk.pdf", (0, 0, 0, 1)))
    client = IccClient(tmp_path / "jobs", {ICC_PROFILE: {"xml": icc_report_xml([])}, "Image Magic/NEW": lambda pdf, mode: {"fixed": cmyk}})
    pdf = _with_output_intent(make_pdf(tmp_path / "a.pdf", (1, 0, 0)))
    record = convert_rgb_to_cmyk(pdf, CallasRgbToCmykConverter(client, "Image Magic/NEW"), "a")
    assert record["output_intent_removed"] is False and record["icc_removed"] is False


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
    assert result["fix_available"] is True  # плашка и меньшая доля K100: кнопка доступна
    if detected:
        assert result["message"].startswith("Плашка 100% K:") and "стр. 1" in result["message"]
    else:
        assert result["message"].startswith("Есть чистый K100:") and "составной чёрный" in result["message"]


@pytest.mark.parametrize("share,available", [(0.10, True), (0.02, True), (0.01, True), (0.005, False), (0.0, False)])
def test_black_fix_is_available_for_small_pure_k100_areas(tmp_path, share, available):
    """Кнопка «в составной чёрный» не ждёт 25% площади: хватает малой доли чистого K100 (порог 1%)."""
    _c, analyzer = analyzer_with(tmp_path, k100=[{"k100": share}])
    result = analyzer(pdf_of(tmp_path), "o")["black_solid"]
    assert result["status"] == "ok" and result["detected"] is False
    assert result["fix_available"] is available
    assert result["fix_pages"] == ([1] if available else [])
    assert (result["message"] is not None) is available


def test_black_fix_threshold_is_configurable_and_never_above_solid_threshold(tmp_path):
    client = FakeCallasProcessClient(
        tmp_path / "jobs", {}, separations_factory=lambda pdf, out: make_separations(out, [{"k100": 0.10}]),
    )
    strict = PrepressAnalyzer(client, black_solid_threshold=0.25, black_solid_fix_threshold=0.2)
    assert strict.check_black_solid(pdf_of(tmp_path), "o")["fix_available"] is False
    capped = PrepressAnalyzer(client, black_solid_threshold=0.05, black_solid_fix_threshold=0.5)
    assert capped.black_solid_fix_threshold == 0.05
    assert capped.check_black_solid(pdf_of(tmp_path), "o")["detected"] is True


def test_black_solid_threshold_is_configurable(tmp_path):
    _c, analyzer = analyzer_with(tmp_path, k100=[{"k100": 0.10}], threshold=0.05)
    assert analyzer(pdf_of(tmp_path), "o")["black_solid"]["detected"] is True


def test_rich_or_raster_black_field_asks_for_manual_check(tmp_path):
    _c, analyzer = analyzer_with(tmp_path, k100=[{"k100": 0.0, "rich": 0.6}])
    result = analyzer(pdf_of(tmp_path), "o")["black_solid"]
    assert result["detected"] is False and result["manual_check"] is True and result["fix_available"] is False
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


def test_pitstop_ink_coverage_group_offers_the_tac_fix_plan():
    issue = {"message": "Total ink coverage of an object is more than 310%", "severity": "warning", "action_id": 1153}
    groups = build_issue_groups(pitstop_issues=[issue, dict(issue, action_id="1153")])
    assert len(groups) == 1
    group = groups[0]
    assert group["code"] == "action:1153" and group["fix_plan"] == "reduce_tac"
    assert group["message"].startswith("Суммарное покрытие красок выше нормы: Total ink coverage (PitStop)")
    assert group["samples"] == ["Total ink coverage of an object is more than 310%"]
    # Одна находка тоже получает подпись группы (как обтравка), исходный текст — в samples.
    single = build_issue_groups(pitstop_issues=[issue])[0]
    assert single["fix_plan"] == "reduce_tac" and single["message"].startswith("Суммарное покрытие красок выше нормы")


# ----------------------------------------------------------------------------- TAC
def test_tac_of_full_four_inks_is_400_percent(tmp_path):
    files = make_tac_separations(tmp_path, [{"cmyk": (100, 100, 100, 100)}])
    result = analyze_tac(files)
    assert TAC_LIMIT_PERCENT == 310.0
    assert result["max_percent"] == 400.0 and result["detected"] is True
    assert result["pages"] == [1] and result["over_share"] == 1.0


def test_tac_below_limit_is_not_detected_and_white_page_is_zero(tmp_path):
    files = make_tac_separations(tmp_path, [{"cmyk": (100, 80, 40, 40), "share": 0.5}, {}])
    result = analyze_tac(files)
    assert result["max_percent"] == 260.0 and result["detected"] is False
    assert result["pages"] == [] and result["over_share"] == 0.0
    assert result["page_values"] == {
        "1": {"max_percent": 260.0, "over_share": 0.0}, "2": {"max_percent": 0.0, "over_share": 0.0},
    }


def test_tac_area_above_limit_is_counted_per_page_and_limit_is_strict(tmp_path):
    files = make_tac_separations(tmp_path, [
        {"cmyk": (100, 100, 60, 60), "share": 0.25},  # 320%
        {"cmyk": (100, 100, 100, 20)},                 # ровно 320%, вся страница
        {"cmyk": (80, 80, 80, 60)},                    # ровно 300%
    ])
    result = analyze_tac(files)
    assert result["pages"] == [1, 2] and result["over_share"] == 1.0
    assert result["page_values"]["1"] == {"max_percent": 320.0, "over_share": 0.25}
    assert analyze_tac(files, 320.0)["detected"] is False  # равно пределу — не выше
    assert analyze_tac(files, 299.0)["pages"] == [1, 2, 3]


def test_tac_ignores_spot_channels_and_incomplete_pages(tmp_path):
    files = make_tac_separations(tmp_path, [{"cmyk": (60, 60, 60, 60)}])
    spot = tmp_path / "spot.png"
    from PIL import Image
    Image.new("L", (40, 40), 0).save(spot)
    files[(1, "PANTONE 485 C")] = spot
    files[(2, "Black")] = spot  # страница без C, M, Y пропускается
    result = analyze_tac(files)
    assert result["max_percent"] == 240.0 and list(result["page_values"]) == ["1"]


def test_check_tac_cleans_up_and_failure_is_a_block(tmp_path):
    client = FakeCallasProcessClient(
        tmp_path / "jobs", {}, separations_factory=lambda pdf, out: make_tac_separations(out, [{"cmyk": (100, 100, 100, 100)}]),
    )
    analyzer = PrepressAnalyzer(client)
    result = analyzer.check_tac(pdf_of(tmp_path), "x")
    assert result["status"] == "ok" and result["max_percent"] == 400.0 and result["limit"] == 310.0
    assert len(client.deleted) == 1 and client.deleted[0].startswith("sep-tac-x-")
    assert measure_tac(client, pdf_of(tmp_path), "y", 410.0)["detected"] is False
    client.separations_factory = None
    failed = analyzer.check_tac(pdf_of(tmp_path), "z")
    assert failed["status"] == "failed" and "separations" in failed["error"]


def test_tac_is_not_part_of_the_automatic_order_checks(tmp_path):
    client, analyzer = analyzer_with(tmp_path)
    assert set(analyzer(pdf_of(tmp_path), "o")) == {"fonts", "high_ppi", "black_solid"}
    assert [call for call in client.calls if call[0] == "separations"] == [("separations", "sep_process")]
