"""Схема послепечатной обработки, загруженная заказчиком в Sborka.

Иногда заказчик прикладывает к заказу картинку с постпечатной схемой (например,
положение отверстий сверления). Она лежит по адресу
``https://sborka.ua/preview/<filename>-preview.png``, где ``<filename>`` — поле
``filename`` записи ``orderinfo``. Список папки пуст, поэтому единственный способ
узнать о схеме — запрос по каждому заказу; схема бывает только у заказов с
постпечаткой (и редко), поэтому запрашиваются только они.

Скачивание идёт в фоне и никогда не задерживает проверку: 404 или любой сбой —
просто «схемы нет» (в журнал приложения, не в ошибки заказа). Найденная схема
сохраняется в ``<входная папка>/Previews/Customer/<заказ>/<filename>-preview.<ext>``.

Схема бывает и ``.png``, и ``.jpg``. Кроме того, Sborka иногда сохраняет её под
именем с неподставленным шаблоном префикса — ``{dateD}_{delivery}(P)_…`` вместо
``30_NP(P)_…`` (заказ 26286105). Поэтому адреса пробуются по очереди
(``preview_urls``): настоящее имя, затем имя с шаблоном, каждое с ``.png`` и ``.jpg``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
import logging
import os
from pathlib import Path
import re
import ssl
import tempfile
import threading
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote
import urllib.error
import urllib.request


logger = logging.getLogger("image_magic.customer_preview")

PREVIEW_BASE_URL = "https://sborka.ua/preview/"
PREVIEW_SUFFIX = "-preview"
PREVIEW_DIR = ("Previews", "Customer")
MAX_PREVIEW_BYTES = 10 * 1024 * 1024
MAX_FILENAME_LENGTH = 200
_CONTENT_TYPES = {"image/png": ".png", "image/jpeg": ".jpg"}
_MAGIC = {".png": b"\x89PNG\r\n\x1a\n", ".jpg": b"\xff\xd8\xff"}
# Буквы (в том числе кириллица), цифры, «_», скобки и обычная пунктуация имён Sborka.
_FILENAME_RE = re.compile(r"^[\w()\[\]\-.,+=@#№ ]+$")
_SIDE_SUFFIX_RE = re.compile(r"-(?:face|back)$", re.IGNORECASE)
_ORDER_DIR_RE = re.compile(r"^[\w\-]{1,64}$")
#: Префикс имени Sborka «<день>_<доставка>» перед «(»: ``30_NP(P)_…``, ``29_KS(P)_…``.
_PREFIX_RE = re.compile(r"^\d{1,2}_[A-Za-z]+(?=\()")
TEMPLATE_PREFIX = "{dateD}_{delivery}"
PREVIEW_EXTENSIONS = (".png", ".jpg")


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes | None = None

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        for key, value in self.headers.items():
            if key.lower() == wanted:
                return value
        return None


#: (method, url, timeout, max_bytes) -> HttpResponse. ``body`` только для GET;
#: читается не больше ``max_bytes + 1`` байт, чтобы распознать слишком большой файл.
HttpFunction = Callable[[str, str, float, int], HttpResponse]


def urllib_http(method: str, url: str, timeout: float, max_bytes: int) -> HttpResponse:
    """HTTP через urllib с тем же непроверяемым SSL, что и скрипты sborka_api."""
    request = urllib.request.Request(
        url, method=method, headers={"User-Agent": "image-magic-prepress"}
    )
    context = ssl._create_unverified_context()  # noqa: S323 — как sborka_orderinfo.py
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            body = response.read(max_bytes + 1) if method == "GET" else None
            return HttpResponse(response.status, dict(response.headers.items()), body)
    except urllib.error.HTTPError as exc:
        return HttpResponse(exc.code, dict(exc.headers.items()) if exc.headers else {}, None)


# ------------------------------------------------------------------ eligibility


def has_postpress(postpress: Mapping[str, Any] | None) -> bool:
    """У заказа есть постпечатка: сгиб/биг или хотя бы одна операция (ламинация тоже)."""
    if not isinstance(postpress, Mapping):
        return False
    return bool(postpress.get("fold")) or bool(postpress.get("operations"))


def valid_filename(value: object) -> str | None:
    """Имя файла Sborka, безопасное для URL и пути, или ``None``."""
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not name or len(name) > MAX_FILENAME_LENGTH:
        return None
    if "/" in name or "\\" in name or ".." in name or name.startswith("."):
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in name):
        return None
    if not _FILENAME_RE.fullmatch(name):
        return None
    return name


def derive_filename(paths: Iterable[Path | str]) -> str | None:
    """Имя из исходного файла заказа: без расширения и без хвоста ``-face``/``-back``.

    Нужен, когда у заказа нет записи orderinfo со ``filename`` (подзаказы комплекта,
    девятизначные номера). Берётся лицевая сторона, если она есть.
    """
    stems = [Path(str(item)).stem for item in paths]
    if not stems:
        return None
    face = [stem for stem in stems if stem.lower().endswith("-face")]
    stem = (face or stems)[0]
    return valid_filename(_SIDE_SUFFIX_RE.sub("", stem))


def preview_url(
    filename: str, base_url: str = PREVIEW_BASE_URL, extension: str = ".png"
) -> str:
    return f"{base_url}{quote(filename + PREVIEW_SUFFIX, safe='')}{extension}"


def template_filename(filename: str) -> str | None:
    """Имя с неподставленным шаблоном префикса (``{dateD}_{delivery}``) или ``None``."""
    if not _PREFIX_RE.match(filename):
        return None
    return _PREFIX_RE.sub(TEMPLATE_PREFIX, filename, count=1)


def preview_urls(filename: str, base_url: str = PREVIEW_BASE_URL) -> list[str]:
    """Адреса схемы в порядке проверки: настоящее имя, затем шаблонное; ``.png``, ``.jpg``."""
    names = [filename]
    templated = template_filename(filename)
    if templated:
        names.append(templated)
    return [preview_url(name, base_url, ext) for name in names for ext in PREVIEW_EXTENSIONS]


def public_customer_preview(
    preview: Mapping[str, Any], run_id: str, order_ref: str
) -> dict[str, Any]:
    """DTO схемы для карточки: адрес ручки, ``last_modified``, ``source_url`` (+ file, size)."""
    return {
        **dict(preview),
        "url": (
            f"/api/checks/{quote(str(run_id), safe='')}/orders/"
            f"{quote(str(order_ref), safe='')}/customer-preview"
        ),
        "last_modified": preview.get("last_modified"),
        "source_url": preview.get("source_url"),
    }


def _timestamp(last_modified: str | None) -> float | None:
    if not last_modified:
        return None
    try:
        return parsedate_to_datetime(last_modified).timestamp()
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def _content_suffix(response: HttpResponse) -> str | None:
    content_type = (response.header("Content-Type") or "").split(";", 1)[0].strip().lower()
    return _CONTENT_TYPES.get(content_type)


def _content_length(response: HttpResponse) -> int | None:
    try:
        return int(response.header("Content-Length") or "")
    except ValueError:
        return None


# ---------------------------------------------------------------------- fetcher


class CustomerPreviewFetcher:
    """Запрос схемы одного заказа и фоновая очередь с ограничением параллельности.

    Один экземпляр на приложение: ограничение ``max_concurrent`` общее для всех проверок.
    """

    def __init__(
        self,
        *,
        http: HttpFunction | None = None,
        timeout_seconds: float = 20.0,
        max_concurrent: int = 3,
        base_url: str = PREVIEW_BASE_URL,
        max_bytes: int = MAX_PREVIEW_BYTES,
    ) -> None:
        self.http = http or urllib_http
        self.timeout_seconds = float(timeout_seconds)
        self.base_url = base_url
        self.max_bytes = int(max_bytes)
        self._slots = threading.BoundedSemaphore(max(1, int(max_concurrent)))

    def fetch(self, order_id: str, filename: str, input_root: Path) -> dict[str, Any] | None:
        """Скачать схему заказа; ``None`` — схемы нет или запрос не удался (не ошибка)."""
        name = valid_filename(filename)
        order_dir = str(order_id or "")
        if name is None or not _ORDER_DIR_RE.fullmatch(order_dir):
            logger.info(
                "customer_preview.invalid_name order_id=%s filename=%r", order_id, filename
            )
            return None
        urls = preview_urls(name, self.base_url)
        url = urls[0]
        with self._slots:
            try:
                return self._fetch(order_dir, name, urls, Path(input_root))
            except Exception as exc:  # noqa: BLE001 — схема никогда не ломает проверку
                logger.warning(
                    "customer_preview.failed order_id=%s url=%s error=%s",
                    order_id, url, exc or type(exc).__name__,
                )
                return None

    def _fetch(
        self, order_id: str, name: str, urls: list[str], input_root: Path
    ) -> dict[str, Any] | None:
        statuses: list[int] = []
        for url in urls:
            head = self.http("HEAD", url, self.timeout_seconds, self.max_bytes)
            if head.status == 200:
                break
            statuses.append(head.status)
        else:
            unexpected = [status for status in statuses if status != 404]
            log = logger.warning if unexpected else logger.info
            log(
                "customer_preview.absent order_id=%s status=%s tried=%s",
                order_id, unexpected[0] if unexpected else 404, len(urls),
            )
            return None
        suffix = _content_suffix(head)
        if suffix is None:
            logger.warning(
                "customer_preview.wrong_type order_id=%s type=%s",
                order_id, head.header("Content-Type"),
            )
            return None
        length = _content_length(head)
        if length is not None and length > self.max_bytes:
            logger.warning("customer_preview.too_large order_id=%s size=%s", order_id, length)
            return None
        folder = input_root.joinpath(*PREVIEW_DIR, order_id)
        target = folder / f"{name}{PREVIEW_SUFFIX}{suffix}"
        last_modified = head.header("Last-Modified")
        stamp = _timestamp(last_modified)
        if stamp is not None and target.is_file():
            stat = target.stat()
            if abs(stat.st_mtime - stamp) < 1 and (length is None or stat.st_size == length):
                logger.info("customer_preview.unchanged order_id=%s file=%s", order_id, target.name)
                return self._result(target, url, last_modified, stat.st_size)

        response = self.http("GET", url, self.timeout_seconds, self.max_bytes)
        if response.status != 200:
            logger.warning("customer_preview.get_failed order_id=%s status=%s", order_id, response.status)
            return None
        suffix = _content_suffix(response) or ""
        body = response.body or b""
        if suffix != target.suffix:
            logger.warning(
                "customer_preview.wrong_type order_id=%s type=%s",
                order_id, response.header("Content-Type"),
            )
            return None
        if not body or len(body) > self.max_bytes:
            logger.warning("customer_preview.bad_size order_id=%s size=%s", order_id, len(body))
            return None
        if not body.startswith(_MAGIC[suffix]):
            logger.warning("customer_preview.not_image order_id=%s", order_id)
            return None
        last_modified = response.header("Last-Modified") or last_modified
        folder.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, body, _timestamp(last_modified))
        logger.info(
            "customer_preview.saved order_id=%s file=%s size=%s", order_id, target.name, len(body)
        )
        return self._result(target, url, last_modified, len(body))

    @staticmethod
    def _result(target: Path, url: str, last_modified: str | None, size: int) -> dict[str, Any]:
        return {
            "file": str(target),
            "source_url": url,
            "last_modified": last_modified,
            "size": int(size),
        }

    def submit(
        self,
        order_id: str,
        filename: str,
        input_root: Path,
        on_done: Callable[[dict[str, Any] | None], None],
    ) -> threading.Thread:
        """Скачать в фоне (поток-демон); ``on_done`` вызывается всегда, с результатом."""

        def run() -> None:
            result = self.fetch(order_id, filename, input_root)
            try:
                on_done(result)
            except Exception:  # noqa: BLE001
                logger.exception("customer_preview.callback_failed order_id=%s", order_id)

        thread = threading.Thread(target=run, name="sborka-customer-preview", daemon=True)
        thread.start()
        return thread


def _atomic_write(target: Path, content: bytes, mtime: float | None) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".imcp-", suffix=".part", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if mtime is not None:
            os.utime(temporary, (mtime, mtime))
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
