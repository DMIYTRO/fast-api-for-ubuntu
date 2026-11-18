"""Комплекты заказов Sborka (``orderinfo`` → ``sets_array``).

Комплект — главный заказ и его подзаказы. ``orderinfo`` отдаёт комплект только в записи
главного заказа: ``{"id": main, "sets_array": [{"id": ...}, ...]}``; запрос одного
подзаказа возвращает пустой список (проверено 2026-11-18 на комплекте 26234881).

Возврат (``touser``) главного заказа возвращает в Sborka ВЕСЬ комплект — с одним
комментарием (Sborka копирует его во все подзаказы) и превью главного. Возврат подзаказа
Sborka не подтверждает (пустой ответ). Поэтому возврат комплекта в Image Magic — одна
операция: в Sborka уходит только главный заказ, остальные заказы комплекта
возвращаются локально (файлы в Troubles) без запроса в Sborka.
"""

from __future__ import annotations

import logging
import re
from collections import OrderedDict
from pathlib import PurePath
from threading import Lock
from typing import Any, Callable, Iterable, Mapping, Sequence

logger = logging.getLogger("image_magic.order_kits")

#: Сколько запусков держать в кэше состава комплектов.
_CACHE_RUNS = 32
#: Номер заказа и позиция в комплекте в имени файла: «…(36056-26234883)_3.pdf» — 3-й заказ.
_KIT_POSITION_RE = re.compile(r"\((\d+)-(\d+)\)_(\d+)(?=$|[_.\-])")


def candidate_main_ids(order: Mapping[str, Any]) -> set[str]:
    """Предполагаемый главный заказ комплекта по имени файла (номера заказов комплекта подряд).

    «…(36056-26234883)_3» → 26234881. Это только кандидат: комплект засчитывается, если
    Sborka в ``orderinfo`` кандидата перечислит этот заказ в ``sets_array``.
    """
    order_id = str(order.get("order_id") or "")
    if not order_id.isdigit():
        return set()
    candidates: set[str] = set()
    for file in order.get("files") or []:
        name = str(file.get("name") or PurePath(str(file.get("path") or "")).name)
        for match in _KIT_POSITION_RE.finditer(name):
            position = int(match.group(3))
            if match.group(2) == order_id and 2 <= position <= 999 and int(order_id) >= position:
                candidates.add(str(int(order_id) - (position - 1)))
    return candidates


def kits_from_order_info(records: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """``{номер заказа: комплект}`` по записям ``orderinfo``; заказы вне комплектов не входят.

    Комплект: ``main_id``, ``member_ids`` (главный первым), ``position`` (1 — главный),
    ``size``.
    """
    kits: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, Mapping) or record.get("id") is None:
            continue
        sets = [
            item for item in record.get("sets_array") or []
            if isinstance(item, Mapping) and item.get("id") is not None
        ]
        if not sets:
            continue
        main_id = str(record["id"])
        members = [main_id, *(str(item["id"]) for item in sets)]
        for position, member in enumerate(members, start=1):
            kits[member] = {
                "main_id": main_id,
                "member_ids": list(members),
                "position": position,
                "size": len(members),
            }
    return kits


def run_orders(run: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Заказы запуска (словарь по aggregate_id или список)."""
    orders = run.get("orders") or {}
    return list(orders.values()) if isinstance(orders, Mapping) else list(orders)


class KitResolver:
    """Состав комплектов для заказов запуска.

    Сначала — сохранённый при проверке ``order["kit"]``; для старых запусков (или если
    ``orderinfo`` при проверке не ответил) — один запрос ``orderinfo`` по всем заказам
    запуска, результат кэшируется на запуск. Ошибка Sborka не кэшируется и даёт ``{}``:
    заказы обрабатываются как обычные.
    """

    def __init__(self, fetcher: Callable[[Sequence[str]], list[dict[str, Any]]] | None) -> None:
        self.fetcher = fetcher
        self._guard = Lock()
        # {run_id: {"looked_up": номера, уже спрошенные у Sborka, "kits": найденное}}
        self._cache: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def for_run(self, run: Mapping[str, Any], *, live: bool = True) -> dict[str, dict[str, Any]]:
        """``{номер заказа: комплект}`` для заказов запуска.

        Сохранённые при проверке комплекты дополняются живым ``orderinfo`` для остальных
        заказов (главного может не быть в проверке); живой результат кэшируется на запуск.
        ``live=False`` — без запросов в Sborka (только сохранённое и кэш).
        """
        orders = run_orders(run)
        kits = {
            str(order.get("order_id")): dict(order["kit"])
            for order in orders
            if isinstance(order.get("kit"), Mapping) and order.get("order_id") is not None
        }
        unresolved = [order for order in orders if order.get("order_id") and str(order.get("order_id")) not in kits]
        if not unresolved:
            return kits
        run_id = str(run.get("id") or "")
        with self._guard:
            entry = self._cache.get(run_id)
            if entry is not None:
                self._cache.move_to_end(run_id)
            looked_up = set(entry["looked_up"]) if entry else set()
            cached = dict(entry["kits"]) if entry else {}
        # Заказы, которые ещё не искали (проверка идёт — они появляются по одному).
        fresh = [order for order in unresolved if str(order.get("order_id")) not in looked_up]
        if fresh and live and self.fetcher is not None:
            found = self._lookup(run_id, fresh)
            if found is not None:
                cached.update(found)
                looked_up.update(str(order.get("order_id")) for order in fresh)
                with self._guard:
                    self._cache[run_id] = {"looked_up": looked_up, "kits": cached}
                    self._cache.move_to_end(run_id)
                    while len(self._cache) > _CACHE_RUNS:
                        self._cache.popitem(last=False)
                logger.info(
                    "order_kits.resolved run_id=%s orders=%d kits=%d",
                    run_id, len(fresh), len({kit["main_id"] for kit in found.values()}),
                )
        for order in unresolved:
            order_id = str(order.get("order_id"))
            if order_id in cached:
                kits[order_id] = cached[order_id]
        return kits

    def _lookup(self, run_id: str, orders: list[dict[str, Any]]) -> dict[str, dict[str, Any]] | None:
        """Живой ``orderinfo``: сами заказы, затем предполагаемые главные; ``None`` — ошибка."""
        order_ids = sorted({str(order.get("order_id")) for order in orders})
        try:
            kits = kits_from_order_info(self.fetcher(order_ids))
        except Exception as exc:
            logger.warning("order_kits.orderinfo_failed run_id=%s error=%s", run_id, exc)
            return None
        # Главного заказа может не быть в запуске (уже возвращён или придёт позже),
        # а подзаказ Sborka отдельно не отдаёт: спросить предполагаемые главные заказы.
        known = set(order_ids)
        candidates = {
            candidate
            for order in orders
            if str(order.get("order_id")) not in kits
            for candidate in candidate_main_ids(order)
        } - known
        if candidates:
            try:
                extra = kits_from_order_info(self.fetcher(sorted(candidates)))
            except Exception as exc:
                logger.warning("order_kits.candidate_orderinfo_failed run_id=%s error=%s", run_id, exc)
                return None
            for order_id in known:
                if order_id not in kits and order_id in extra:
                    kits[order_id] = extra[order_id]
        return {order_id: kit for order_id, kit in kits.items() if order_id in known}
