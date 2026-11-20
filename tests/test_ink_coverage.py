"""Разбор ответа callas ``inkcoverage``, светлые участки и перелив, DTO заказа."""

from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path

import pytest

from services.callas.errors import CallasProtocolError
from services.callas.ink_coverage import (
    InkCoverageResult,
    LightAreas,
    LightAreaSettings,
    InkCoverageThresholds,
    build_ink_coverage_checker,
    evaluate,
    parse_ink_coverage,
    thresholds_for,
    to_dto,
)

# Синтетический пример документированного формата; сетевые вызовы не выполняются.
SERVICE_RESPONSE = {
    "job_id": "ink-1",
    "type": "inkcoverage",
    "status": "done",
    "exit_code": 0,
    "files": [],
    "ink_coverage": {
        "resolution": 72,
        "box": "TrimBox",
        "pages": [
            {
                "page": 2,
                "inks": [
                    {"name": "Cyan", "kind": "process", "percent": 100.0},
                    {"name": "Magenta", "kind": "process", "percent": 100.0},
                    {"name": "Yellow", "kind": "process", "percent": 100.0},
                    {"name": "Black", "kind": "process", "percent": 100.0},
                ],
                "process_sum_percent": 400.0,
                "total_sum_percent": 400.0,
            },
            {
                "page": 1,
                "inks": [
                    {"name": "Cyan", "kind": "process", "percent": 2.023},
                    {"name": "Magenta", "kind": "process", "percent": 2.023},
                    {"name": "Yellow", "kind": "process", "percent": 2.023},
                    {"name": "Black", "kind": "process", "percent": 2.023},
                    {"name": "PANTONE 185 C", "kind": "spot", "percent": 3.0},
                ],
                "process_sum_percent": 8.092,
                "total_sum_percent": 11.092,
            },
        ],
        "document": {
            "inks": [{"name": "Cyan", "kind": "process", "percent": 51.0115}],
            "process_sum_percent": 204.046,
            "total_sum_percent": 204.046,
        },
    },
}
THRESHOLDS = InkCoverageThresholds(min_percent=20.0, max_percent=250.0)


def test_parser_takes_page_min_max_from_total_sum_percent():
    result = parse_ink_coverage(SERVICE_RESPONSE)

    assert result.status == "completed"
    assert result.resolution == 72 and result.box == "TrimBox"
    assert [page.page for page in result.pages] == [1, 2]  # отсортировано
    assert result.pages[0].inks[4].kind == "spot"
    # Значение страницы — total_sum_percent (включая плашки), не блок document.
    assert (result.document_min_percent, result.min_page) == (11.092, 1)
    assert (result.document_max_percent, result.max_page) == (400.0, 2)


def test_parser_accepts_bare_ink_coverage_block_and_fills_missing_sums():
    block = {
        "resolution": 10,
        "box": "CropBox",
        "pages": [
            {"page": 1, "inks": [{"name": "Cyan", "percent": 5.0}, {"name": "Gold", "percent": 1.5}]},
            {"page": 2, "inks": []},
        ],
    }
    result = parse_ink_coverage(block)

    page = result.pages[0]
    assert page.inks[0].kind == "process" and page.inks[1].kind == "spot"
    assert page.process_sum_percent == 5.0 and page.total_sum_percent == 6.5
    assert result.pages[1].total_sum_percent == 0.0
    assert (result.document_min_percent, result.min_page) == (0.0, 2)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.pop("pages"),
        lambda b: b["pages"].append("x"),
        lambda b: b["pages"][0].update(page=0),
        lambda b: b["pages"][0]["inks"].append({"name": "Cyan"}),
    ],
)
def test_parser_rejects_malformed_payload(mutate):
    block = copy.deepcopy(SERVICE_RESPONSE["ink_coverage"])
    mutate(block)
    with pytest.raises(CallasProtocolError):
        parse_ink_coverage(block, job_id="ink-1")


