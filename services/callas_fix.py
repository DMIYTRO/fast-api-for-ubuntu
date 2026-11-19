"""Исправление PDF заказа процесс-планом Callas и сборка превью «до / после».

Порядок: ``start`` ставит состояние ``processing`` → ``execute`` запускает план в режиме
``fix`` на сервере callas, сохраняет результат рядом с исходным PDF и строит три
картинки (оригинал, результат, наложение). Оператор принимает или отклоняет результат.

Исходный PDF не изменяется никогда. Всё, что создаёт сервис, лежит в
``<папка PDF>/CallasFix/<имя PDF>/``:

* ``state.json`` — состояние (переживает перезапуск сервиса);
* ``fixed.pdf`` — результат исправления (кандидат на следующую ревизию);
* ``compare/original|result|overlay`` — картинки сравнения;
* ``accepted.pdf`` — только для исходного PDF заказа: копия принятого ``fixed.pdf``,
  из которой собирается заказ, ждущий решения по коррекции (см.
  ``services/callas_source_fix.py``). Повторный запуск исправления её не трогает;
* ``source.pdf`` — только для растрового исходника (TIFF/JPEG/PNG): одностраничный PDF,
  собранный из растра так же, как при обычной сборке заказа (ImageMagick, то же DPI,
  без пересэмплирования и перевода цвета). План callas и сравнение работают с ним.

Для каждого исправления в ``summary`` пишется вердикт ``bleed_only`` (поля вылета
дорисованы, макет внутри формата не изменён) и его причины ``bleed_only_reasons``
(``services/callas_bleed.py``).
"""

from __future__ import annotations

import filecmp
import hashlib
import json
import logging
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote
from uuid import uuid4

from config.callas_plans import CALLAS_PLANS, CallasPlan
from core.fix_compare import FixCompareError, build_fix_comparison
from services.callas.errors import CallasError
from services.callas_bleed import BLEED_PLAN_KEY, bleed_only_verdict, profile_bleed_mm
from services.pdf_routing import STATUS_FOLDERS

logger = logging.getLogger(__name__)

STATE_NAME = "state.json"
FIXED_PDF_NAME = "fixed.pdf"
ACCEPTED_SOURCE_PDF_NAME = "accepted.pdf"
SOURCE_PDF_NAME = "source.pdf"
#: Растровые исходники, которые перед планом callas собираются в ``source.pdf``.
RASTER_SOURCE_SUFFIXES = frozenset({".tif", ".tiff", ".jpg", ".jpeg", ".png"})
COMPARE_DIR = "compare"
IMAGE_KINDS = ("original", "result", "overlay")
#: Сколько страниц показывать в сравнении «до / после» (лицо и оборот, как ``MAX_SUPPORTED_PAGES``).
#: Коррекция размера сравнивает все страницы: вердикту ``bleed_only`` нужна каждая.
PREVIEW_PAGES = 2

STATUS_PROCESSING = "processing"
STATUS_READY = "ready"
STATUS_FAILED = "failed"
STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"


class CallasFixError(Exception):
    """Ошибка, текст которой можно показать оператору."""

    def __init__(self, message: str, *, status_code: int = 422, code: str = "callas_fix_error") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def fix_dir_for(pdf_path: Path | str) -> Path:
    """Папка результата исправления.

    Для собранного PDF (лежит в ``PDF/`` или в папке ``PDF/Checked|Rework|Failed|Print/``) —
    ``PDF/CallasFix/<имя>``. Для исходного
    PDF заказа, который лежит вне ``PDF/``, результат кладётся в ``PDF/CallasFix/<имя>.source``
    входной папки (и для файла из папки полной проверки ``CorelDRAW``), чтобы не писать в
    папку с исходниками и не пересекаться с собранным PDF.
    """
    pdf = Path(pdf_path)
    if pdf.parent.name == "PDF":
        return pdf.parent / "CallasFix" / pdf.stem
    if pdf.parent.name in STATUS_FOLDERS and pdf.parent.parent.name == "PDF":
        # Собранный PDF, разложенный по папке статуса (Print/Rework/Failed).
        return pdf.parent.parent / "CallasFix" / pdf.stem
    from services.check_policy import source_output_root

    return source_output_root(pdf) / "PDF" / "CallasFix" / f"{pdf.stem}.source"


