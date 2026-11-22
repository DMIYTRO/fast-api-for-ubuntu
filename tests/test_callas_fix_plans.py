"""Планы Callas: шрифты в кривые, составной чёрный, удаление обтравки (фейковый callas, без сети)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from config.callas_plans import CALLAS_PLANS, DEFAULT_DOWNSAMPLE_PROFILE, with_profile_overrides
from services.callas.fix_checks import build_post_checks
from services.callas.prepress import PrepressAnalyzer, WARN_FONTS_PROFILE
from services.callas_fix import CallasFixError, CallasFixService
from services.callas.prepress import PPI_PROFILE
from tests.fake_callas import (
    FakeCallasProcessClient,
    fonts_report_xml,
    make_separations,
    make_tac_separations,
    ppi_report_xml,
)
from tests.test_rgb_to_cmyk import make_pdf

FONTS = CALLAS_PLANS["fonts_to_outlines"].profile
BLACK = CALLAS_PLANS["black_to_rich"].profile
CLIP = CALLAS_PLANS["remove_clipping"].profile
DOWN = CALLAS_PLANS["downsample_images"].profile
TAC = CALLAS_PLANS["reduce_tac"].profile


def fake_compare(changed: float):
    def compare(original, fixed, out_dir):
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        for name in ("original", "result", "overlay"):
            (out / f"{name}.png").write_bytes(b"png")
        return SimpleNamespace(
            original_image=out / "original.png", result_image=out / "result.png",
            overlay_image=out / "overlay.png",
            summary={"changed_percent": changed, "original": {}, "result": {}},
        )

    return compare


class Env:
    def __init__(
        self, tmp: Path, *, changed=0.0, k100_before=0.5, k100_after=0.0, fonts_after=(), ppi_after=(),
        text_before=0.0, text_after=0.0,
    ):
        self.tmp = tmp
        pdf_dir = tmp / "PDF"
        pdf_dir.mkdir()
        self.source = make_pdf(pdf_dir / "order.pdf", (0, 0, 0, 1))
        self.fixed = make_pdf(tmp / "fixed_source.pdf", (0, 0, 0, 1))
        self.shares = {"source": k100_before, "fixed": k100_after}
        self.text = {"source": text_before, "fixed": text_after}
        self.fonts_after = list(fonts_after)
        self.fonts_calls = 0
        self.ppi_after = list(ppi_after)
        self.ppi_before = [(1, 720.0, 8)]

        def ppi_handler(pdf, mode):
            return {"xml": ppi_report_xml(self.ppi_before if Path(pdf).name == "order.pdf" else self.ppi_after)}

        def fonts_handler(pdf, mode):
            # Первый вызов Warn Fonts — после фикса (на результате).
            self.fonts_calls += 1
            return {"xml": fonts_report_xml(self.fonts_after)}

        def seps(pdf, out):
            key = "fixed" if Path(pdf).name != "order.pdf" else "source"
            return make_separations(out, [{"k100": self.shares[key], "text": self.text[key]}])

        self.profiles = {
            WARN_FONTS_PROFILE: fonts_handler,
            FONTS: lambda pdf, mode: {"summary": {"fixups_applied": 2}, "exit_code": 5, "xml": "<r/>", "fixed": self.fixed},
            BLACK: lambda pdf, mode: {"summary": {"fixups_applied": 1}, "exit_code": 5, "xml": "<r/>", "fixed": self.fixed},
            CLIP: lambda pdf, mode: {"summary": {"fixups_applied": 1}, "exit_code": 5, "xml": "<r/>", "fixed": self.fixed},
            DOWN: lambda pdf, mode: {"summary": {"fixups_applied": 0}, "exit_code": 5, "xml": "<r/>", "fixed": self.fixed},
            TAC: lambda pdf, mode: {"summary": {"fixups_applied": 2}, "exit_code": 5, "xml": "<r/>", "fixed": self.fixed},
            PPI_PROFILE: ppi_handler,
        }
        self.client = FakeCallasProcessClient(tmp / "jobs", self.profiles, separations_factory=seps)
        self.profiles = self.client.profiles  # тесты подменяют обработчики на лету
        analyzer = PrepressAnalyzer(self.client)
        self.service = CallasFixService(
            self.client, compare=fake_compare(changed), post_checks=build_post_checks(analyzer)
        )

    def run(self, key):
        self.service.start(self.source, key)
        return self.service.execute(self.source, key)


@pytest.fixture()
def env(tmp_path):
    return Env(tmp_path)


def test_plans_are_registered_with_hints_and_limits():
    assert CALLAS_PLANS["black_to_rich"].requires_cmyk is True
    assert "мелкий текст и штрихкоды" in CALLAS_PLANS["black_to_rich"].warning
    assert "RemoveClipPathes" in CALLAS_PLANS["remove_clipping"].description
    assert "PitStop" in CALLAS_PLANS["remove_clipping"].description and "2079" in CALLAS_PLANS["remove_clipping"].description
    assert "скрытое содержимое" in CALLAS_PLANS["remove_clipping"].warning
    assert "не копировать" in CALLAS_PLANS["fonts_to_outlines"].description or "скопировать" in CALLAS_PLANS["fonts_to_outlines"].description


# ----------------------------------------------------------------------------- fonts
def test_fonts_fix_then_recheck_shows_no_live_fonts(env):
    state = env.run("fonts_to_outlines")
    assert state["status"] == "ready"
    check = state["summary"]["post_check"]
    assert check["kind"] == "fonts" and check["level"] == "ok" and check["remaining"] == 0
    assert (env.client.calls[0]) == (FONTS, "fix") and (WARN_FONTS_PROFILE, "analyze") in env.client.calls
    assert state["summary"]["plan_warning"].startswith("Невстроенные шрифты")
    assert env.source.is_file()  # исходник не тронут


def test_fonts_fix_with_non_embedded_font_is_an_error_for_operator(env):
    env.profiles[FONTS] = lambda pdf, mode: {"summary": {"fixups_failed": 2}, "exit_code": 8, "status": "errors", "xml": "<r/>"}
    state = env.run("fonts_to_outlines")
    assert state["status"] == "failed"
    assert state["error"] == "Шрифты не встроены, перевод в кривые невозможен, нужна проверка макета."
    assert env.service.file_path(env.source, "pdf") is None


def test_fonts_partial_fix_is_ready_with_warning(env):
    env.profiles[FONTS] = lambda pdf, mode: {"summary": {"fixups_applied": 1, "fixups_failed": 1}, "exit_code": 8, "xml": "<r/>", "fixed": env.fixed}
    env.fonts_after = [("Helvetica", "Type1", False)]
    state = env.run("fonts_to_outlines")
    assert state["status"] == "ready"
    assert state["summary"]["partial_failure"].startswith("Шрифты не встроены")
    check = state["summary"]["post_check"]
    assert check["level"] == "warning" and check["remaining"] == 1
    assert any("Шрифты не встроены, перевод невозможен" in message for message in check["messages"])


def test_fonts_nothing_to_convert_is_reported(env):
    env.profiles[FONTS] = lambda pdf, mode: {"exit_code": 0, "xml": "<r/>"}
    state = env.run("fonts_to_outlines")
    assert state["status"] == "failed" and state["error"] == "Живых шрифтов для перевода в кривые не найдено."


# ----------------------------------------------------------------------------- black
def test_black_requires_rgb_converted_first(tmp_path):
    env = Env(tmp_path)
    rgb = make_pdf(tmp_path / "PDF" / "rgb.pdf", (1, 0, 0))
    with pytest.raises(CallasFixError) as error:
        env.service.start(rgb, "black_to_rich")
    assert error.value.code == "rgb_not_converted" and "Сначала переведите RGB в CMYK" in str(error.value)
    assert not env.client.calls  # callas не запускался


def test_black_fix_compares_and_rechecks_share(tmp_path):
    env = Env(tmp_path, changed=3.2, k100_before=0.5, k100_after=0.0)
    state = env.run("black_to_rich")
    assert state["status"] == "ready"
    check = state["summary"]["post_check"]
    assert check["level"] == "ok"
    assert "было 50%, стало 0%" in check["messages"][0]
    assert any("Изменено 3.2% площади" in message for message in check["messages"])
    assert not any("текст" in message for message in check["messages"])  # текста K100 не было
    assert "мелкий текст и штрихкоды" in state["summary"]["plan_warning"]


def test_black_fix_keeping_text_k100_is_ok(tmp_path):
    """Плашка ушла в составной, текст остался K100: замечаний о тексте нет."""
    env = Env(tmp_path, k100_before=0.3, k100_after=0.0, text_before=0.02, text_after=0.02)
    check = env.run("black_to_rich")["summary"]["post_check"]
    assert check["level"] == "ok"
    assert not any("текст" in message for message in check["messages"])


def test_black_fix_warns_when_text_lost_k100(tmp_path):
    """Профиль перевёл в составной и текст K100: оператор получает предупреждение."""
    env = Env(tmp_path, k100_before=0.3, k100_after=0.0, text_before=0.02, text_after=0.0)
    check = env.run("black_to_rich")["summary"]["post_check"]
    assert check["level"] == "warning"
    assert any("текст, линии) тоже переведён в составной" in message for message in check["messages"])


def test_black_fix_warns_if_solid_remains(tmp_path):
    env = Env(tmp_path, k100_before=0.5, k100_after=0.4)
    check = env.run("black_to_rich")["summary"]["post_check"]
    assert check["level"] == "warning" and any("осталась" in message for message in check["messages"])


def test_black_fix_without_k100_reports_no_change(env):
    env.profiles[BLACK] = lambda pdf, mode: {"exit_code": 0, "xml": "<r/>"}
    state = env.run("black_to_rich")
    assert state["status"] == "failed" and "K100" in state["error"]


# ----------------------------------------------------------------------------- clipping
def test_clipping_service_clip_looks_unchanged(tmp_path):
    env = Env(tmp_path, changed=0.0)
    state = env.run("remove_clipping")
    assert state["status"] == "ready"
    check = state["summary"]["post_check"]
    assert check["level"] == "ok" and "служебная обтравка" in check["messages"][0]
    assert "скрытое содержимое" in state["summary"]["plan_warning"]


def test_clipping_reveals_hidden_content_asks_operator(tmp_path):
    env = Env(tmp_path, changed=34.0)
    check = env.run("remove_clipping")["summary"]["post_check"]
    assert check["level"] == "warning" and "скрытое обтравкой" in check["messages"][0]
    assert check["changed_percent"] == 34.0


def test_clipping_no_clips_found(env):
    env.profiles[CLIP] = lambda pdf, mode: {"exit_code": 0, "xml": "<r/>"}
    assert env.run("remove_clipping")["error"] == "Обтравок в PDF не найдено: профиль ничего не изменил."


# ----------------------------------------------------------------------------- failures
def test_failed_callas_fix_is_stored(env):
    env.profiles[CLIP] = RuntimeError("callas упал")
    state = env.run("remove_clipping")
    assert state["status"] == "failed" and "callas упал" in state["error"]


def test_post_check_failure_keeps_result(env):
    env.profiles[WARN_FONTS_PROFILE] = RuntimeError("анализ недоступен")
    state = env.run("fonts_to_outlines")
    assert state["status"] == "ready"
    assert state["summary"]["post_check"]["level"] == "warning"


def test_missing_pdf_is_rejected(env):
    with pytest.raises(CallasFixError) as error:
        env.service.start(env.tmp / "PDF" / "absent.pdf", "fonts_to_outlines")
    assert error.value.code == "pdf_not_found"


def test_exit_code_decides_when_report_has_no_fixup_counters(env):
    """Отчёт без fixup-записей (ошибка утверждения pdfToolbox): код 5 значит «исправлено», код 8 «ошибки»."""
    env.profiles[CLIP] = lambda pdf, mode: {"exit_code": 5, "xml": "<r/>", "fixed": env.fixed}
    assert env.run("remove_clipping")["status"] == "ready"
    env.profiles[FONTS] = lambda pdf, mode: {"exit_code": 8, "status": "errors", "xml": "<r/>"}
    state = env.run("fonts_to_outlines")
    assert state["status"] == "failed" and state["error"].startswith("Шрифты не встроены")


def test_black_plan_profile_matches_the_deployed_fixup_file():
    """Имя профиля плана = «Image Magic/» + файл .kfpx из callas_profiles/ (копия шары soft/callas_checks_fixups)."""
    profile = CALLAS_PLANS["black_to_rich"].profile
    assert profile == "Image Magic/Pure CMYK black to rich black (C60 M40 Y40 K100)"
    folder, name = profile.split("/", 1)
    assert folder == "Image Magic"
    assert (Path(__file__).resolve().parent.parent / "callas_profiles" / f"{name}.kfpx").is_file()


def test_black_post_check_warns_when_small_pure_k100_remains(tmp_path):
    env = Env(tmp_path, k100_before=0.05, k100_after=0.03)
    check = env.run("black_to_rich")["summary"]["post_check"]
    assert check["level"] == "warning" and "осталась" in " ".join(check["messages"])


# ----------------------------------------------------------------------------- downsample
def test_downsample_plan_is_registered_without_variables():
    plan = CALLAS_PLANS["downsample_images"]
    assert plan.profile == DEFAULT_DOWNSAMPLE_PROFILE == "PDF fixups/Downsample image resolution to specified value"
    assert plan.title == "Уменьшить разрешение до 300 ppi" and plan.requires_cmyk is False
    assert "уменьшит до 300 ppi все изображения выше 300 ppi; штриховые 1-бит проверьте по превью" in plan.description
    assert "Однобитные" in plan.warning


def test_downsample_profile_is_configurable():
    plans = with_profile_overrides(CALLAS_PLANS, {"downsample_images": " Custom/Downsample "})
    assert plans["downsample_images"].profile == "Custom/Downsample"
    assert plans["fonts_to_outlines"] is CALLAS_PLANS["fonts_to_outlines"]
    assert CALLAS_PLANS["downsample_images"].profile == DEFAULT_DOWNSAMPLE_PROFILE
    assert with_profile_overrides(CALLAS_PLANS, {"downsample_images": " "})["downsample_images"].profile == DEFAULT_DOWNSAMPLE_PROFILE


def test_downsample_uses_exit_code_when_report_has_xmp_assertion(env):
    """Профиль отдаёт код 5 и fixups_applied=0 (ошибка XMP в отчёте): результат всё равно готов."""
    state = env.run("downsample_images")
    assert state["status"] == "ready"
    assert env.client.calls[0] == (DOWN, "fix")
    check = state["summary"]["post_check"]
    assert check["kind"] == "downsample_images" and check["level"] == "ok" and check["remaining"] == 0
    assert "было 1, стало 0" in check["messages"][0]
    assert "1-бит" in check["messages"][-1]
    assert env.source.is_file()


def test_downsample_warns_when_high_ppi_remains(tmp_path):
    env = Env(tmp_path, ppi_after=[(2, 650.0, 8)])
    check = env.run("downsample_images")["summary"]["post_check"]
    assert check["level"] == "warning" and check["remaining"] == 1
    assert any("остались: стр. 2" in message for message in check["messages"])


def test_downsample_nothing_to_do_is_reported(env):
    env.profiles[DOWN] = lambda pdf, mode: {"exit_code": 0, "xml": "<r/>"}
    state = env.run("downsample_images")
    assert state["status"] == "failed" and state["error"] == "Изображений выше 300 ppi в PDF не найдено: профиль ничего не изменил."


def test_downsample_recheck_failure_keeps_result(env):
    env.profiles[PPI_PROFILE] = RuntimeError("анализ недоступен")
    state = env.run("downsample_images")
    assert state["status"] == "ready" and state["summary"]["post_check"]["level"] == "warning"


def test_downsample_custom_profile_is_sent_to_callas(tmp_path):
    env = Env(tmp_path)
    plans = with_profile_overrides(CALLAS_PLANS, {"downsample_images": "Custom/Downsample"})
    env.profiles["Custom/Downsample"] = env.profiles[DOWN]
    service = CallasFixService(env.client, plans, compare=fake_compare(0.0))
    service.start(env.source, "downsample_images")
    assert service.execute(env.source, "downsample_images")["status"] == "ready"
    assert env.client.calls[0] == ("Custom/Downsample", "fix")


# ----------------------------------------------------------------------------- TAC
def tac_env(tmp_path, before, after, *, changed=4.5):
    """``before`` / ``after`` — спецификации страниц ``make_tac_separations`` для исходника и результата."""
    env = Env(tmp_path, changed=changed)

    def seps(pdf, out):
        return make_tac_separations(out, before if Path(pdf).name == "order.pdf" else after)

    env.client.separations_factory = seps
    return env


def test_tac_plan_is_registered_with_deployed_profile_and_cmyk_requirement():
    plan = CALLAS_PLANS["reduce_tac"]
    assert plan.title == "Снижение суммарного покрытия до 300%"
    assert plan.profile == "Image Magic/TAC 300 - CMYK and ICC CMYK - preserve spots"
    assert plan.requires_cmyk is True and plan.mode == "fix"
    assert "300%" in plan.description and "Плашечные цвета" in plan.description and "1153" in plan.description
    assert "ВСЕ CMYK-цвета" in plan.warning and "превью" in plan.warning
    assert plan.no_change_message
    folder, name = plan.profile.split("/", 1)
    assert folder == "Image Magic"
    assert (Path(__file__).resolve().parent.parent / "callas_profiles" / f"{name}.kfpx").is_file()


def test_tac_requires_rgb_converted_first_with_its_own_reason(tmp_path):
    env = Env(tmp_path)
    rgb = make_pdf(tmp_path / "PDF" / "rgb.pdf", (1, 0, 0))
    with pytest.raises(CallasFixError) as error:
        env.service.start(rgb, "reduce_tac")
    assert error.value.code == "rgb_not_converted"
    assert "покрытие RGB-объектов он не ограничит" in str(error.value) and "K100" not in str(error.value)
    assert not env.client.calls


def test_black_rgb_refusal_keeps_its_reason(tmp_path):
    env = Env(tmp_path)
    rgb = make_pdf(tmp_path / "PDF" / "rgb.pdf", (1, 0, 0))
    with pytest.raises(CallasFixError) as error:
        env.service.start(rgb, "black_to_rich")
    assert str(error.value).endswith("затем запускайте этот план: иначе RGB-чёрный не станет K100.")


def test_tac_post_check_reports_before_and_after(tmp_path):
    env = tac_env(tmp_path, [{"cmyk": (100, 100, 100, 100), "share": 0.25}], [{"cmyk": (100, 80, 60, 60), "share": 0.25}])
    state = env.run("reduce_tac")
    assert state["status"] == "ready"
    assert env.client.calls[0] == (TAC, "fix")
    check = state["summary"]["post_check"]
    assert check["kind"] == "reduce_tac" and check["level"] == "ok"
    assert check["messages"][0] == "Макс. суммарное покрытие: было 400%, стало 300%."
    assert check["max_before"] == 400.0 and check["max_after"] == 300.0 and check["remaining_pages"] == []
    text = " ".join(check["messages"])
    assert "50 ppi" in text and "PitStop" in text and "Изменено 4.5% площади" in text
    assert "ВСЕ CMYK-цвета" in state["summary"]["plan_warning"]
    assert env.source.is_file()


def test_tac_post_check_warns_when_coverage_above_limit_remains(tmp_path):
    env = tac_env(
        tmp_path,
        [{"cmyk": (100, 100, 100, 100)}, {"cmyk": (100, 100, 100, 100), "share": 0.5}],
        [{"cmyk": (60, 60, 60, 60)}, {"cmyk": (80, 80, 80, 80), "share": 0.1}],
    )
    check = env.run("reduce_tac")["summary"]["post_check"]
    assert check["level"] == "warning" and check["remaining_pages"] == [2]
    assert check["messages"][0] == "Макс. суммарное покрытие: было 400%, стало 320%."
    assert any("Покрытие выше 310% осталось: стр. 2 (10.0% площади)" in message for message in check["messages"])


def test_tac_post_check_without_separations_is_a_warning_not_a_failure(env):
    env.client.separations_factory = None  # служба callas без цветоделений
    state = env.run("reduce_tac")
    assert state["status"] == "ready"
    check = state["summary"]["post_check"]
    assert check["level"] == "warning"
    assert check["messages"][0].startswith("Повторная проверка суммарного покрытия не выполнена: separations не настроены")
    assert any("Изменено" in message for message in check["messages"])


def test_tac_post_check_when_only_the_source_measurement_fails(tmp_path):
    env = Env(tmp_path)

    def seps(pdf, out):
        if Path(pdf).name == "order.pdf":
            raise RuntimeError("исходник не разобран")
        return make_tac_separations(out, [{"cmyk": (60, 60, 60, 60)}])

    env.client.separations_factory = seps
    check = env.run("reduce_tac")["summary"]["post_check"]
    assert check["level"] == "ok" and check["messages"][0] == "Макс. суммарное покрытие после исправления: 240%."
    assert "max_before" not in check


def test_tac_nothing_to_recalculate_is_reported(env):
    env.profiles[TAC] = lambda pdf, mode: {"exit_code": 0, "xml": "<r/>"}
    state = env.run("reduce_tac")
    assert state["status"] == "failed" and state["error"] == CALLAS_PLANS["reduce_tac"].no_change_message


def test_tac_uses_exit_code_when_report_has_no_fixup_counters(env):
    env.profiles[TAC] = lambda pdf, mode: {"exit_code": 5, "xml": "<r/>", "fixed": env.fixed}
    assert env.run("reduce_tac")["status"] == "ready"


# ------------------------------------------------------- RGB перед планом (диагностический PDF)
class _FakeRgbConverter:
    """``converter(source, target, label)`` как ``CallasRgbToCmykConverter``: пишет CMYK-PDF."""

    profile = "Image Magic/NEW_Convert color to 4c for digital printing"

    def __init__(self, *, leave_rgb: bool = False) -> None:
        self.leave_rgb = leave_rgb
        self.sources: list[Path] = []

    def __call__(self, source, target, label):
        self.sources.append(Path(source))
        make_pdf(Path(target), (1, 0, 0) if self.leave_rgb else (0, 0, 0, 1))
        return {"fixups_applied": 1}


def _env_with_converter(tmp_path, converter):
    env = Env(tmp_path)
    env.service = CallasFixService(
        env.client, compare=fake_compare(0.0), post_checks={}, rgb_converter=converter
    )
    return env


@pytest.mark.parametrize("plan_key", ["reduce_tac", "black_to_rich"])
def test_cmyk_plan_converts_rgb_first_when_a_converter_is_wired(tmp_path, plan_key):
    """Диагностический PDF собран «как есть», RGB в нём остаётся: план с ``requires_cmyk``
    сам переводит RGB тем же профилем, что и автоконвертация, и только потом запускается."""
    from core.pdf_inspector import find_rgb_pages

    converter = _FakeRgbConverter()
    env = _env_with_converter(tmp_path, converter)
    rgb = make_pdf(tmp_path / "PDF" / "rgb.pdf", (1, 0, 0))
    seen: list[tuple[Path, tuple[int, ...]]] = []
    profile = CALLAS_PLANS[plan_key].profile
    original = env.profiles[profile]

    def handler(pdf, mode):
        seen.append((Path(pdf), find_rgb_pages(pdf)))
        return original(pdf, mode)

    env.profiles[profile] = handler
    env.service.start(rgb, plan_key)  # без отказа 409
    state = env.service.execute(rgb, plan_key)

    assert state["status"] == "ready", state.get("error")
    assert converter.sources, "RGB→CMYK не вызывался"
    [(plan_input, rgb_pages)] = seen
    assert rgb_pages == ()  # план получил уже CMYK
    # callas получает локальный снимок, а не свежий файл на шаре (mtime на CIFS «плывёт»)
    assert tmp_path not in plan_input.parents
    converted = state["summary"]["rgb_converted"]
    assert converted["pages"] == [1] and converted["profile"] == converter.profile
    assert state["summary"]["post_check"]["messages"][0].startswith("Перед исправлением RGB переведён в CMYK (стр. 1)")
    assert find_rgb_pages(rgb) == (1,)  # исходник заказа не тронут


def test_cmyk_plan_fails_clearly_when_rgb_conversion_leaves_rgb(tmp_path):
    env = _env_with_converter(tmp_path, _FakeRgbConverter(leave_rgb=True))
    rgb = make_pdf(tmp_path / "PDF" / "rgb.pdf", (1, 0, 0))
    env.service.start(rgb, "reduce_tac")
    state = env.service.execute(rgb, "reduce_tac")
    assert state["status"] == "failed"
    assert "перевести его в CMYK перед планом не удалось" in state["error"]
    assert "покрытие RGB-объектов он не ограничит" in state["error"]
    assert (CALLAS_PLANS["reduce_tac"].profile, "fix") not in env.client.calls


def test_cmyk_plan_on_cmyk_pdf_skips_conversion(tmp_path):
    converter = _FakeRgbConverter()
    env = _env_with_converter(tmp_path, converter)
    state = env.run("reduce_tac")
    assert state["status"] == "ready"
    assert not converter.sources and "rgb_converted" not in state["summary"]