def test_low_page_average_is_not_a_flag_any_more():
    """Среднее 11% на стр. 1 само по себе флага не даёт: важны светлые участки."""
    flags = evaluate(parse_ink_coverage(SERVICE_RESPONSE), THRESHOLDS)

    assert [(f.kind, f.page, f.percent, f.threshold) for f in flags] == [("overflow", 2, 400.0, 250.0)]
    assert flags[0].label == "Перелив"
    assert flags[0].describe() == "Перелив: 400% на стр. 2 (порог 250%)"


def test_light_areas_flag_every_page_over_the_area_share():
    light = LightAreas(
        status="completed", tac_max_percent=20.0, settings=LightAreaSettings(5.0, 2.5),
        pages=((1, 18.1), (2, 51.9)),
    )
    result = replace(parse_ink_coverage(SERVICE_RESPONSE), light_areas=light)

    flags = evaluate(result, InkCoverageThresholds(20.0, None))

    assert [(f.kind, f.page, f.percent) for f in flags] == [("light_areas", 1, 18.1), ("light_areas", 2, 51.9)]
    assert flags[0].label == "Светлые участки"
    assert flags[1].describe() == "Светлые участки: 51.9% площади стр. 2 светлее 20%"
    below = replace(result, light_areas=replace(light, pages=((1, 4.9), (2, 0.0))))
    assert evaluate(below, InkCoverageThresholds(20.0, None)) == []
    failed = replace(result, light_areas=LightAreas(status="failed", error="нет службы"))
    assert evaluate(failed, InkCoverageThresholds(20.0, None)) == []


def test_evaluate_has_no_flags_inside_thresholds_or_without_data():
    result = parse_ink_coverage(SERVICE_RESPONSE)
    assert evaluate(result, InkCoverageThresholds(5.0, 400.0)) == []
    assert evaluate(result, None) == []
    assert evaluate(InkCoverageResult.failed("boom"), THRESHOLDS) == []
    assert evaluate(InkCoverageResult.not_checked(), THRESHOLDS) == []


def test_to_dto_shape():
    result = parse_ink_coverage(SERVICE_RESPONSE)

    dto = to_dto(result, THRESHOLDS)

    assert set(dto) == {"status", "resolution", "box", "document", "pages", "flags", "thresholds", "light_areas", "error"}
    assert dto["light_areas"] is None
    assert dto["status"] == "completed" and dto["error"] is None
    assert dto["document"] == {"min_percent": 11.092, "max_percent": 400.0, "min_page": 1, "max_page": 2}
    assert dto["thresholds"] == {"min_percent": 20.0, "max_percent": 250.0}
    assert dto["pages"][0]["page"] == 1
    assert dto["pages"][0]["inks"][0] == {"name": "Cyan", "kind": "process", "percent": 2.023}
    assert set(dto["pages"][0]) == {"page", "total_sum_percent", "process_sum_percent", "inks"}
    assert dto["flags"] == [{"kind": "overflow", "page": 2, "percent": 400.0, "threshold": 250.0}]


def test_failed_result_dto_keeps_error_and_empty_pages():
    dto = to_dto(InkCoverageResult.failed("Callas недоступен"), THRESHOLDS)
    assert dto["status"] == "failed"
    assert dto["error"] == "Callas недоступен"
    assert dto["pages"] == [] and dto["flags"] == []
    assert dto["document"]["min_percent"] is None


def test_thresholds_come_from_profile_with_env_override():
    assert thresholds_for("digital") == InkCoverageThresholds()
    assert thresholds_for("offset") == InkCoverageThresholds()
    assert thresholds_for("digital", min_percent_override=15, max_percent_override=None) == InkCoverageThresholds(15.0, None)


class _FakeClient:
    def __init__(self, share: Path, *, fail: Exception | None = None):
        self.share = share
        self.fail = fail
        self.calls: list[dict] = []

    def job_dir(self, job_id: str) -> Path:
        return self.share / ".callas" / job_id

    def ink_coverage(self, pdf, *, resolution, box, job_id):
        self.calls.append({"pdf": pdf, "resolution": resolution, "box": box, "job_id": job_id})
        (self.job_dir(job_id) / "in").mkdir(parents=True)
        if self.fail:
            raise self.fail
        return parse_ink_coverage(SERVICE_RESPONSE)


