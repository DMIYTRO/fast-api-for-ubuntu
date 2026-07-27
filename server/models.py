"""Persistent state for web sessions and processing runs."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def uuid_hex() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class SessionRecord(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_hex)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class CheckRun(Base):
    __tablename__ = "check_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_hex)
    input_path: Mapped[str] = mapped_column(Text)
    direction: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), index=True, default="queued")
    stage: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    options_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    orders: Mapped[list["OrderResult"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    events: Mapped[list["RunEvent"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class OrderResult(Base):
    __tablename__ = "order_results"
    __table_args__ = (
        Index(
            "ix_order_results_run_customer_order",
            "run_id",
            "customer_id",
            "order_id",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("check_runs.id", ondelete="CASCADE"), index=True
    )
    order_id: Mapped[str] = mapped_column(String(64), index=True)
    customer_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default="pending")
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    errors_json: Mapped[str] = mapped_column(Text, default="[]")
    warnings_json: Mapped[str] = mapped_column(Text, default="[]")
    pdf_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    run: Mapped[CheckRun] = relationship(back_populates="orders")
    files: Mapped[list["FileResult"]] = relationship(
        back_populates="order_result", cascade="all, delete-orphan"
    )
    actions: Mapped[list["OrderAction"]] = relationship(
        back_populates="order_result", cascade="all, delete-orphan"
    )


class FileResult(Base):
    __tablename__ = "file_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_result_id: Mapped[int] = mapped_column(
        ForeignKey("order_results.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(512))
    source_path: Mapped[str] = mapped_column(Text)
    side: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    format: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    colorspace: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    width_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    height_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    dpi_x: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    dpi_y: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default="pending")
    errors_json: Mapped[str] = mapped_column(Text, default="[]")
    warnings_json: Mapped[str] = mapped_column(Text, default="[]")
    preview_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    order_result: Mapped[OrderResult] = relationship(back_populates="files")
    decisions: Mapped[list["CorrectionDecision"]] = relationship(
        back_populates="file_result", cascade="all, delete-orphan"
    )


class CorrectionDecision(Base):
    __tablename__ = "correction_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_result_id: Mapped[int] = mapped_column(
        ForeignKey("file_results.id", ondelete="CASCADE"), index=True
    )
    decision: Mapped[str] = mapped_column(String(32))
    original_parameters_json: Mapped[str] = mapped_column(Text, default="{}")
    proposed_parameters_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    file_result: Mapped[FileResult] = relationship(back_populates="decisions")


class RunEvent(Base):
    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("check_runs.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    run: Mapped[CheckRun] = relationship(back_populates="events")


class OrderAction(Base):
    __tablename__ = "order_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_result_id: Mapped[int] = mapped_column(
        ForeignKey("order_results.id", ondelete="CASCADE"), index=True
    )
    action: Mapped[str] = mapped_column(String(32))
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default="prepared")
    cms_response_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    order_result: Mapped[OrderResult] = relationship(back_populates="actions")
