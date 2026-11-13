"""Планы Callas: шрифты в кривые, составной чёрный, удаление обтравки (фейковый callas, без сети)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from config.callas_plans import CALLAS_PLANS
from services.callas.fix_checks import build_post_checks
from services.callas.prepress import PrepressAnalyzer, WARN_FONTS_PROFILE
from services.callas_fix import CallasFixError, CallasFixService
from tests.fake_callas import FakeCallasProcessClient, fonts_report_xml, make_separations
from tests.test_rgb_to_cmyk import make_pdf

FONTS = CALLAS_PLANS["fonts_to_outlines"].profile
BLACK = CALLAS_PLANS["black_to_rich"].profile
CLIP = CALLAS_PLANS["remove_clipping"].profile


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
    def __init__(self, tmp: Path, *, changed=0.0, k100_before=0.5, k100_after=0.0, fonts_after=()):
        self.tmp = tmp
        pdf_dir = tmp / "PDF"
        pdf_dir.mkdir()
        self.source = make_pdf(pdf_dir / "order.pdf", (0, 0, 0, 1))
        self.fixed = make_pdf(tmp / "fixed_source.pdf", (0, 0, 0, 1))
        self.shares = {"source": k100_before, "fixed": k100_after}
        self.fonts_after = list(fonts_after)
        self.fonts_calls = 0

        def fonts_handler(pdf, mode):
            # Первый вызов Warn Fonts — после фикса (на результате).
            self.fonts_calls += 1
            return {"xml": fonts_report_xml(self.fonts_after)}

        def seps(pdf, out):
            share = self.shares["fixed"] if Path(pdf).name != "order.pdf" else self.shares["source"]
            return make_separations(out, [{"k100": share}])

        self.profiles = {
            WARN_FONTS_PROFILE: fonts_handler,
            FONTS: lambda pdf, mode: {"summary": {"fixups_applied": 2}, "exit_code": 5, "xml": "<r/>", "fixed": self.fixed},
            BLACK: lambda pdf, mode: {"summary": {"fixups_applied": 1}, "exit_code": 5, "xml": "<r/>", "fixed": self.fixed},
            CLIP: lambda pdf, mode: {"summary": {"fixups_applied": 1}, "exit_code": 5, "xml": "<r/>", "fixed": self.fixed},
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
    assert any("штрихкоды" in message for message in check["messages"])
    assert "мелкий текст и штрихкоды" in state["summary"]["plan_warning"]


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