def test_checker_returns_dto_and_cleans_job_dir(tmp_path: Path):
    pdf = tmp_path / "order.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    client = _FakeClient(tmp_path)
    check = build_ink_coverage_checker(client, min_percent_override=20, max_percent_override=300)

    dto = check(pdf, "digital")

    assert dto["status"] == "completed"
    assert dto["thresholds"] == {"min_percent": 20.0, "max_percent": 300.0}
    # Клиент без цветоделений: светлые участки не проверялись, флаг по среднему не ставится.
    assert [f["kind"] for f in dto["flags"]] == ["overflow"]
    assert dto["light_areas"]["status"] == "not_checked"
    call = client.calls[0]
    assert (call["resolution"], call["box"]) == (72, "TrimBox")
    assert call["job_id"].startswith("callas-ink-")
    assert not client.job_dir(call["job_id"]).exists()
    # Повтор того же PDF даёт тот же job_id.
    check(pdf, "digital")
    assert client.calls[1]["job_id"] == call["job_id"]


def test_checker_never_raises_and_reports_failure(tmp_path: Path):
    pdf = tmp_path / "order.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    client = _FakeClient(tmp_path, fail=RuntimeError("служба недоступна"))

    dto = build_ink_coverage_checker(client)(pdf, "offset")

    assert dto["status"] == "failed"
    assert "служба недоступна" in dto["error"]
    assert dto["flags"] == [] and dto["thresholds"] == {"min_percent": None, "max_percent": None}
    assert client.job_dir(client.calls[0]["job_id"]).exists()


@pytest.mark.parametrize("raw", [{"page": 1}, {"page": 1, "total_sum_percent": float("nan")}, {"page": 1, "inks": "invalid"}, {"page": 1, "inks": [{"name": "Cyan", "percent": float("inf")}]}, {"page": 1, "total_sum_percent": -1}])
def test_missing_or_invalid_measurements_never_become_underflow(raw):
    with pytest.raises(CallasProtocolError):
        parse_ink_coverage({"pages": [raw]})


def test_no_configured_thresholds_means_no_flags():
    assert evaluate(parse_ink_coverage(SERVICE_RESPONSE), thresholds_for("digital")) == []


@pytest.mark.parametrize("minimum, maximum", [(float("nan"), 250), (-1, 250), (251, 250)])
def test_invalid_thresholds_rejected(minimum, maximum):
    with pytest.raises(ValueError):
        InkCoverageThresholds(minimum, maximum)


def test_single_configured_bound_is_evaluated():
    result = parse_ink_coverage(SERVICE_RESPONSE)
    assert [f.kind for f in evaluate(result, InkCoverageThresholds(max_percent=250))] == ["overflow"]
    assert evaluate(result, InkCoverageThresholds(min_percent=20)) == []


def test_checker_reports_invalid_configuration_without_raising(tmp_path):
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-test")
    check = build_ink_coverage_checker(_FakeClient(tmp_path), min_percent_override=300, max_percent_override=250)
    assert check(pdf, "digital")["status"] == "failed"


# --- светлые участки по цветоделениям ----------------------------------------------

def _separation(path: Path, size, regions):
    """PNG цветоделения callas: 255 — краски нет; ``regions`` — [(box, процент краски)]."""
    from PIL import Image, ImageDraw

    image = Image.new("L", size, 255)
    draw = ImageDraw.Draw(image)
    for box, percent in regions:
        draw.rectangle(box, fill=round(255 - percent * 255 / 100))
    image.save(path)
    return path


def _page(tmp_path: Path, page: int, cyan=(), black=()):
    size = (200, 100)
    return {
        (page, "Cyan"): _separation(tmp_path / f"p{page}-c.png", size, cyan),
        (page, "Magenta"): _separation(tmp_path / f"p{page}-m.png", size, ()),
        (page, "Yellow"): _separation(tmp_path / f"p{page}-y.png", size, ()),
        (page, "Black"): _separation(tmp_path / f"p{page}-k.png", size, black),
    }


