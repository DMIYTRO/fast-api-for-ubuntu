"""
Модуль сохранения истории аудитов макетов и заказов через SQLAlchemy.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from processing.models import FileCheck, OrderCheck
from config.profiles import DEFAULT_PROFILE, PrePressProfile


class Base(DeclarativeBase):
    pass


class OrderAudit(Base):
    """Модель аудита заказа в базе данных."""

    __tablename__ = "order_audits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    order_id: Mapped[str] = mapped_column(String(50), index=True)
    customer_id: Mapped[str] = mapped_column(String(50), index=True)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    total_files: Mapped[int] = mapped_column(Integer, default=0)
    errors_json: Mapped[str] = mapped_column(Text, default="[]")
    warnings_json: Mapped[str] = mapped_column(Text, default="[]")
    pdf_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    previews_count: Mapped[int] = mapped_column(Integer, default=0)

    files: Mapped[List["FileAudit"]] = relationship(
        "FileAudit", back_populates="order", cascade="all, delete-orphan"
    )

    @property
    def errors(self) -> list[str]:
        return json.loads(self.errors_json or "[]")

    @property
    def warnings(self) -> list[str]:
        return json.loads(self.warnings_json or "[]")


class FileAudit(Base):
    """Модель аудита отдельного файла в базе данных."""

    __tablename__ = "file_audits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_audit_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("order_audits.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(255))
    side: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    actual_width_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    actual_height_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    expected_width_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    expected_height_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    width_px: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    height_px: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    dpi_x: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    dpi_y: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    colorspace: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    actual_format: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    color_mode: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    needs_resample: Mapped[bool] = mapped_column(Boolean, default=False)
    resample_target_w_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    resample_target_h_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    errors_json: Mapped[str] = mapped_column(Text, default="[]")
    warnings_json: Mapped[str] = mapped_column(Text, default="[]")

    order: Mapped[OrderAudit] = relationship("OrderAudit", back_populates="files")

    @property
    def errors(self) -> list[str]:
        return json.loads(self.errors_json or "[]")

    @property
    def warnings(self) -> list[str]:
        return json.loads(self.warnings_json or "[]")


def get_db_engine(db_path: Optional[Path] = None):
    if db_path is None:
        db_path = Path(__file__).resolve().parent.parent / "audit_history.db"
    db_url = f"sqlite:///{db_path}"
    return create_engine(db_url, echo=False)


def init_db(db_path: Optional[Path] = None) -> None:
    """Инициализирует таблицы базы данных."""
    engine = get_db_engine(db_path)
    Base.metadata.create_all(engine)


def save_order_audit(
    order: OrderCheck,
    pdf_path: Optional[str | Path] = None,
    previews_count: int = 0,
    db_path: Optional[Path] = None,
    profile: PrePressProfile = DEFAULT_PROFILE,
) -> OrderAudit:
    """Сохраняет полную проверку заказа и его файлов в базу данных SQLAlchemy."""
    engine = get_db_engine(db_path)
    Base.metadata.create_all(engine)

    Session = sessionmaker(bind=engine)
    with Session() as session:
        audit = OrderAudit(
            order_id=order.order_id,
            customer_id=order.customer_id,
            passed=order.passed,
            total_files=len(order.files),
            errors_json=json.dumps(order.errors, ensure_ascii=False),
            warnings_json=json.dumps(order.warnings, ensure_ascii=False),
            pdf_path=str(pdf_path) if pdf_path else None,
            previews_count=previews_count,
        )

        for file_check in order.files:
            target_w = (
                file_check.resample_target_mm[0]
                if file_check.resample_target_mm
                else None
            )
            target_h = (
                file_check.resample_target_mm[1]
                if file_check.resample_target_mm
                else None
            )

            expected = (
                profile.expected_size_mm(
                    file_check.path.name, file_check.parsed.width_mm, file_check.parsed.height_mm
                )
                if file_check.parsed
                else (None, None)
            )
            exp_w, exp_h = expected
            c_mode = f"{file_check.parsed.front_colors}-{file_check.parsed.back_colors}" if file_check.parsed else None

            file_audit = FileAudit(
                filename=file_check.path.name,
                side=file_check.parsed.side if file_check.parsed else None,
                passed=file_check.passed,
                actual_width_mm=file_check.actual_width_mm,
                actual_height_mm=file_check.actual_height_mm,
                expected_width_mm=exp_w,
                expected_height_mm=exp_h,
                width_px=file_check.width_px,
                height_px=file_check.height_px,
                dpi_x=file_check.dpi_x,
                dpi_y=file_check.dpi_y,
                colorspace=file_check.colorspace,
                actual_format=file_check.actual_format,
                color_mode=c_mode,
                needs_resample=file_check.needs_resample,
                resample_target_w_mm=target_w,
                resample_target_h_mm=target_h,
                errors_json=json.dumps(file_check.errors, ensure_ascii=False),
                warnings_json=json.dumps(file_check.warnings, ensure_ascii=False),
            )
            audit.files.append(file_audit)

        session.add(audit)
        session.commit()
        session.refresh(audit)
        return audit
