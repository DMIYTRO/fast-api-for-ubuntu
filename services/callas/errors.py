"""Ошибки клиента callas. Тексты рассчитаны на оператора и не содержат секретов."""

from __future__ import annotations

from typing import Any


class CallasError(Exception):
    """Базовая ошибка интеграции с callas."""

    def __init__(self, message: str, *, job_id: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.job_id = job_id


class CallasConfigError(CallasError):
    """Интеграция не настроена или параметры запроса некорректны."""


class CallasAuthError(CallasError):
    """HTTP 401/403: токен отсутствует, неверен или не имеет прав."""


class CallasConflictError(CallasError):
    """HTTP 409: задание с таким идентификатором уже существует."""


class CallasValidationError(CallasError):
    """HTTP 422: служба отклонила описание задания."""


class CallasNotFoundError(CallasError):
    """HTTP 404: задание не найдено."""


class CallasUnavailableError(CallasError):
    """Служба недоступна (429/503/сеть) и повторы исчерпаны."""


class CallasTimeoutError(CallasError):
    """Общий срок выполнения задания истёк."""


class CallasCancelledError(CallasError):
    """Операция отменена приложением (threading.Event)."""


class CallasProtocolError(CallasError):
    """Ответ службы нельзя разобрать или он не соответствует контракту."""


class CallasReportError(CallasProtocolError):
    """XML-отчёт pdfToolbox нельзя прочитать или он небезопасен."""


class CallasJobFailedError(CallasError):
    """Служба завершила задание статусом failed/cancelled."""

    def __init__(
        self,
        message: str,
        *,
        job_id: str | None = None,
        status: str = "failed",
        error_class: str | None = None,
        bad_inputs: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message, job_id=job_id)
        self.status = status
        self.error_class = error_class
        self.bad_inputs = bad_inputs or []


class CallasResultError(CallasError):
    """Результат задания не прошёл проверку пути, размера или SHA-256."""