def test_same_light_background_is_found_on_both_sides(tmp_path):
    """26226163: одинаковый фон C10 на обеих сторонах, на лице ещё тёмная картинка."""
    from services.callas.ink_coverage import light_area_share

    background = ((0, 0, 199, 99), 10)
    face = _page(tmp_path, 1, cyan=[background], black=[((0, 0, 99, 99), 90)])
    back = _page(tmp_path, 2, cyan=[background])

    share_face = light_area_share(*(face[(1, ink)] for ink in ("Cyan", "Magenta", "Yellow", "Black")),
                                  tac_max_percent=20, min_width_mm=2.5)
    share_back = light_area_share(*(back[(2, ink)] for ink in ("Cyan", "Magenta", "Yellow", "Black")),
                                  tac_max_percent=20, min_width_mm=2.5)

    assert share_face == pytest.approx(50, abs=1)  # правая половина — светлый фон
    assert share_back == pytest.approx(100, abs=0.1)


def test_white_paper_dark_areas_and_thin_strokes_are_not_light_areas(tmp_path):
    from services.callas.ink_coverage import light_area_share

    page = _page(
        tmp_path, 1,
        cyan=[((0, 0, 99, 99), 60), ((150, 0, 151, 99), 10)],  # тёмный блок и линия 2 px C10
    )
    share = light_area_share(*(page[(1, ink)] for ink in ("Cyan", "Magenta", "Yellow", "Black")),
                             tac_max_percent=20, min_width_mm=2.5)
    assert share == 0.0


class _SeparationsClient(_FakeClient):
    def __init__(self, share: Path, files, *, fail_separations: Exception | None = None):
        super().__init__(share)
        self.files = files
        self.fail_separations = fail_separations
        self.deleted: list[str] = []

    def separations(self, pdf, *, resolution, job_id):
        self.calls.append({"separations": job_id, "resolution": resolution})
        (self.job_dir(job_id) / "out").mkdir(parents=True)
        if self.fail_separations:
            raise self.fail_separations
        return self.files

    def delete(self, job_id):
        self.deleted.append(job_id)


def test_checker_flags_light_areas_on_both_pages_and_cleans_up(tmp_path):
    pdf = tmp_path / "order.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    background = ((0, 0, 199, 99), 10)
    files = {**_page(tmp_path, 1, cyan=[background], black=[((0, 0, 99, 99), 90)]),
             **_page(tmp_path, 2, cyan=[background])}
    client = _SeparationsClient(tmp_path, files)

    dto = build_ink_coverage_checker(client, min_percent_override=20)(pdf, "digital")

    assert [(f["kind"], f["page"]) for f in dto["flags"]] == [("light_areas", 1), ("light_areas", 2)]
    assert dto["light_areas"]["status"] == "completed"
    assert [p["page"] for p in dto["light_areas"]["pages"]] == [1, 2]
    assert dto["light_areas"]["tac_max_percent"] == 20.0
    job_id = client.calls[-1]["separations"]
    assert client.calls[-1]["resolution"] == 50
    assert client.deleted == [job_id] and not client.job_dir(job_id).exists()


def test_separation_failure_keeps_ink_coverage_and_reports_it(tmp_path):
    pdf = tmp_path / "order.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    client = _SeparationsClient(tmp_path, {}, fail_separations=RuntimeError("тип separations выключен"))

    dto = build_ink_coverage_checker(client, min_percent_override=20)(pdf, "digital")

    assert dto["status"] == "completed" and dto["flags"] == []
    assert dto["light_areas"]["status"] == "failed"
    assert "separations" in dto["light_areas"]["error"]


def test_light_areas_not_searched_without_minimum_threshold(tmp_path):
    pdf = tmp_path / "order.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    client = _SeparationsClient(tmp_path, {})

    dto = build_ink_coverage_checker(client)(pdf, "digital")

    assert dto["light_areas"] is None
    assert not any("separations" in call for call in client.calls)