def is_raster_source(path: Path | str) -> bool:
    return Path(path).suffix.lower() in RASTER_SOURCE_SUFFIXES


def baseline_pdf_for(source: Path | str) -> Path:
    """PDF стороны ДО исправления: сам исходный PDF или ``source.pdf`` растра."""
    path = Path(source)
    return fix_dir_for(path) / SOURCE_PDF_NAME if is_raster_source(path) else path


def _default_raster_to_pdf(raster: Path, target: Path, *, strip_icc_cmyk: bool = False) -> Any:
    from processing.batch_processor import build_raster_source_pdf

    return build_raster_source_pdf(raster, target, strip_icc_cmyk=strip_icc_cmyk)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _signature(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def source_signature_matches(path: Path | str, signature: Mapping[str, Any] | None) -> bool:
    """Исходный файл тот же, что при исправлении (размер и время изменения совпадают)."""
    source = Path(path)
    if not signature or not source.is_file():
        return False
    current = _signature(source)
    return (signature.get("size"), signature.get("mtime_ns")) == (
        current["size"], current["mtime_ns"],
    )


def read_state_file(pdf_path: Path | str) -> dict[str, Any] | None:
    """Читает сохранённое состояние; ``None``, если исправления не было или файл повреждён."""
    path = fix_dir_for(pdf_path) / STATE_NAME
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def _write_state(directory: Path, state: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f"{STATE_NAME}.{uuid4().hex[:8]}.tmp"
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, directory / STATE_NAME)


LEGACY_BLEED_REASON = "исправление выполнено до проверки вылетов: запустите его заново"


def _summary_with_bleed_verdict(summary: Any) -> dict[str, Any]:
    """Сводка для интерфейса: ``bleed_only``/``bleed_only_reasons`` есть всегда."""
    result = dict(summary) if isinstance(summary, Mapping) else {}
    if "bleed_only" not in result:
        result["bleed_only"] = False
        result["bleed_only_reasons"] = [LEGACY_BLEED_REASON] if result else []
    result.setdefault("bleed_only_reasons", [])
    return result


def comparison_payload(
    state: dict[str, Any] | None,
    base_url: str,
    *,
    source_only: bool = False,
    can_accept: bool = False,
    applied: bool = False,
    status: str | None = None,
) -> dict[str, Any] | None:
    """Состояние → контракт интерфейса ``comparison`` (URL картинок строятся от ``base_url``).

    Для исходного PDF (``source_only``) ``can_accept`` — исправление можно принять
    для текущей проверки, ``applied`` — заказ собирается из принятого исправления;
    ``status`` позволяет показать состояние с точки зрения этого заказа.
    """
    if not state:
        return None
    images = state.get("images") or {}
    # Метка завершения в URL: новое исправление того же заказа не показывает старые картинки из кэша.
    version = f"?v={quote(str(state.get('finished_at') or ''), safe='')}" if state.get("finished_at") else ""
    return {
        "status": status or state.get("status") or STATUS_FAILED,
        "source_only": source_only,
        "can_accept": can_accept,
        "applied": applied,
        "plan": state.get("plan") or None,
        "error": state.get("error"),
        "decision": state.get("decision"),
        "started_at": state.get("started_at"),
        "finished_at": state.get("finished_at"),
        "summary": _summary_with_bleed_verdict(state.get("summary")),
        "original_url": f"{base_url}/files/original{version}" if "original" in images else None,
        "result_url": f"{base_url}/files/result{version}" if "result" in images else None,
        "overlay_url": f"{base_url}/files/overlay{version}" if "overlay" in images else None,
        "pdf_url": f"{base_url}/files/pdf{version}" if state.get("fixed_pdf") else None,
        "pages": _page_tiles(state, base_url, version),
    }


def _page_tiles(state: Mapping[str, Any], base_url: str, version: str) -> list[dict[str, Any]]:
    """Картинки «до / после» по страницам: 1 — основные, 2… — ``summary["pages"][i]["images"]``.

    Старые результаты (до сравнения оборота) дают только первую страницу.
    """
    images = state.get("images") or {}
    summary = state.get("summary") if isinstance(state.get("summary"), Mapping) else {}

    def tile(page: int, kinds: Mapping[str, Any], described: Mapping[str, Any]) -> dict[str, Any]:
        suffix = "" if page == 1 else f"{'&' if version else '?'}page={page}"
        return {
            "page": page,
            **{
                f"{kind}_url": f"{base_url}/files/{kind}{version}{suffix}" if kinds.get(kind) else None
                for kind in IMAGE_KINDS
            },
            "changed_percent": described.get("changed_percent"),
            "overlay_changed_percent": described.get("overlay_changed_percent"),
        }

    if not any(images.get(kind) for kind in IMAGE_KINDS):
        return []
    pages = [tile(1, images, summary)]
    for extra in summary.get("pages") or ():
        if not isinstance(extra, Mapping) or not isinstance(extra.get("page"), int) or extra["page"] < 2:
            continue
        kinds = extra.get("images")
        if isinstance(kinds, Mapping) and any(kinds.get(kind) for kind in IMAGE_KINDS):
            pages.append(tile(extra["page"], kinds, extra))
    return pages


class CallasFixService:
    def __init__(
        self,
        client: Any,
        plans: dict[str, CallasPlan] | None = None,
        *,
        compare: Callable[..., Any] = build_fix_comparison,
        post_checks: Mapping[str, Callable[[Path, Path, dict[str, Any]], dict[str, Any]]] | None = None,
        raster_to_pdf: Callable[..., Any] | None = None,
    ) -> None:
        self._client = client
        # Растр → одностраничный PDF (как при обычной сборке заказа); шов для тестов.
        self._raster_to_pdf = raster_to_pdf or _default_raster_to_pdf
        self._post_checks = dict(post_checks or {})
        self._plans = plans if plans is not None else CALLAS_PLANS
        self._compare = compare
        self._lock = threading.Lock()
        self._active: set[str] = set()

    # ------------------------------------------------------------------ plans
    def plans(self, direction: str | None = None) -> list[CallasPlan]:
        return [
            plan
            for plan in self._plans.values()
            if not direction or direction in plan.directions
        ]

    def plan(self, key: str) -> CallasPlan | None:
        return self._plans.get(key)

    # ------------------------------------------------------------------ state
    def read_state(self, pdf_path: Path | str) -> dict[str, Any] | None:
        """Состояние с поправкой: ``processing`` без живой задачи — это прерванная перезапуском."""
        state = read_state_file(pdf_path)
        if state and state.get("status") == STATUS_PROCESSING:
            with self._lock:
                alive = str(fix_dir_for(pdf_path)) in self._active
            if not alive:
                state = {
                    **state,
                    "status": STATUS_FAILED,
                    "error": "Исправление прервано перезапуском сервиса. Запустите его заново.",
                    "finished_at": _now(),
                }
                _write_state(fix_dir_for(pdf_path), state)
        return state

    def start(self, pdf_path: Path | str, plan_key: str, *, direction: str | None = None) -> dict[str, Any]:
        """Проверяет запрос и записывает состояние ``processing``; саму работу делает ``execute``."""
        pdf = Path(pdf_path)
        plan = self._plans.get(plan_key)
        if plan is None:
            raise CallasFixError("Неизвестный план Callas.", code="unknown_callas_plan")
        if plan.mode != "fix":
            raise CallasFixError("Этот план не предназначен для исправления.", code="callas_plan_mode")
        if direction and direction not in plan.directions:
            raise CallasFixError("План недоступен для выбранного направления.", code="callas_plan_direction")
        if not pdf.is_file():
            raise CallasFixError("PDF заказа не найден.", status_code=404, code="pdf_not_found")
        if plan.requires_cmyk and not is_raster_source(pdf):
            # Растр сначала собирается в PDF (в ``execute``); там же проверяется RGB.
            self._require_no_rgb(pdf)
        key = str(fix_dir_for(pdf))
        with self._lock:
            if key in self._active:
                raise CallasFixError(
                    "Исправление этого заказа уже выполняется.", status_code=409, code="callas_fix_busy"
                )
            self._active.add(key)
        try:
            state = {
                "status": STATUS_PROCESSING,
                "plan": {
                    "key": plan.key, "title": plan.title,
                    "description": plan.description, "warning": plan.warning,
                },
                "started_at": _now(),
                "finished_at": None,
                "error": None,
                "decision": None,
                "source": {"name": pdf.name, **_signature(pdf)},
                "images": {},
                "summary": {},
            }
            _write_state(fix_dir_for(pdf), state)
        except BaseException:
            with self._lock:
                self._active.discard(key)
            raise
        return state

    @staticmethod
    def _require_no_rgb(pdf: Path) -> None:
        """Перед «чёрным» планом RGB уже должен быть переведён в CMYK (иначе RGB-чёрный не станет K100)."""
        from core.pdf_inspector import find_rgb_pages

        try:
            pages = find_rgb_pages(pdf)
        except Exception as exc:  # noqa: BLE001
            raise CallasFixError(
                f"Не удалось проверить цвет PDF перед исправлением: {exc}", code="rgb_check_failed"
            ) from exc
        if pages:
            raise CallasFixError(
                "В PDF остался RGB (стр. " + ", ".join(str(page) for page in pages) + "). "
                "Сначала переведите RGB в CMYK (включите автоконвертацию или исправьте макет), "
                "затем запускайте этот план: иначе RGB-чёрный не станет K100.",
                status_code=409,
                code="rgb_not_converted",
            )

    def execute(
        self,
        pdf_path: Path | str,
        plan_key: str,
        *,
        format_mm: tuple[float, float] | None = None,
        bleed_mm: float | None = None,
        strip_icc_cmyk: bool = False,
    ) -> dict[str, Any]:
        """Выполняет исправление. Исключения не выпускает: ошибка попадает в состояние ``failed``.

        ``format_mm`` (формат из имени файла, без вылетов) и ``bleed_mm`` (вылет профиля)
        нужны вердикту ``bleed_only``; без формата размер результата не сверяется.
        ``strip_icc_cmyk`` — как ``IMAGE_MAGIC_STRIP_ICC_CMYK`` обычной сборки, для растра.
        """
        pdf = Path(pdf_path)
        directory = fix_dir_for(pdf)
        key = str(directory)
        plan = self._plans[plan_key]
        state = read_state_file(pdf) or {}
        job_id = f"fix-{uuid4().hex[:16]}"
        try:
            source_pdf = pdf
            if is_raster_source(pdf):
                source_pdf = self._build_source_pdf(pdf, directory, strip_icc_cmyk=strip_icc_cmyk)
                state["source_pdf"] = SOURCE_PDF_NAME
                if plan.requires_cmyk:
                    self._require_no_rgb(source_pdf)
            result = self._client.process(
                source_pdf, plan.profile, "fix", job_id=job_id, report=("xml",), keep_name=True
            )
            callas_summary = dict(result.summary)
            applied = int(callas_summary.get("fixups_applied") or 0)
            failed = int(callas_summary.get("fixups_failed") or 0)
            # Счётчики берутся из XML-отчёта; если в нём вместо fixup-записей стоит ошибка
            # (например утверждение pdfToolbox на PDF без XMP), надёжнее код выхода:
            # 5-7 — исправлено, 8 — ошибки после исправления.
            exit_code = int(getattr(result, "exit_code", 0) or 0)
            changed = applied > 0 or exit_code in (5, 6, 7)
            errors_left = failed > 0 or exit_code == 8
            if plan.unfixable_message and errors_left and applied == 0:
                raise CallasFixError(plan.unfixable_message, code="callas_unfixable")
            if plan.no_change_message and not changed and not errors_left:
                raise CallasFixError(plan.no_change_message, code="callas_no_change")
            fixed = directory / FIXED_PDF_NAME
            shutil.copyfile(result.output_pdf, fixed)
            compare_dir = directory / COMPARE_DIR
            if compare_dir.exists():
                shutil.rmtree(compare_dir, ignore_errors=True)
            comparison = self._compare(source_pdf, fixed, compare_dir)
            images = {
                kind: Path(getattr(comparison, f"{kind}_image")).name for kind in IMAGE_KINDS
            }
            summary = dict(comparison.summary)
            # Плоские поля, которые читает интерфейс (размер страницы до/после, мм).
            for side, field in (("original", "original_size_mm"), ("result", "result_size_mm")):
                described = summary.get(side) or {}
                summary[field] = [described.get("page_width_mm"), described.get("page_height_mm")]
            summary["callas"] = callas_summary
            summary["callas_status"] = result.status
            summary["plan_key"] = plan.key
            # Оборот виден оператору при любом плане (шрифты, обтравка, сжатие картинок…),
            # коррекция размера сравнивает все страницы ради вердикта ``bleed_only``.
            extra_pages = self._compare_extra_pages(
                source_pdf, fixed, compare_dir, summary,
                limit=None if plan.key == BLEED_PLAN_KEY else PREVIEW_PAGES,
            )
            if extra_pages:
                summary["pages"] = extra_pages
            bleed = profile_bleed_mm(None) if bleed_mm is None else float(bleed_mm)
            bleed_only, bleed_reasons = bleed_only_verdict(summary, format_mm=format_mm, bleed_mm=bleed)
            summary["bleed_only"] = bleed_only
            summary["bleed_only_reasons"] = bleed_reasons
            summary["bleed_check"] = {
                "format_mm": list(format_mm) if format_mm else None,
                "bleed_mm": bleed,
            }
            if plan.warning:
                summary["plan_warning"] = plan.warning
            if failed > 0:
                summary["partial_failure"] = plan.unfixable_message or (
                    "Часть исправлений callas не удалось выполнить."
                )
            check = self._post_checks.get(plan_key)
            if check is not None:
                try:
                    summary["post_check"] = check(pdf, fixed, summary)
                except Exception as exc:  # noqa: BLE001 — повторная проверка не должна терять результат
                    logger.warning("callas_fix.post_check_failed plan=%s", plan_key, exc_info=True)
                    summary["post_check"] = {
                        "kind": plan_key, "level": "warning",
                        "messages": [f"Повторная проверка результата не выполнена: {exc}"],
                    }
            state.update(
                status=STATUS_READY,
                finished_at=_now(),
                error=None,
                images=images,
                summary=summary,
                fixed_pdf=FIXED_PDF_NAME,
            )
            _write_state(directory, state)
        except Exception as exc:  # noqa: BLE001 — любое сбой должен дойти до оператора
            logger.exception("callas_fix.failed pdf=%s plan=%s", pdf.name, plan_key)
            message = str(exc).strip() or exc.__class__.__name__
            if not isinstance(exc, (CallasError, FixCompareError, CallasFixError)):
                message = f"Исправление не выполнено: {message}"
            state.update(status=STATUS_FAILED, finished_at=_now(), error=message)
            _write_state(directory, state)
        finally:
            with self._lock:
                self._active.discard(key)
            self._cleanup_job(job_id)
        return state

    def _build_source_pdf(self, raster: Path, directory: Path, *, strip_icc_cmyk: bool) -> Path:
        """``source.pdf`` из растрового исходника; сам растр не меняется."""
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / SOURCE_PDF_NAME
        temporary = directory / f".source.{uuid4().hex[:8]}.tmp.pdf"
        try:
            self._raster_to_pdf(raster, temporary, strip_icc_cmyk=strip_icc_cmyk)
            if not temporary.is_file() or temporary.stat().st_size == 0:
                raise CallasFixError(
                    f"Не удалось собрать PDF из растра {raster.name}: ImageMagick не вернул файл.",
                    code="raster_to_pdf_failed",
                )
            os.replace(temporary, target)
        except CallasFixError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise CallasFixError(
                f"Не удалось собрать PDF из растра {raster.name}: {str(exc).strip() or type(exc).__name__}",
                code="raster_to_pdf_failed",
            ) from exc
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def _compare_extra_pages(
        self,
        source_pdf: Path,
        fixed: Path,
        compare_dir: Path,
        summary: Mapping[str, Any],
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Сводки и картинки страниц 2…N (``limit`` — не дальше этой страницы)."""
        count = summary.get("original_page_count")
        if not isinstance(count, int) or count < 2 or summary.get("result_page_count") != count:
            return []
        last = count if limit is None else min(count, limit)
        pages = []
        for number in range(2, last + 1):
            try:
                comparison = self._compare(source_pdf, fixed, compare_dir / f"page-{number}", page=number)
            except Exception:  # noqa: BLE001 — несравнённая страница даёт bleed_only = False
                logger.warning("callas_fix.page_compare_failed pdf=%s page=%s", source_pdf.name, number, exc_info=True)
                continue
            described = dict(comparison.summary)
            described["page"] = number
            described["images"] = {
                kind: Path(getattr(comparison, f"{kind}_image")).name for kind in IMAGE_KINDS
            }
            pages.append(described)
        return pages

    def _cleanup_job(self, job_id: str) -> None:
        job_dir = getattr(self._client, "job_dir", None)
        if job_dir is None:
            return
        try:
            shutil.rmtree(job_dir(job_id), ignore_errors=True)
        except Exception:  # noqa: BLE001 — уборка не должна ломать результат
            logger.warning("callas_fix.cleanup_failed job_id=%s", job_id, exc_info=True)

    # --------------------------------------------------------------- decision
    def decide(self, pdf_path: Path | str, decision: str, *, redecide_accepted: bool = False) -> dict[str, Any]:
        """Решение оператора по готовому результату.

        ``redecide_accepted`` — только для исходного PDF, чьё принятое исправление
        текущий заказ не использует (принято в другой проверке): его можно отклонить.
        """
        if decision not in {"accept", "reject"}:
            raise CallasFixError("Решение должно быть «accept» или «reject».", code="invalid_decision")
        pdf = Path(pdf_path)
        state = self.read_state(pdf)
        if state and state.get("status") == STATUS_ACCEPTED and decision == "accept":
            return state
        decidable = {STATUS_READY, STATUS_ACCEPTED} if redecide_accepted else {STATUS_READY}
        if not state or state.get("status") not in decidable:
            raise CallasFixError(
                "Принять или отклонить можно только готовый результат исправления.",
                status_code=409,
                code="callas_fix_not_ready",
            )
        source = state.get("source") or {}
        current = _signature(pdf) if pdf.is_file() else None
        if current and (source.get("size"), source.get("mtime_ns")) != (
            current["size"],
            current["mtime_ns"],
        ):
            raise CallasFixError(
                "Исходный PDF изменился после исправления. Запустите исправление заново.",
                status_code=409,
                code="callas_fix_stale",
            )
        state.update(
            status=STATUS_ACCEPTED if decision == "accept" else STATUS_REJECTED,
            decision="accepted" if decision == "accept" else "rejected",
            decided_at=_now(),
        )
        _write_state(fix_dir_for(pdf), state)
        return state

    def restore_decision(self, pdf_path: Path | str, previous: dict[str, Any]) -> None:
        """Undo a decision whose PDF publication/recheck did not commit.

        Called under the workflow correction guard. Keep the completed fix
        and its comparison available so the operator can retry or reject it.
        """
        _write_state(fix_dir_for(pdf_path), previous)

    def mark_published(self, pdf_path: Path | str, *, sha256: str, revision: int) -> None:
        """Запоминает ревизию, ставшую производственным PDF после принятия исправления."""
        state = read_state_file(pdf_path)
        if not state:
            return
        state["published"] = {"sha256": sha256, "revision": revision, "at": _now()}
        _write_state(fix_dir_for(pdf_path), state)

    def accepted_source_copy(self, pdf_path: Path | str) -> tuple[Path, dict[str, Any]]:
        """Копия принятого исправления исходного PDF для сборки заказа и сведения о нём.

        Копия (``accepted.pdf``) отделена от ``fixed.pdf``: повторный запуск исправления
        перезаписывает ``fixed.pdf``, но не меняет файл, из которого собирается заказ.
        """
        pdf = Path(pdf_path)
        state = read_state_file(pdf) or {}
        accepted = self.accepted_pdf(pdf)
        if accepted is None:
            raise CallasFixError(
                "Принятое исправление Callas не найдено. Запустите исправление заново.",
                status_code=409,
                code="callas_fix_not_ready",
            )
        target = fix_dir_for(pdf) / ACCEPTED_SOURCE_PDF_NAME
        temporary = target.with_name(f".{target.stem}.{uuid4().hex[:8]}.tmp.pdf")
        try:
            shutil.copyfile(accepted, temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        plan = state.get("plan") or {}
        source = state.get("source") or {}
        summary = state.get("summary") or {}
        info = {
            "status": STATUS_ACCEPTED,
            "plan": plan.get("key"),
            "plan_title": plan.get("title"),
            "accepted_at": state.get("decided_at"),
            "source": {"size": source.get("size"), "mtime_ns": source.get("mtime_ns")},
            # Контракт для повторной проверки и PitStop: только поля дорисованы →
            # содержимое проверяется по ``baseline_pdf`` (PDF стороны до исправления).
            "bleed_only": summary.get("bleed_only") is True,
            "bleed_only_reasons": [str(reason) for reason in summary.get("bleed_only_reasons") or ()],
            "baseline_pdf": str(baseline_pdf_for(pdf)),
        }
        return target, info

    def accepted_pdf(self, pdf_path: Path | str) -> Path | None:
        """Принятый результат исправления или ``None``."""
        state = read_state_file(pdf_path)
        if not state or state.get("status") != STATUS_ACCEPTED:
            return None
        fixed = fix_dir_for(pdf_path) / str(state.get("fixed_pdf") or FIXED_PDF_NAME)
        return fixed if fixed.is_file() else None

    def publish_accepted(self, pdf_path: Path | str) -> Path | None:
        """Печатная копия принятого исправления рядом с исходным PDF или ``None``.

        Нужна на шаге «в печать»: результат копируется в ``<имя>_callas-fixed.pdf``, и
        дальше заказ идёт обычным путём (проверка PDF, PitStop, новая ревизия). Если
        исходный PDF изменился после принятия, печать отклоняется, а не подменяется.
        """
        accepted = self.accepted_pdf(pdf_path)
        if accepted is None:
            return None
        pdf = Path(pdf_path)
        state = read_state_file(pdf) or {}
        published = state.get("published") or {}
        if (
            pdf.is_file()
            and published.get("sha256")
            and hashlib.sha256(pdf.read_bytes()).hexdigest() == published["sha256"]
        ):
            # Производственный PDF — ровно та ревизия, что была опубликована после
            # принятия (в т. ч. после перевода RGB → CMYK, когда байты уже не совпадают
            # с fixed.pdf): публиковать нечего.
            return None
        if pdf.is_file() and filecmp.cmp(accepted, pdf, shallow=False):
            # Принятое исправление уже стало производственным PDF (после
            # повторной проверки и раскладки по папке статуса): публиковать нечего.
            return None
        source = state.get("source") or {}
        current = _signature(pdf) if pdf.is_file() else None
        if current is None or (source.get("size"), source.get("mtime_ns")) != (
            current["size"],
            current["mtime_ns"],
        ):
            raise CallasFixError(
                "PDF заказа изменился после принятого исправления Callas. Запустите исправление заново.",
                status_code=409,
                code="callas_fix_stale",
            )
        target = fix_dir_for(pdf) / f"{pdf.stem}_callas-fixed.pdf"
        temporary = target.with_name(f".{target.stem}.{uuid4().hex[:8]}.tmp.pdf")
        try:
            shutil.copyfile(accepted, temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def file_path(self, pdf_path: Path | str, kind: str, *, page: int = 1) -> Path | None:
        """Путь к картинке сравнения (``original|result|overlay``) страницы ``page``
        или к ``pdf`` результата."""
        state = read_state_file(pdf_path)
        if not state:
            return None
        directory = fix_dir_for(pdf_path)
        if kind == "pdf":
            name = state.get("fixed_pdf") if page == 1 else None
            target = directory / str(name) if name else None
        elif kind in IMAGE_KINDS and page == 1:
            name = (state.get("images") or {}).get(kind)
            target = directory / COMPARE_DIR / str(name) if name else None
        elif kind in IMAGE_KINDS and isinstance(page, int) and page >= 2:
            summary = state.get("summary") or {}
            extra = next(
                (
                    item for item in summary.get("pages") or ()
                    if isinstance(item, Mapping) and item.get("page") == page
                ),
                None,
            )
            name = ((extra or {}).get("images") or {}).get(kind)
            target = directory / COMPARE_DIR / f"page-{page}" / str(name) if name else None
        else:
            return None
        return target if target is not None and target.is_file() else None
