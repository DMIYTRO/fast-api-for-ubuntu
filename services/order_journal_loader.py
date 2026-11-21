"""Загрузка данных для журнала заказа (``services/order_journal.py``).

Число запросов не зависит от размера запуска: заказ читается через
``RunCoordinator.list_orders_page`` с поиском по номеру (активный запуск — из
памяти), события — одним отфильтрованным запросом к ``run_events`` и одним к
``archived_run_events``, действия и ревизии — двумя запросами по строке заказа.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from server.models import OrderAction, OrderResult, PdfRevision
from services.coordinator import AmbiguousOrderError, RunCoordinator, RunNotFoundError
from services.order_journal import JOURNAL_EVENT_TYPES, build_order_journal

logger = logging.getLogger("image_magic.order_journal")

_PAGE_SIZE = 100
_MAX_PAGES = 20


class OrderJournalNotFound(LookupError):
    """Запуск, заказ или запись истории не найдены."""


class OrderJournalLoader:
    def __init__(
        self, coordinator: RunCoordinator, session_factory: Callable[[], Session] | None
    ) -> None:
        self.coordinator = coordinator
        self.session_factory = session_factory

    # ---------------------------------------------------------------- public
    def for_history_action(self, action_id: int) -> dict[str, Any]:
        if self.session_factory is None:
            raise OrderJournalNotFound("history")
        with self.session_factory() as session:
            action = session.get(OrderAction, action_id)
            if action is None:
                raise OrderJournalNotFound("history")
            record = action.order_result
            run_id, customer_id, order_id = record.run_id, record.customer_id or "", record.order_id
        return self._journal(run_id, customer_id, order_id)

    def for_order(self, run_id: str, order_ref: str) -> dict[str, Any]:
        """``order_ref`` — ``"клиент:заказ"`` или номер заказа (неоднозначный → 409)."""
        customer_id, _, number = order_ref.rpartition(":")
        return self._journal(run_id, customer_id or None, number)

    # -------------------------------------------------------------- internals
    def _journal(self, run_id: str, customer_id: str | None, order_id: str) -> dict[str, Any]:
        order = self._find_order(run_id, customer_id, order_id)
        customer = str(order.get("customer_id") or "")
        aggregate = str(order.get("aggregate_id") or (f"{customer}:{order_id}" if customer else order_id))
        order.setdefault("aggregate_id", aggregate)
        events = self._events(run_id, order_id, aggregate, customer)
        actions, revisions = self._rows(run_id, customer, order_id)
        if not actions:
            actions = [item for item in order.get("actions") or () if isinstance(item, dict)]
        return build_order_journal(order, events, actions, revisions, run_id=run_id)

    def _find_order(self, run_id: str, customer_id: str | None, order_id: str) -> dict[str, Any]:
        matches: list[dict[str, Any]] = []
        try:
            for page in range(1, _MAX_PAGES + 1):
                result = self.coordinator.list_orders_page(
                    run_id, page=page, page_size=_PAGE_SIZE, status="all",
                    search=order_id, active_only=False,
                )
                for item in result.get("items") or ():
                    if str(item.get("order_id") or "") != order_id:
                        continue
                    if customer_id is not None and str(item.get("customer_id") or "") != customer_id:
                        continue
                    matches.append(item)
                if page >= int(result.get("total_pages") or 1):
                    break
        except RunNotFoundError as exc:
            raise OrderJournalNotFound("run") from exc
        if not matches:
            raise OrderJournalNotFound("order")
        if len({str(item.get("customer_id") or "") for item in matches}) > 1:
            raise AmbiguousOrderError(order_id)
        return matches[0]

    def _events(self, run_id: str, order_id: str, aggregate: str, customer: str) -> list[Any]:
        repository = self.coordinator.repository
        lister = getattr(repository, "list_order_events", None)
        if callable(lister):
            events = lister(run_id, order_id=order_id, event_types=JOURNAL_EVENT_TYPES)
        else:
            wanted = set(JOURNAL_EVENT_TYPES)
            events = [event for event in self.coordinator.events(run_id) if event.type in wanted]
        result = []
        for event in events:
            data = event.data if isinstance(event.data, dict) else {}
            event_aggregate = data.get("aggregate_id")
            if event_aggregate:
                if str(event_aggregate) != aggregate:
                    continue
            elif str(data.get("order_id") or "") != order_id or (
                data.get("customer_id") not in (None, "") and str(data["customer_id"]) != customer
            ):
                continue
            result.append(event)
        return result

    def _rows(self, run_id: str, customer: str, order_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if self.session_factory is None:
            return [], []
        with self.session_factory() as session:
            customer_clause = (
                OrderResult.customer_id == customer if customer
                else OrderResult.customer_id.is_(None) | (OrderResult.customer_id == "")
            )
            record = session.scalars(
                select(OrderResult)
                .where(OrderResult.run_id == run_id, OrderResult.order_id == order_id, customer_clause)
                .options(
                    selectinload(OrderResult.actions),
                    selectinload(OrderResult.pdf_revisions).selectinload(PdfRevision.checks),
                )
                .order_by(OrderResult.id)
            ).first()
            if record is None:
                return [], []
            actions = [
                {
                    "id": action.id,
                    "action": action.action,
                    "status": action.status,
                    "comment": action.comment,
                    "created_at": _iso(action.created_at),
                    "cms_response": _json(action.cms_response_json),
                }
                for action in sorted(record.actions, key=lambda item: item.id)
            ]
            revisions = []
            for revision in sorted(record.pdf_revisions, key=lambda item: item.revision_number):
                latest = max(
                    revision.checks,
                    key=lambda item: (item.checked_at or item.created_at, item.created_at, item.id),
                    default=None,
                )
                revisions.append({
                    "revision_number": revision.revision_number,
                    "created_at": _iso(revision.created_at),
                    "sha256": revision.sha256,
                    "is_current": bool(revision.is_current),
                    "created_by_action": revision.created_by_action,
                    "verdict": latest.verdict if latest is not None else None,
                })
            return actions, revisions


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _json(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        logger.warning("order_journal.cms_response_unreadable")
        return None
    return parsed if isinstance(parsed, dict) else None
