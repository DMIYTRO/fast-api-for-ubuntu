"""Тесты разбора XML-отчёта pdfToolbox. Образец собран по описанию в docs/callas-preflight-plan.md."""

from __future__ import annotations

from pathlib import Path

import pytest

from services.callas import CallasFinding, CallasReportError, parse_report_bytes, parse_report_xml

SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<report>
  <profile_info>
    <profile_name>Image Magic/size_correction</profile_name>
    <rules>
      <rule id="RUL71" display_name="Размер страницы не совпадает с заказом"/>
      <rule id="RUL72"><display_name>Overprint  in
        use</display_name></rule>
      <rule id="RUL73" display_name="Информация о шрифтах"/>
    </rules>
  </profile_info>
  <results>
    <hits rule_id="RUL71" severity="error">
      <hit page="1" llx="0" lly="0.5" urx="294.8" ury="595.3"/>
      <hit page="2" llx="0" lly="0" urx="294.8" ury="595.3"/>
    </hits>
    <hits rule_id="RUL72" severity="warning">
      <hit page="1" llx="10" lly="20" urx="30" ury="40"/>
    </hits>
    <hits rule_id="RUL73" severity="information"/>
  </results>
</report>
"""


def test_parses_findings_with_names_pages_and_bboxes():
    findings = parse_report_bytes(SAMPLE.encode("utf-8"))
    assert findings == [
        CallasFinding("Размер страницы не совпадает с заказом", "error", 1, (0.0, 0.5, 294.8, 595.3), "RUL71"),
        CallasFinding("Размер страницы не совпадает с заказом", "error", 2, (0.0, 0.0, 294.8, 595.3), "RUL71"),
        CallasFinding("Overprint in use", "warning", 1, (10.0, 20.0, 30.0, 40.0), "RUL72"),
        CallasFinding("Информация о шрифтах", "info", None, None, "RUL73"),
    ]


def test_rule_key_is_name_not_rule_id():
    """rule_id — порядковый номер в отчёте: тот же план в другом отчёте имеет другие номера."""
    other = SAMPLE.replace("RUL71", "RUL5").replace("RUL72", "RUL6")
    first = {f.rule for f in parse_report_bytes(SAMPLE.encode())}
    second = {f.rule for f in parse_report_bytes(other.encode())}
    assert first == second


def test_namespaced_report_and_file_reading(tmp_path: Path):
    text = SAMPLE.replace("<report>", '<report xmlns="http://www.callassoftware.com/ns/report">')
    path = tmp_path / "r.xml"
    path.write_text(text, encoding="utf-8")
    assert len(parse_report_xml(path)) == 4


def test_unknown_rule_id_falls_back_to_id_and_bad_numbers_are_dropped():
    text = (
        '<report><hits rule_id="RUL9" severity="Warning">'
        '<hit page="x" llx="a" lly="0" urx="1" ury="1"/></hits></report>'
    )
    [finding] = parse_report_bytes(text.encode())
    assert finding.rule == "RUL9"
    assert finding.severity == "warning"
    assert finding.page is None and finding.bbox is None


def test_empty_report_has_no_findings():
    assert parse_report_bytes(b"<report><profile_info/></report>") == []


@pytest.mark.parametrize(
    "payload",
    [
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><report>&x;</report>',
        b'<!DOCTYPE lolz [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><report>&b;</report>',
        b"<!doctype report><report/>",
        "<!DOCTYPE r [<!ENTITY x 'y'>]><r/>".encode("utf-16"),
    ],
)
def test_xxe_and_entity_bombs_are_rejected(payload):
    with pytest.raises(CallasReportError, match="DOCTYPE"):
        parse_report_bytes(payload)


def test_malformed_and_missing_files(tmp_path: Path):
    with pytest.raises(CallasReportError, match="повреждён"):
        parse_report_bytes(b"<report><hits>")
    with pytest.raises(CallasReportError, match="Не удалось прочитать"):
        parse_report_xml(tmp_path / "missing.xml")
