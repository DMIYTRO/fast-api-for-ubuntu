"""HTTP-клиент службы callas: merge PDF, render страниц PDF в PNG, process, inkcoverage.

Контракт (docs/callas/openapi.json):
  PUT /jobs/{job_id} -> 202, тело MergeSpec / RenderSpec (output, input.path и
  out_dir служба требует АБСОЛЮТНЫМИ путями в её представлении);
  GET /jobs/{job_id} -> status, files[{name,path,size,sha256}], error, bad_inputs;
  DELETE /jobs/{job_id}.

Файлы обмениваются через общую папку. Приложение копирует входы в
``<share>/.callas/<job_id>/in`` до PUT; служба пишет результат в
``<share>/.callas/<job_id>/out``. В теле запроса пути указываются так, как их
видит *сервер* callas (``server_share``, по умолчанию ``/mnt/shared``), а
приложение читает результат по своему ``share``.

Токен никогда не логируется; в сообщения об ошибках попадают только
усечённые и очищенные фрагменты ответа службы.
"""

from __future__ import annotations

import email.utils
import hashlib
import json
import logging
import os
import random
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

import httpx

from services.callas.ink_coverage import (
    INK_COVERAGE_BOXES,
    InkCoverageResult,
    parse_ink_coverage,
)
from services.callas.errors import (
    CallasAuthError,
    CallasCancelledError,
    CallasConfigError,
    CallasConflictError,
    CallasError,
    CallasJobFailedError,
    CallasNotFoundError,
    CallasProtocolError,
    CallasResultError,
    CallasTimeoutError,
    CallasUnavailableError,
    CallasValidationError,
)

logger = logging.getLogger("services.callas")

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PAGE_RE = re.compile(r"^page-(\d+)\.png$", re.IGNORECASE)
_RETRY_STATUSES = frozenset({429, 502, 503, 504})
_AMBIGUOUS_STATUSES = frozenset({502, 503, 504})
_DONE_STATUSES = frozenset({"done", "succeeded", "success", "completed"})
_FAILED_STATUSES = frozenset({"failed", "error"})
_CANCELLED_STATUSES = frozenset({"cancelled", "canceled"})
_MAX_ERROR_TEXT = 500
_MAX_RETRY_AFTER_SECONDS = 120.0
_HASH_CHUNK = 1024 * 1024
_PROCESS_MODES = frozenset({"analyze", "fix"})
_PROCESS_REPORTS = frozenset({"xml", "pdf"})
_PROCESS_FIXED_NAME = "fixed.pdf"
_PROCESS_SUMMARY_KEYS = ("errors", "warnings", "infos", "fixups_applied", "fixups_failed")
# Коды завершения pdfToolbox: 0 — чисто; 1–8 — найдены проблемы или выполнены
# исправления (это результат проверки, не сбой); 100+ — ошибка выполнения.
_EXIT_OK_MAX = 8


def deterministic_job_id(prefix: str, *parts: str) -> str:
    """Стабильный job_id: повтор той же операции получает тот же идентификатор."""
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:20]
    safe_prefix = re.sub(r"[^A-Za-z0-9._-]+", "-", prefix).strip("-.") or "job"
    return f"{safe_prefix[:40]}-{digest}"


