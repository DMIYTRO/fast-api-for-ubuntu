"""Разметка разрешения: диагностическое превью с ppi каждого изображения PDF заказа.

Кнопка «Показать разрешение» на карточке заказа. Строится только по нажатию (не во
время проверки) из текущего PDF заказа — производственного (текущая ревизия, после
RGB → CMYK и коррекции размера: тот файл, что проверял PitStop) или, если его нет,
диагностического.

Порядок: копия PDF во временной папке на общем диске → профиль callas
``Image Magic/Mark_images_with_resolution_in_a_loop_via_a_r`` в режиме ``fix`` (ставит на
середину каждого цветного/полутонового изображения надпись «<N>ppi»: красную ниже
250 ppi, зелёную от 250 ppi; цвета и порог задаёт сам профиль) → рендер страниц
размеченного PDF через callas → одна картинка ImageMagick (двусторонний заказ: лицо
слева, оборот справа). Результат лежит в ``Previews/LowRes/`` входной папки:

* ``<id>_lowres.jpg`` — картинка для оператора и для возврата клиенту;
* ``<id>_lowres.pdf`` — размеченный PDF, только для скачивания: в ``PDF/Checked|Rework|
  Failed|Print`` он не попадает никогда, в печать уйти не может;
* ``<id>_lowres.json`` — кэш: SHA-256 исходного PDF, время, страницы, порог, профиль.

Повторное нажатие при неизменном PDF сразу возвращает кэш. Одновременно для заказа
строится одна разметка (блокировка). Ошибки callas и ImageMagick превращаются в
``LowResPreviewError`` с текстом для оператора.

XML-отчёт профиля содержит только счётчики исправлений (по одному ``<fixup count="1">``
на изображение), но не ppi и не страницу каждого изображения: список изображений
(``images``) поэтому не заполняется, известно только число размеченных.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from core.pdf_inspector import POINTS_PER_INCH, MM_PER_INCH, inspect_pdf
from core.preview_cache import jpeg_output_arguments
from core.share_tempdir import share_temp_dir
from core.tool_runner import run_command
from services.callas.errors import CallasError
from services.callas.renderer import dpi_for_longest_side
from services.return_preview import side_by_side_command

logger = logging.getLogger("image_magic.lowres_preview")

PROFILE = "Image Magic/Mark_images_with_resolution_in_a_loop_via_a_r"
#: Порог профиля (красные ниже, зелёные от него); PitStop 1150 считает низким ниже 275 ppi.
THRESHOLD_PPI = 250
PITSTOP_THRESHOLD_PPI = 275
LEGEND = (
    "Красные — ниже 250 ppi, зелёные — 250 ppi и выше. "
    "PitStop считает низким разрешение ниже 275 ppi."
)
OUTPUT_RELATIVE_PATH = Path("Previews") / "LowRes"
#: Длинная сторона страницы на картинке, px.
PAGE_LONG_SIDE_PX = 1800
#: Предел JPEG: как у загружаемого оператором превью для возврата (1 МБ).
MAX_IMAGE_BYTES = 1000 * 1024
JPEG_QUALITY = 90
SIDE_CAPTIONS = ("Лицо", "Оборот")
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)
NO_IMAGES_MESSAGE = "В PDF нет цветных или полутоновых изображений: размечать нечего."


class LowResPreviewError(Exception):
    """Ошибка, текст которой можно показать оператору."""

    def __init__(self, message: str, *, status_code: int = 422, code: str = "lowres_preview_error") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _storage_id(value: str) -> str:
    normalized = str(value).strip()
    if not normalized or Path(normalized).name != normalized or normalized in {".", ".."}:
        raise ValueError("Номер заказа не должен быть пустым или содержать путь.")
    return normalized


def output_dir(input_root: Path) -> Path:
    return Path(input_root) / OUTPUT_RELATIVE_PATH


def output_paths(input_root: Path, storage_id: str) -> dict[str, Path]:
    """Пути картинки, размеченного PDF и кэша заказа (``storage_id`` — как у превью возврата)."""
    base = output_dir(input_root) / f"{_storage_id(storage_id)}_lowres"
    return {
        "image": base.with_name(f"{base.name}.jpg"),
        "pdf": base.with_name(f"{base.name}.pdf"),
        "meta": base.with_name(f"{base.name}.json"),
    }


def read_meta(input_root: Path, storage_id: str) -> dict[str, Any] | None:
    try:
        meta = json.loads(output_paths(input_root, storage_id)["meta"].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def find_font() -> Path | None:
    for candidate in FONT_CANDIDATES:
        path = Path(candidate)
        if path.is_file():
            return path
    return None


def _replace_atomically(source: Path, target: Path) -> None:
    """Копия рядом с целью и ``os.replace``: читатель видит либо старый файл, либо новый."""
    staged = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        shutil.copyfile(source, staged)
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)


def _write_json_atomically(target: Path, payload: dict[str, Any]) -> None:
    staged = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        staged.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)


class LowResPreviewService:
    """Строит и кэширует разметку разрешения; без FastAPI (транспорт — в server/)."""

    def __init__(
        self,
        client: Any,
        *,
        profile: str = PROFILE,
        page_long_side_px: int = PAGE_LONG_SIDE_PX,
        max_image_bytes: int = MAX_IMAGE_BYTES,
        font: Path | None | bool = True,
    ) -> None:
        self._client = client
        self.profile = profile
        self.page_long_side_px = int(page_long_side_px)
        self.max_image_bytes = int(max_image_bytes)
        # True — найти шрифт подписей «Лицо»/«Оборот» самостоятельно; None/False — без подписей.
        self._font = find_font() if font is True else (Path(font) if font else None)
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    # ------------------------------------------------------------------ public

    def cached(self, pdf: Path, *, input_root: Path, storage_id: str, sha256: str | None = None) -> dict[str, Any] | None:
        """Результат для текущего PDF, если он уже построен, иначе ``None``."""
        meta = read_meta(input_root, storage_id)
        if not meta or meta.get("profile") != self.profile:
            return None
        paths = output_paths(input_root, storage_id)
        if not (paths["image"].is_file() and paths["pdf"].is_file()):
            return None
        digest = sha256 or _sha256(Path(pdf))
        if meta.get("source_sha256") != digest:
            return None
        return {**meta, "cached": True}

    def generate(self, pdf: Path, *, input_root: Path, storage_id: str) -> dict[str, Any]:
        """Построить разметку (или вернуть кэш при неизменном PDF)."""
        source = Path(pdf)
        if not source.is_file():
            raise LowResPreviewError("PDF заказа не найден: разметку строить не из чего.", status_code=404, code="pdf_not_found")
        storage_id = _storage_id(storage_id)
        paths = output_paths(input_root, storage_id)
        with self._lock_for(paths["meta"]):
            digest = _sha256(source)
            cached = self.cached(source, input_root=input_root, storage_id=storage_id, sha256=digest)
            if cached is not None:
                return cached
            target_dir = paths["image"].parent
            target_dir.mkdir(parents=True, exist_ok=True)
            with share_temp_dir(prefix=".lowres-", dir=target_dir) as raw_tmp:
                tmp = Path(raw_tmp)
                result = self._build(source, tmp, digest)
                _replace_atomically(tmp / "marked.pdf", paths["pdf"])
                _replace_atomically(tmp / "lowres.jpg", paths["image"])
                _write_json_atomically(paths["meta"], result)
            logger.info(
                "lowres_preview.generated storage_id=%s pages=%s marked=%s",
                storage_id, result.get("pages"), result.get("marked_images"),
            )
            return {**result, "cached": False}

    # ----------------------------------------------------------------- internal

    def _lock_for(self, key: Path) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(str(key), threading.Lock())

    def _build(self, source: Path, tmp: Path, digest: str) -> dict[str, Any]:
        copy = tmp / "source.pdf"
        shutil.copyfile(source, copy)
        if _sha256(copy) != digest:
            raise LowResPreviewError(
                "PDF заказа изменился во время копирования: повторите через несколько секунд.",
                status_code=409, code="pdf_changed",
            )
        marked = tmp / "marked.pdf"
        marked_count = self._mark(copy, marked)
        page_sizes = self._page_sizes(marked)
        pages = self._render(marked, tmp / "pages", page_sizes)
        self._compose(pages, tmp / "lowres.jpg")
        return {
            "status": "ready",
            "generated_at": _now(),
            "source_pdf": source.name,
            "source_sha256": digest,
            "pages": len(pages),
            "marked_images": marked_count,
            # Отчёт профиля не содержит ppi и страниц изображений (см. docstring модуля).
            "images": None,
            "threshold_ppi": THRESHOLD_PPI,
            "pitstop_threshold_ppi": PITSTOP_THRESHOLD_PPI,
            "legend": LEGEND,
            "profile": self.profile,
            "captions": bool(self._font) and len(pages) == 2,
        }

    def _mark(self, copy: Path, marked: Path) -> int:
        job_id = f"lowres-{uuid.uuid4().hex[:16]}"
        try:
            result = self._client.process(copy, self.profile, "fix", job_id=job_id, report=("xml",))
            applied = int((result.summary or {}).get("fixups_applied") or 0)
            if applied == 0:
                raise LowResPreviewError(NO_IMAGES_MESSAGE, code="lowres_no_images")
            output = getattr(result, "output_pdf", None)
            if output is None or not Path(output).is_file():
                raise LowResPreviewError(
                    "Callas не вернул размеченный PDF: повторите позже или проверьте профиль "
                    f"«{self.profile}» на сервере callas.",
                    status_code=502, code="lowres_callas_failed",
                )
            shutil.copyfile(output, marked)
            return applied
        except LowResPreviewError:
            raise
        except CallasError as exc:
            raise LowResPreviewError(
                f"Разметка разрешения не выполнена: {str(exc).strip() or type(exc).__name__}",
                status_code=502, code="lowres_callas_failed",
            ) from exc
        finally:
            self._cleanup_job(job_id)

    @staticmethod
    def _page_sizes(marked: Path) -> list[tuple[float, float]]:
        inspection = inspect_pdf(marked)
        if inspection.errors and not inspection.pages:
            raise LowResPreviewError(
                "Размеченный PDF не читается: " + "; ".join(inspection.errors),
                status_code=502, code="lowres_bad_output",
            )
        if not inspection.pages:
            raise LowResPreviewError("В размеченном PDF нет страниц.", status_code=502, code="lowres_bad_output")
        to_pt = POINTS_PER_INCH / MM_PER_INCH
        return [(page.width_mm * to_pt, page.height_mm * to_pt) for page in inspection.pages]

    def _render(self, marked: Path, target: Path, page_sizes: list[tuple[float, float]]) -> list[Path]:
        longest = max(max(width, height) for width, height in page_sizes)
        # Мелкий формат (визитка) увеличивается сильнее, чем в обычных превью: надписи ppi
        # должны читаться. Все страницы — с одним DPI, чтобы лицо и оборот совпадали по масштабу.
        dpi = dpi_for_longest_side(longest, longest, self.page_long_side_px, max_scale=12.0)
        job_id = f"lowres-render-{uuid.uuid4().hex[:16]}"
        try:
            pages = self._client.render(marked, dpi, job_id=job_id, expected_pages=len(page_sizes))
            target.mkdir(parents=True, exist_ok=True)
            copied = []
            for number, page in enumerate(pages, start=1):
                destination = target / f"page-{number}.png"
                shutil.copyfile(page, destination)
                copied.append(destination)
            return copied
        except CallasError as exc:
            raise LowResPreviewError(
                f"Не удалось отрисовать размеченный PDF в callas: {str(exc).strip() or type(exc).__name__}",
                status_code=502, code="lowres_render_failed",
            ) from exc
        finally:
            delete = getattr(self._client, "delete", None)
            if delete is not None:
                try:
                    delete(job_id)
                except Exception:  # noqa: BLE001 — уборка задания не должна ломать результат
                    pass
            self._cleanup_job(job_id)

    def _compose(self, pages: Iterable[Path], output: Path) -> None:
        page_list = list(pages)
        magick = shutil.which("magick")
        if not magick:
            raise LowResPreviewError("Не найдена утилита ImageMagick (`magick`).", status_code=500, code="lowres_compose_failed")
        captions = SIDE_CAPTIONS if len(page_list) == 2 else None
        output_options = [
            *jpeg_output_arguments(quality=JPEG_QUALITY),
            "-define", f"jpeg:extent={max(64, self.max_image_bytes // 1024)}KB",
        ]
        # Страницы уже отрисованы одним DPI под нужный размер: без -resize.
        command = side_by_side_command(
            magick, page_list, output,
            height=None,
            output_options=output_options,
            captions=captions,
            font=self._font,
            caption_bar_px=max(40, self.page_long_side_px // 28),
        )
        try:
            run_command(command, check=True, capture_output=True)
        except Exception as exc:  # noqa: BLE001 — ExternalToolError, OSError, CalledProcessError
            raise LowResPreviewError(
                "Не удалось собрать картинку разметки разрешения (ImageMagick).",
                status_code=500, code="lowres_compose_failed",
            ) from exc
        if not output.is_file() or output.stat().st_size == 0:
            raise LowResPreviewError(
                "ImageMagick не создал картинку разметки разрешения.",
                status_code=500, code="lowres_compose_failed",
            )

    def _cleanup_job(self, job_id: str) -> None:
        job_dir = getattr(self._client, "job_dir", None)
        if job_dir is None:
            return
        try:
            shutil.rmtree(job_dir(job_id), ignore_errors=True)
        except Exception:  # noqa: BLE001 — уборка не должна ломать результат
            logger.warning("lowres_preview.cleanup_failed job_id=%s", job_id, exc_info=True)
