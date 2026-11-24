"""Заказы, отменённые в Sborka: распознавание по ``orderinfo`` и перенос исходников.

Отменённый клиентом заказ приходит из ``orderinfo`` со ``status`` = ``"14"`` (сверено
24.11.2026: у всех таких заказов в ``comment_client`` есть «отменен клиентом», у
заказов в других статусах — нет; заказ 26255491). Такой заказ не проверяется: его
исходники уходят из входной папки в ``Cancelled/<заказ>/``, в Sborka ничего не
отправляется. Восстановленный в Sborka заказ получает другой статус и проверяется
обычным образом, поэтому решает именно статус, а не текст комментария.
"""

from __future__ import annotations

import logging
from pathlib import Path
import shutil
from typing import Any, Mapping, Sequence

from processing.models import OrderCheck

logger = logging.getLogger("image_magic.sborka_cancel")

SBORKA_CANCELLED_STATUSES = frozenset({"14"})
CANCELLED_FOLDER = "Cancelled"


def cancelled_orders(
    records_by_id: Mapping[str, Mapping[str, Any]], order_ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """``{номер: {"sborka_status": ...}}`` для заказов *order_ids*, отменённых в Sborka."""
    result: dict[str, dict[str, Any]] = {}
    for order_id in order_ids:
        record = records_by_id.get(str(order_id))
        if record is None:
            continue
        status = str(record.get("status") or "").strip()
        if status in SBORKA_CANCELLED_STATUSES:
            result[str(order_id)] = {"sborka_status": status}
    return result


def move_cancelled_order(order: OrderCheck, input_dir: Path) -> None:
    """Перенести исходники отменённого заказа в ``Cancelled/<заказ>/``.

    Файлы не перезаписываются: при совпадении имени добавляется суффикс ``(2)``,
    ``(3)``… Результат пишется в ``order.sborka_cancelled`` (``moved_to``, при сбое
    ``move_error``); сбой переноса не превращается в исключение — файлы остаются на
    месте и попадут в следующую проверку, где заказ снова будет отброшен.
    """
    info = dict(order.sborka_cancelled or {})
    target_dir = Path(input_dir) / CANCELLED_FOLDER / order.order_id
    moved: list[str] = []
    failures: list[str] = []
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        failures.append(f"{target_dir}: {exc}")
    else:
        for item in order.files:
            source = Path(item.path)
            if not source.exists():
                continue
            try:
                target = _free_name(target_dir / source.name)
                shutil.move(str(source), str(target))
                moved.append(target.name)
            except OSError as exc:
                failures.append(f"{source.name}: {exc}")
    info["moved_to"] = str(target_dir)
    info["moved_files"] = moved
    if failures:
        info["move_error"] = "; ".join(failures)
        logger.warning(
            "order.cancelled_move_failed order_id=%s error=%s", order.order_id, info["move_error"]
        )
    else:
        logger.info(
            "order.cancelled_moved order_id=%s files=%d target=%s",
            order.order_id, len(moved), target_dir,
        )
    order.sborka_cancelled = info


def _free_name(path: Path) -> Path:
    if not path.exists():
        return path
    for number in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({number}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise OSError(f"нет свободного имени для {path.name}")