@dataclass(frozen=True, slots=True)
class CallasConfig:
    base_url: str
    token: str = field(repr=False)
    share: Path = Path("/mnt/shared")
    server_share: str = "/mnt/shared"
    user: str = "image-magic"
    request_timeout_seconds: float = 30.0
    job_timeout_seconds: float = 600.0
    poll_interval_seconds: float = 1.0
    poll_max_interval_seconds: float = 5.0
    max_concurrent_jobs: int = 2
    max_retries: int = 4
    backoff_base_seconds: float = 0.5
    backoff_max_seconds: float = 8.0
    file_visible_timeout_seconds: float = 15.0

    @classmethod
    def from_settings(cls, settings: Any) -> "CallasConfig":
        return cls(
            base_url=settings.callas_url,
            token=settings.callas_token,
            share=Path(settings.callas_share),
            server_share=settings.callas_server_share,
            user=settings.callas_user,
            request_timeout_seconds=settings.callas_request_timeout_seconds,
            job_timeout_seconds=settings.callas_job_timeout_seconds,
            poll_interval_seconds=settings.callas_poll_interval_seconds,
            max_concurrent_jobs=settings.callas_max_concurrent_jobs,
            max_retries=settings.callas_max_retries,
        )


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Итог задания ``process`` (проверка или исправление процесс-планом pdfToolbox).

    ``status`` — ``result_status`` службы (например ``ok``/``warnings``/``errors``);
    если служба его не передала, выводится из счётчиков. ``summary`` содержит ключи
    errors, warnings, infos, fixups_applied, fixups_failed. ``output_pdf`` задан только
    для режима ``fix`` и всегда указывает на новый файл в папке задания (``job_dir``),
    а не на исходный PDF. ``report_xml`` — проверенный по SHA-256 XML-отчёт.
    Папку ``job_dir(job_id)`` после использования удаляет вызывающий код.
    """

    job_id: str
    mode: str
    status: str
    exit_code: int
    summary: dict[str, int]
    output_pdf: Path | None
    report_xml: Path | None


@dataclass(slots=True)
class _Ctx:
    job_id: str
    deadline: float
    cancel: threading.Event
    put_sent: bool = False


class CallasClient:
    """Потокобезопасный клиент; один экземпляр на приложение."""

    def __init__(
        self,
        config: CallasConfig,
        *,
        http_client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
    ) -> None:
        if not config.base_url.strip():
            raise CallasConfigError(
                "Callas не настроен: не задан IMAGE_MAGIC_CALLAS_URL."
            )
        if not config.token.strip():
            raise CallasConfigError(
                "Callas не настроен: не задан IMAGE_MAGIC_CALLAS_TOKEN."
            )
        if config.max_concurrent_jobs < 1:
            raise CallasConfigError("Лимит одновременных заданий callas должен быть не меньше 1.")
        self._cfg = config
        self._base = config.base_url.strip().rstrip("/")
        self._clock = clock
        self._rng = rng
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(follow_redirects=False)
        self._slots = threading.BoundedSemaphore(config.max_concurrent_jobs)
        self._share = Path(config.share)
        self._server_share = PurePosixPath(config.server_share)
        if not self._server_share.is_absolute():
            raise CallasConfigError("IMAGE_MAGIC_CALLAS_SERVER_SHARE должен быть абсолютным путём.")

    @classmethod
    def from_settings(cls, settings: Any, **kwargs: Any) -> "CallasClient":
        return cls(CallasConfig.from_settings(settings), **kwargs)

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    # ------------------------------------------------------------------ API

    def merge(
        self,
        inputs: Sequence[Path | str],
        output_name: str,
        *,
        job_id: str,
        cancel: threading.Event | None = None,
        optimize: bool | None = None,
    ) -> Path:
        """Объединяет PDF по порядку; возвращает локальный путь результата в папке задания."""
        self._validate_job_id(job_id)
        sources = [Path(item) for item in inputs]
        if not sources:
            raise CallasConfigError("Для объединения нужен хотя бы один PDF.", job_id=job_id)
        if (
            not output_name
            or output_name != Path(output_name).name
            or "\\" in output_name
            or output_name in {".", ".."}
            or not output_name.lower().endswith(".pdf")
        ):
            raise CallasConfigError(
                "Имя результата объединения должно быть именем файла .pdf без пути.",
                job_id=job_id,
            )
        ctx = self._new_ctx(job_id, cancel)
        with self._slot(ctx):
            staged = self._stage_inputs(job_id, sources)
            spec: dict[str, Any] = {
                "type": "merge",
                "inputs": [
                    {"path": server_path, "sha256": digest}
                    for server_path, digest in staged
                ],
                "output": str(self._server_job_dir(job_id) / "out" / output_name),
                "skip_corrupt": False,
            }
            if optimize is not None:
                spec["optimize"] = optimize
            job = self._execute(ctx, spec, sources)
            files = self._verify_files(ctx, job)
            expected = files.get(output_name)
            if expected is None or len(files) != 1:
                raise CallasResultError(
                    f"Callas вернул неожиданный набор файлов для объединения (ожидался {output_name}).",
                    job_id=job_id,
                )
            return expected

    def render(
        self,
        input: Path | str,
        dpi: int = 150,
        *,
        job_id: str,
        cancel: threading.Event | None = None,
        expected_pages: int | None = None,
    ) -> list[Path]:
        """Рендерит страницы PDF в PNG; возвращает пути, отсортированные по номеру страницы."""
        self._validate_job_id(job_id)
        if not isinstance(dpi, int) or isinstance(dpi, bool) or not 36 <= dpi <= 1200:
            raise CallasConfigError("DPI рендера должен быть целым числом от 36 до 1200.", job_id=job_id)
        source = Path(input)
        ctx = self._new_ctx(job_id, cancel)
        with self._slot(ctx):
            staged = self._stage_inputs(job_id, [source])
            server_path, digest = staged[0]
            spec = {
                "type": "render",
                "input": {"path": server_path, "sha256": digest},
                "out_dir": str(self._server_job_dir(job_id) / "out"),
                "dpi": dpi,
                "format": "png",
            }
            job = self._execute(ctx, spec, [source])
            files = self._verify_files(ctx, job)
            pages: list[tuple[int, Path]] = []
            for name, local in files.items():
                match = _PAGE_RE.match(name)
                if match:
                    pages.append((int(match.group(1)), local))
            if not pages:
                raise CallasResultError("Callas не вернул ни одной PNG-страницы.", job_id=job_id)
            pages.sort(key=lambda item: item[0])
            numbers = [number for number, _ in pages]
            if numbers != list(range(1, len(numbers) + 1)):
                raise CallasResultError(
                    "Callas вернул страницы с пропусками в нумерации.", job_id=job_id
                )
            if expected_pages is not None and len(pages) != expected_pages:
                raise CallasResultError(
                    f"Callas вернул {len(pages)} стр., ожидалось {expected_pages}.",
                    job_id=job_id,
                )
            return [path for _, path in pages]

    def process(
        self,
        pdf: Path | str,
        profile: str,
        mode: str,
        *,
        variables: Mapping[str, str] | None = None,
        report: Sequence[str] = ("xml",),
        cancel: threading.Event | None = None,
        job_id: str | None = None,
        keep_name: bool = False,
    ) -> ProcessResult:
        """Запускает процесс-план pdfToolbox: ``analyze`` (только проверка) или ``fix``.

        Исходный PDF только читается: служба получает его копию в папке задания, а
        результат ``fix`` — всегда новый файл в ``<share>/.callas/<job_id>/out``.
        Коды завершения 1–8 (найдены проблемы / выполнены исправления) не являются
        ошибкой; 9 и выше (в том числе 100+) — ошибка выполнения.
        ``job_id`` по умолчанию детерминирован (PDF, профиль, режим, переменные); для
        повторного запуска с теми же параметрами после завершения задания на службе
        передайте новый ``job_id``.
        """
        if mode not in _PROCESS_MODES:
            raise CallasConfigError("Режим callas должен быть «analyze» или «fix».")
        profile_name = self._validate_profile(profile)
        report_kinds = self._validate_reports(report)
        variable_map = self._validate_variables(variables)
        source = Path(pdf)
        if not source.is_file():
            raise CallasConfigError(f"Входной файл не найден: {source.name}")
        try:
            before = source.stat()
            source_digest = _sha256_file(source)
        except OSError as exc:
            raise CallasConfigError(
                f"Не удалось прочитать {source.name}: {exc.strerror or exc}"
            ) from exc
        if job_id is None:
            job_id = deterministic_job_id(
                f"callas-{mode}",
                mode,
                profile_name,
                source_digest,
                json.dumps(variable_map, sort_keys=True, ensure_ascii=False),
                ",".join(report_kinds),
            )
        self._validate_job_id(job_id)
        ctx = self._new_ctx(job_id, cancel)
        with self._slot(ctx):
            staged = self._stage_inputs(job_id, [source], keep_names=keep_name)
            server_path, digest = staged[0]
            spec: dict[str, Any] = {
                "type": "process",
                "input": {"path": server_path, "sha256": digest},
                "profile": profile_name,
                "mode": mode,
                "report": list(report_kinds),
                "out_dir": str(self._server_job_dir(job_id) / "out"),
            }
            if variable_map:
                spec["variables"] = variable_map
            if mode == "fix":
                spec["output_name"] = _PROCESS_FIXED_NAME
            try:
                job = self._execute(ctx, spec, [source])
            except CallasValidationError as exc:
                if "profile" in exc.message.lower():
                    raise CallasValidationError(
                        f"Callas не принял профиль «{profile_name}»: {exc.message} "
                        "Проверьте имя профиля в каталоге профилей сервера.",
                        job_id=job_id,
                    ) from exc
                raise
            except CallasJobFailedError as exc:
                hint = self._unknown_profile_hint(exc, profile_name)
                if hint is None:
                    raise
                raise CallasJobFailedError(
                    hint,
                    job_id=job_id,
                    status=exc.status,
                    error_class=exc.error_class,
                    bad_inputs=exc.bad_inputs,
                ) from exc
            exit_code = self._process_exit_code(ctx, job)
            summary = self._process_summary(ctx, job)
            files = self._verify_files(ctx, job)
            output_pdf = self._process_output_pdf(ctx, files, mode)
            report_xml = self._process_report_xml(ctx, files, "xml" in report_kinds)
            try:
                after = source.stat()
            except OSError as exc:
                raise CallasResultError(
                    f"Исходный файл {source.name} стал недоступен во время обработки.",
                    job_id=job_id,
                ) from exc
            if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                raise CallasResultError(
                    f"Исходный файл {source.name} изменился во время обработки callas; "
                    "результат отклонён.",
                    job_id=job_id,
                )
            if output_pdf is not None and output_pdf.resolve() == source.resolve():
                raise CallasResultError(
                    "Callas вернул исходный файл вместо нового; результат отклонён.",
                    job_id=job_id,
                )
            return ProcessResult(
                job_id=job_id,
                mode=mode,
                status=self._process_status(job, summary),
                exit_code=exit_code,
                summary=summary,
                output_pdf=output_pdf,
                report_xml=report_xml,
            )

    def ink_coverage(
        self,
        pdf: Path | str,
        *,
        pages: str | None = None,
        resolution: int = 72,
        box: str = "TrimBox",
        job_id: str | None = None,
        cancel: threading.Event | None = None,
    ) -> InkCoverageResult:
        """Красочность (среднее покрытие красками по страницам) заданием ``inkcoverage``.

        Вход только читается; выходных файлов у задания нет — результат берётся из
        блока ``ink_coverage`` ответа службы. ``percent`` в отчёте — среднее покрытие
        по рамке ``box``, не TAC (см. ``services.callas.ink_coverage``).
        """
        if not isinstance(resolution, int) or isinstance(resolution, bool) or not 1 <= resolution <= 1200:
            raise CallasConfigError("Разрешение расчёта красочности должно быть целым числом от 1 до 1200 ppi.")
        box_name = next((item for item in INK_COVERAGE_BOXES if item.lower() == str(box).lower()), None)
        if box_name is None:
            raise CallasConfigError(
                "Рамка расчёта красочности должна быть одной из: " + ", ".join(INK_COVERAGE_BOXES) + "."
            )
        if pages is not None and (not isinstance(pages, str) or not re.fullmatch(r"[0-9,\- ]+", pages)):
            raise CallasConfigError("Диапазон страниц красочности задаётся строкой вида «1-3,5».")
        source = Path(pdf)
        if not source.is_file():
            raise CallasConfigError(f"Входной файл не найден: {source.name}")
        if job_id is None:
            try:
                digest = _sha256_file(source)
            except OSError as exc:
                raise CallasConfigError(
                    f"Не удалось прочитать {source.name}: {exc.strerror or exc}"
                ) from exc
            job_id = deterministic_job_id(
                "callas-ink", digest, pages or "", str(resolution), box_name
            )
        self._validate_job_id(job_id)
        ctx = self._new_ctx(job_id, cancel)
        with self._slot(ctx):
            staged = self._stage_inputs(job_id, [source])
            server_path, digest = staged[0]
            spec: dict[str, Any] = {
                "type": "inkcoverage",
                "input": {"path": server_path, "sha256": digest},
                "resolution": resolution,
                "box": box_name,
            }
            if pages is not None:
                spec["pages"] = pages
            job = self._execute(ctx, spec, [source])
            block = job.get("ink_coverage")
            if not isinstance(block, dict):
                raise CallasProtocolError(
                    "Callas не вернул блок ink_coverage в ответе задания.", job_id=job_id
                )
            return parse_ink_coverage(block, job_id=job_id)

    def delete(self, job_id: str, *, cancel: threading.Event | None = None) -> None:
        """Удаляет/отменяет задание на стороне службы; 404 не считается ошибкой."""
        self._validate_job_id(job_id)
        ctx = self._new_ctx(job_id, cancel)
        try:
            self._request(ctx, "DELETE", f"/jobs/{job_id}")
        except CallasNotFoundError:
            return

    def job_dir(self, job_id: str) -> Path:
        """Локальная папка задания (для очистки вызывающим кодом)."""
        self._validate_job_id(job_id)
        return self._share / ".callas" / job_id

    # ------------------------------------------------------------ internals

    def _server_job_dir(self, job_id: str) -> PurePosixPath:
        return self._server_share / ".callas" / job_id

    @staticmethod
    def _validate_job_id(job_id: str) -> None:
        if not isinstance(job_id, str) or not _JOB_ID_RE.match(job_id) or ".." in job_id:
            raise CallasConfigError(
                "Некорректный идентификатор задания callas: допустимы латиница, цифры, '.', '_', '-'."
            )

    def _new_ctx(self, job_id: str, cancel: threading.Event | None) -> _Ctx:
        return _Ctx(
            job_id=job_id,
            deadline=self._clock() + self._cfg.job_timeout_seconds,
            cancel=cancel or threading.Event(),
        )

    def _remaining(self, ctx: _Ctx) -> float:
        return ctx.deadline - self._clock()

    def _check(self, ctx: _Ctx) -> None:
        if ctx.cancel.is_set():
            self._abandon(ctx)
            raise CallasCancelledError("Операция callas отменена.", job_id=ctx.job_id)
        if self._remaining(ctx) <= 0:
            self._abandon(ctx)
            raise CallasTimeoutError(
                f"Callas не завершил задание за {self._cfg.job_timeout_seconds:g} с.",
                job_id=ctx.job_id,
            )

    def _wait(self, ctx: _Ctx, seconds: float) -> None:
        """Отменяемое ожидание, ограниченное общим deadline."""
        ctx.cancel.wait(max(0.0, min(seconds, self._remaining(ctx))))
        self._check(ctx)

    def _abandon(self, ctx: _Ctx) -> None:
        """Лучшая попытка отменить удалённое задание при отмене/таймауте."""
        if not ctx.put_sent:
            return
        ctx.put_sent = False
        try:
            self._http.request(
                "DELETE",
                f"{self._base}/jobs/{ctx.job_id}",
                headers=self._headers(),
                timeout=5.0,
            )
        except Exception:  # noqa: BLE001 - best effort, ошибка не должна скрыть причину
            logger.warning("callas: не удалось отменить задание %s", ctx.job_id)

    class _SlotGuard:
        def __init__(self, owner: "CallasClient", ctx: _Ctx) -> None:
            self._owner = owner
            self._ctx = ctx

        def __enter__(self) -> None:
            owner, ctx = self._owner, self._ctx
            while not owner._slots.acquire(timeout=0.05):
                owner._check(ctx)
            try:
                owner._check(ctx)
            except BaseException:
                owner._slots.release()
                raise

        def __exit__(self, *exc: object) -> None:
            self._owner._slots.release()

    def _slot(self, ctx: _Ctx) -> "_SlotGuard":
        return self._SlotGuard(self, ctx)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._cfg.token}",
            "X-Callas-User": self._cfg.user,
            "Accept": "application/json",
        }

    def _redact(self, text: str) -> str:
        text = text.replace(self._cfg.token, "***")
        text = re.sub(r"(?i)bearer\s+\S+", "Bearer ***", text)
        text = " ".join(text.split())
        if len(text) > _MAX_ERROR_TEXT:
            text = text[:_MAX_ERROR_TEXT] + "…"
        return text

    # -- входы

    def _stage_inputs(
        self, job_id: str, sources: Sequence[Path], *, keep_names: bool = False
    ) -> list[tuple[str, str]]:
        """Копирует входы в <share>/.callas/<job_id>/in; возвращает (путь для сервера, sha256).

        По умолчанию вход получает имя ``0001.pdf``. С ``keep_names`` сохраняется имя
        исходного файла (в безопасной записи): процесс-планы, которые читают целевой
        размер из имени заказа вида ``…(99x210)…``, иначе его не увидят.
        """
        in_dir = self.job_dir(job_id) / "in"
        try:
            in_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CallasConfigError(
                f"Общая папка callas недоступна ({self._share}): {exc.strerror or exc}",
                job_id=job_id,
            ) from exc
        staged: list[tuple[str, str]] = []
        for index, source in enumerate(sources, start=1):
            if not source.is_file():
                raise CallasConfigError(f"Входной файл не найден: {source.name}", job_id=job_id)
            suffix = source.suffix.lower() if re.fullmatch(r"\.[A-Za-z0-9]{1,8}", source.suffix) else ""
            target = in_dir / (
                _safe_input_name(source.name) if keep_names and len(sources) == 1 else f"{index:04d}{suffix}"
            )
            try:
                digest = self._copy_verified(source, target)
            except OSError as exc:
                raise CallasConfigError(
                    f"Не удалось скопировать {source.name} в общую папку callas: {exc.strerror or exc}",
                    job_id=job_id,
                ) from exc
            server_path = str(self._server_job_dir(job_id) / "in" / target.name)
            staged.append((server_path, digest))
        return staged

    def _copy_verified(self, source: Path, target: Path) -> str:
        digest = _sha256_file(source)
        size = source.stat().st_size
        if target.is_file() and target.stat().st_size == size and _sha256_file(target) == digest:
            return digest
        part = target.with_name(target.name + ".part")
        try:
            shutil.copyfile(source, part)
            if part.stat().st_size != size or _sha256_file(part) != digest:
                raise OSError("копия на общей папке не совпала с исходником")
            os.replace(part, target)
        finally:
            if part.exists():
                try:
                    part.unlink()
                except OSError:
                    pass
        return digest

    # -- HTTP

    def _execute(self, ctx: _Ctx, spec: Mapping[str, Any], sources: Sequence[Path]) -> dict[str, Any]:
        job_id = ctx.job_id
        logger.info("callas: задание %s (%s) отправляется", job_id, spec["type"])
        self._request(ctx, "PUT", f"/jobs/{job_id}", json_body=spec, adopt_conflict=True)
        interval = self._cfg.poll_interval_seconds
        while True:
            job = self._get_job(ctx)
            status = str(job.get("status", "")).strip().lower()
            if status in _DONE_STATUSES:
                logger.info("callas: задание %s выполнено", job_id)
                bad = job.get("bad_inputs") or []
                if bad:
                    raise self._failed(ctx, job, "done", sources)
                return job
            if status in _FAILED_STATUSES or status in _CANCELLED_STATUSES:
                raise self._failed(ctx, job, status, sources)
            self._wait(ctx, interval)
            interval = min(interval * 1.5, self._cfg.poll_max_interval_seconds)

    def _failed(
        self, ctx: _Ctx, job: Mapping[str, Any], status: str, sources: Sequence[Path]
    ) -> CallasJobFailedError:
        raw_bad = job.get("bad_inputs")
        bad: list[dict[str, Any]] = [item for item in raw_bad if isinstance(item, dict)] if isinstance(raw_bad, list) else []
        details: list[str] = []
        for item in bad:
            index = item.get("index")
            name = ""
            if isinstance(index, int) and 0 <= index < len(sources):
                name = sources[index].name
            reason = self._redact(str(item.get("reason", "причина не указана")))
            details.append(f"{name or 'вход №' + str(index)}: {reason}")
        error_text = self._redact(str(job.get("error") or "")) if job.get("error") else ""
        if status in _CANCELLED_STATUSES:
            head = "Callas отменил задание."
        elif status == "done":
            head = "Callas пропустил часть входных файлов; страницы терять нельзя."
        else:
            head = "Callas не смог выполнить задание."
        message = " ".join(part for part in (head, error_text, "; ".join(details)) if part)
        error_class = job.get("error_class")
        return CallasJobFailedError(
            message,
            job_id=ctx.job_id,
            status=status,
            error_class=error_class if isinstance(error_class, str) else None,
            bad_inputs=bad,
        )

    def _get_job(self, ctx: _Ctx) -> dict[str, Any]:
        response = self._request(ctx, "GET", f"/jobs/{ctx.job_id}")
        return self._json_object(ctx, response)

    def _json_object(self, ctx: _Ctx, response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise CallasProtocolError(
                "Callas вернул некорректный JSON в ответе.", job_id=ctx.job_id
            ) from exc
        if not isinstance(data, dict):
            raise CallasProtocolError(
                "Callas вернул неожиданную структуру ответа.", job_id=ctx.job_id
            )
        return data

    def _request(
        self,
        ctx: _Ctx,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        adopt_conflict: bool = False,
    ) -> httpx.Response:
        attempt = 0
        ambiguous = False
        while True:
            self._check(ctx)
            timeout = max(0.1, min(self._cfg.request_timeout_seconds, self._remaining(ctx)))
            retry_after: float | None = None
            if method == "PUT":
                ctx.put_sent = True
            try:
                response = self._http.request(
                    method,
                    f"{self._base}{path}",
                    json=json_body,
                    headers=self._headers(),
                    timeout=timeout,
                )
            except httpx.TransportError as exc:
                ambiguous = True
                reason = f"сетевая ошибка ({type(exc).__name__})"
            else:
                code = response.status_code
                if 200 <= code < 300:
                    return response
                if code == 409 and adopt_conflict and ambiguous:
                    logger.info("callas: задание %s уже принято службой", ctx.job_id)
                    return response
                if code not in _RETRY_STATUSES:
                    raise self._map_error(ctx, method, response)
                if code in _AMBIGUOUS_STATUSES:
                    ambiguous = True
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                reason = f"HTTP {code}"
            attempt += 1
            if attempt > self._cfg.max_retries:
                raise CallasUnavailableError(
                    f"Callas недоступен ({reason}); повторы исчерпаны.", job_id=ctx.job_id
                )
            if retry_after is not None:
                delay = min(retry_after, _MAX_RETRY_AFTER_SECONDS)
                if delay >= self._remaining(ctx):
                    self._abandon(ctx)
                    raise CallasTimeoutError(
                        "Callas просит подождать дольше общего срока выполнения задания.",
                        job_id=ctx.job_id,
                    )
            else:
                base = min(
                    self._cfg.backoff_max_seconds,
                    self._cfg.backoff_base_seconds * (2 ** (attempt - 1)),
                )
                delay = base * (0.75 + 0.5 * self._rng())
            logger.warning(
                "callas: %s %s: %s, повтор %d/%d через %.1f с",
                method,
                "/jobs/<id>",
                reason,
                attempt,
                self._cfg.max_retries,
                delay,
            )
            self._wait(ctx, delay)

    def _map_error(self, ctx: _Ctx, method: str, response: httpx.Response) -> CallasError:
        code = response.status_code
        job_id = ctx.job_id
        if code in (401, 403):
            return CallasAuthError(
                f"Callas отклонил доступ (HTTP {code}): проверьте IMAGE_MAGIC_CALLAS_TOKEN.",
                job_id=job_id,
            )
        if code == 409:
            return CallasConflictError(
                "Задание с таким идентификатором уже существует в callas.", job_id=job_id
            )
        if code == 404:
            return CallasNotFoundError("Задание не найдено в callas.", job_id=job_id)
        if code == 422:
            return CallasValidationError(
                "Callas отклонил описание задания: " + self._validation_text(response),
                job_id=job_id,
            )
        detail = self._validation_text(response)
        suffix = "" if detail == "подробности недоступны." else f": {detail}"
        return CallasError(f"Callas вернул ошибку HTTP {code}{suffix}", job_id=job_id)

    def _validation_text(self, response: httpx.Response) -> str:
        try:
            detail = response.json().get("detail")
        except (ValueError, AttributeError):
            return "подробности недоступны."
        if isinstance(detail, str):
            return self._redact(detail)
        if isinstance(detail, list):
            parts = []
            for item in detail[:5]:
                if isinstance(item, dict):
                    loc = ".".join(str(x) for x in item.get("loc", []) if x != "body")
                    parts.append(self._redact(f"{loc}: {item.get('msg', '')}".strip(": ")))
            if parts:
                return "; ".join(parts)
        return "подробности недоступны."

    # -- process

    @staticmethod
    def _validate_profile(profile: str) -> str:
        if not isinstance(profile, str):
            raise CallasConfigError("Имя профиля callas должно быть строкой.")
        name = profile.strip()
        parts = name.split("/")
        if (
            not name
            or len(name) > 300
            or "\x00" in name
            or "\\" in name
            or name.startswith("/")
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise CallasConfigError(
                "Некорректное имя профиля callas: нужно имя из каталога профилей сервера "
                "(до 300 символов, без путей и '..')."
            )
        return name

    @staticmethod
    def _validate_reports(report: Sequence[str]) -> tuple[str, ...]:
        if isinstance(report, str):
            report = (report,)
        kinds: list[str] = []
        for item in report:
            if item not in _PROCESS_REPORTS:
                raise CallasConfigError("Тип отчёта callas должен быть «xml» или «pdf».")
            if item not in kinds:
                kinds.append(item)
        if not kinds:
            raise CallasConfigError("Нужен хотя бы один тип отчёта callas («xml» или «pdf»).")
        return tuple(kinds)

    @staticmethod
    def _validate_variables(variables: Mapping[str, str] | None) -> dict[str, str]:
        if not variables:
            return {}
        if len(variables) > 50:
            raise CallasConfigError("Переменных профиля callas может быть не больше 50.")
        result: dict[str, str] = {}
        for key, value in variables.items():
            if not isinstance(key, str) or not key or not isinstance(value, str):
                raise CallasConfigError(
                    "Переменные профиля callas должны быть парами «строка: строка»."
                )
            result[key] = value
        return result

    @staticmethod
    def _unknown_profile_hint(exc: CallasJobFailedError, profile: str) -> str | None:
        text = f"{exc.error_class or ''} {exc.message}".lower()
        if "profile" in text or "профил" in text:
            if any(
                word in text
                for word in ("not found", "unknown", "no such", "не найден", "не существует")
            ):
                return (
                    f"Профиль callas «{profile}» не найден на сервере. "
                    "Проверьте имя в каталоге профилей (GET /profiles)."
                )
        return None

    def _process_exit_code(self, ctx: _Ctx, job: Mapping[str, Any]) -> int:
        code = job.get("exit_code")
        if not isinstance(code, int) or isinstance(code, bool) or code < 0:
            raise CallasProtocolError(
                "Callas не вернул корректный код завершения проверки.", job_id=ctx.job_id
            )
        if code > _EXIT_OK_MAX:
            detail = self._redact(str(job.get("error") or ""))
            error_class = job.get("error_class")
            raise CallasJobFailedError(
                f"Callas завершил проверку с ошибкой выполнения (код {code})."
                + (f" {detail}" if detail else ""),
                job_id=ctx.job_id,
                status="failed",
                error_class=error_class if isinstance(error_class, str) else None,
            )
        return code

    def _process_summary(self, ctx: _Ctx, job: Mapping[str, Any]) -> dict[str, int]:
        raw = job.get("summary")
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise CallasProtocolError("Callas вернул некорректную сводку проверки.", job_id=ctx.job_id)
        summary: dict[str, int] = {}
        for key in _PROCESS_SUMMARY_KEYS:
            value = raw.get(key, 0)
            if value is None:
                value = 0
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise CallasProtocolError(
                    "Callas вернул некорректную сводку проверки.", job_id=ctx.job_id
                )
            summary[key] = value
        return summary

    @staticmethod
    def _process_status(job: Mapping[str, Any], summary: Mapping[str, int]) -> str:
        status = job.get("result_status")
        if isinstance(status, str) and status.strip():
            return status.strip().lower()
        if summary["errors"]:
            return "errors"
        if summary["warnings"]:
            return "warnings"
        return "ok"

    @staticmethod
    def _process_output_pdf(ctx: _Ctx, files: Mapping[str, Path], mode: str) -> Path | None:
        if mode != "fix":
            return None
        direct = files.get(_PROCESS_FIXED_NAME)
        if direct is not None:
            return direct
        candidates = [
            path
            for name, path in files.items()
            if name.lower().endswith(".pdf") and "report" not in name.lower()
        ]
        if len(candidates) != 1:
            raise CallasResultError(
                "Callas не вернул исправленный PDF: результат исправления отсутствует. "
                "Исходный файл не изменён.",
                job_id=ctx.job_id,
            )
        return candidates[0]

    @staticmethod
    def _process_report_xml(ctx: _Ctx, files: Mapping[str, Path], required: bool) -> Path | None:
        candidates = sorted(
            (name for name in files if name.lower().endswith(".xml")),
            key=lambda name: ("report" not in name.lower(), name),
        )
        if not candidates:
            if required:
                raise CallasResultError("Callas не вернул XML-отчёт проверки.", job_id=ctx.job_id)
            return None
        return files[candidates[0]]

    # -- результат

    def _verify_files(self, ctx: _Ctx, job: Mapping[str, Any]) -> dict[str, Path]:
        """Проверяет, что каждый файл лежит строго в out/ задания, и сверяет размер и SHA-256."""
        job_id = ctx.job_id
        raw = job.get("files")
        if not isinstance(raw, list) or not raw:
            raise CallasResultError("Callas завершил задание без файлов результата.", job_id=job_id)
        server_out = self._server_job_dir(job_id) / "out"
        local_out = self.job_dir(job_id) / "out"
        result: dict[str, Path] = {}
        for item in raw:
            if not isinstance(item, dict):
                raise CallasProtocolError("Некорректное описание файла в ответе callas.", job_id=job_id)
            name, path, size, sha = (item.get(k) for k in ("name", "path", "size", "sha256"))
            if (
                not isinstance(name, str)
                or not isinstance(path, str)
                or not isinstance(size, int)
                or isinstance(size, bool)
                or not isinstance(sha, str)
                or not re.fullmatch(r"[0-9a-fA-F]{64}", sha)
            ):
                raise CallasProtocolError("Некорректное описание файла в ответе callas.", job_id=job_id)
            if name in {"", ".", ".."} or "/" in name or "\\" in name or "\x00" in name:
                raise CallasResultError("Callas вернул недопустимое имя файла.", job_id=job_id)
            reported = PurePosixPath(path)
            if ".." in reported.parts or reported != server_out / name:
                raise CallasResultError(
                    "Путь результата callas вне папки задания; файл отклонён.", job_id=job_id
                )
            if name in result:
                raise CallasResultError("Callas вернул файл дважды.", job_id=job_id)
            local = local_out / name
            self._wait_visible(ctx, local)
            self._check_local(ctx, local, local_out, size, sha.lower())
            result[name] = local
        return result

    def _wait_visible(self, ctx: _Ctx, local: Path) -> None:
        """SMB-кэш может показать файл с задержкой: ждём ограниченное время."""
        limit = self._clock() + self._cfg.file_visible_timeout_seconds
        while not local.is_file():
            if self._clock() >= limit:
                raise CallasResultError(
                    f"Файл результата {local.name} не появился в общей папке.",
                    job_id=ctx.job_id,
                )
            self._wait(ctx, 0.25)

    def _check_local(self, ctx: _Ctx, local: Path, local_out: Path, size: int, sha: str) -> None:
        job_id = ctx.job_id
        try:
            if local.is_symlink() or local.resolve(strict=True).parent != local_out.resolve(strict=True):
                raise CallasResultError(
                    "Файл результата callas выходит за пределы папки задания.", job_id=job_id
                )
            actual_size = local.stat().st_size
            if actual_size != size:
                raise CallasResultError(
                    f"Размер {local.name} не совпал с ответом callas ({actual_size} вместо {size}).",
                    job_id=job_id,
                )
            if _sha256_file(local) != sha:
                raise CallasResultError(
                    f"Контрольная сумма {local.name} не совпала с ответом callas.", job_id=job_id
                )
        except OSError as exc:
            raise CallasResultError(
                f"Не удалось прочитать результат {local.name}: {exc.strerror or exc}",
                job_id=job_id,
            ) from exc


def _safe_input_name(name: str) -> str:
    """Имя файла только из ASCII-букв, цифр и ``._()- ``: остальное заменяется на ``_``."""
    stem, dot, suffix = name.rpartition(".")
    if not dot or not re.fullmatch(r"[A-Za-z0-9]{1,8}", suffix):
        stem, suffix = name, "pdf"
    # Кириллическая «х» и «×» внутри «(99х210)» читаются планами как разделитель размера.
    stem = re.sub(r"(\d)\s*[хХ×]\s*(\d)", r"\1x\2", stem)
    cleaned = re.sub(r"[^A-Za-z0-9._()\- ]", "_", stem).strip(" .") or "input"
    return f"{cleaned[:150]}.{suffix.lower()}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_retry_after(value: str | None) -> float | None:
    """Секунды или HTTP-дата; некорректное значение игнорируется (None)."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    try:
        moment = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (moment - datetime.now(timezone.utc)).total_seconds())
