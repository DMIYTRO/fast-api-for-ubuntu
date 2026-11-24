"""Итог последнего действия оператора над заказом для карточки (``order["action_result"]``).

Карточка показывает результат «В печать»/«Вернуть» сразу после нажатия (ответ API) и
после перечитывания заказов — из сохранённого ``order_actions``. Чтобы после
перечитывания было видно то же самое, неудача хранит свой итог в
``cms_response_json["result"]`` (``OrderWorkflowService._store_action_failure``):
статус ответа (``review_required``, ``error``, ``rejected``, ``conflict``), код,
сообщение и созданные из слоёв TIFF файлы. Старые записи без него получают общее
сообщение вместо голого ``failed``.
"""

from __future__ import annotations

from typing import Any, Mapping

UNKNOWN_FAILURE_MESSAGE = (
    "Не удалось выполнить действие; причина не сохранена — повторите или посмотрите журнал."
)
RESTART_FAILURE_MESSAGE = (
    "Действие прервано перезапуском приложения — проверьте заказ и повторите."
)


def stored_action_result(order_id: str, action: Mapping[str, Any]) -> dict[str, Any]:
    """``action`` — элемент ``order["actions"]`` (см. ``SqlRunRepository``)."""
    result = action.get("result") if isinstance(action.get("result"), Mapping) else {}
    stored_status = action.get("status")
    status = result.get("status") or stored_status
    message = str(result.get("message") or "")
    if not message and stored_status == "failed":
        message = (
            RESTART_FAILURE_MESSAGE
            if action.get("reason") == "application_restart"
            else UNKNOWN_FAILURE_MESSAGE
        )
    value: dict[str, Any] = {
        "order_id": order_id,
        "status": status,
        "action": action.get("action"),
    }
    if message:
        value["message"] = message
    if result.get("code"):
        value["code"] = result["code"]
    if result.get("generated_layered_file_ids"):
        value["generated_layered_file_ids"] = list(result["generated_layered_file_ids"])
    if action.get("pitstop"):
        value["pitstop"] = action["pitstop"]
    return value
