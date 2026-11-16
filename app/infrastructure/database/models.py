# app/infrastructure/database/models.py
"""ORM PostgreSQL: задания, компании, checkpoints, ошибки и состояние worker."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.domain.models import utcnow


class Base(DeclarativeBase):
    """Общая metadata миграций и изолированных интеграционных тестов."""


class Job(Base):
    """Долговечный checkpoint; unique slot исключает второй незавершённый сбор."""

    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    slot: Mapped[int | None] = mapped_column(Integer, unique=True, default=1)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(32), default="preflight")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    next_url: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    revenue_backfill_next_url: Mapped[str | None] = mapped_column(Text)
    revenue_backfill_complete: Mapped[bool] = mapped_column(
        default=False, server_default=text("false")
    )
    revenue_backfill_pages: Mapped[int] = mapped_column(default=0, server_default="0")
    pages: Mapped[int] = mapped_column(default=0)
    expected_total: Mapped[int | None]
    discovery_complete: Mapped[bool] = mapped_column(default=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_success: Mapped[str | None] = mapped_column(Text)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[float] = mapped_column(default=0)
    timed_companies: Mapped[int] = mapped_column(default=0)


class Company(Base):
    """Одна обнаруженная карточка с независимыми checkpoints двух страниц."""

    __tablename__ = "companies"
    __table_args__ = (
        UniqueConstraint("job_id", "detail_url"),
        UniqueConstraint("job_id", "ordinal"),
        Index("ix_company_job_inn", "job_id", "inn"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    detail_url: Mapped[str] = mapped_column(Text)
    ordinal: Mapped[int]
    name: Mapped[str] = mapped_column(Text)
    inn: Mapped[str] = mapped_column(String(32))
    kpp: Mapped[str] = mapped_column(String(32), default="", server_default="")
    revenue_mln: Mapped[Decimal | None] = mapped_column(Numeric(26, 8))
    kpp_status: Mapped[str] = mapped_column(String(32), default="pending", server_default="pending")
    revenue_status: Mapped[str] = mapped_column(
        String(32), default="pending", server_default="pending"
    )
    kpp_attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    director: Mapped[str] = mapped_column(Text, default="")
    director_status: Mapped[str] = mapped_column(String(32), default="unchecked")
    phones: Mapped[list] = mapped_column(JSON, default=list)
    emails: Mapped[list] = mapped_column(JSON, default=list)
    websites: Mapped[list] = mapped_column(JSON, default=list)
    detail_status: Mapped[str] = mapped_column(String(32), default="pending")
    contacts_status: Mapped[str] = mapped_column(String(32), default="pending")
    detail_attempts: Mapped[int] = mapped_column(default=0)
    contacts_attempts: Mapped[int] = mapped_column(default=0)
    inn_conflict: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PageCheckpoint(Base):
    """Фиксирует обработанные URL и fingerprint списка для обнаружения циклов."""

    __tablename__ = "catalog_pages"
    __table_args__ = (UniqueConstraint("job_id", "url"), UniqueConstraint("job_id", "fingerprint"))
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64))


class RevenueBackfillPage(Base):
    """Отдельный журнал прохода выручки, не меняющий checkpoints индексации."""

    __tablename__ = "revenue_backfill_pages"
    __table_args__ = (UniqueConstraint("job_id", "url"), UniqueConstraint("job_id", "fingerprint"))
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64))


class Issue(Base):
    """Текущие и разрешённые ошибки без сохранения ответов источника."""

    __tablename__ = "issues"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(Text)
    stage: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    http_status: Mapped[int | None]
    attempts: Mapped[int] = mapped_column(default=1)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved: Mapped[bool] = mapped_column(default=False)


class Event(Base):
    """Ограниченный журнал пользовательских событий задания без контактов."""

    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkerState(Base):
    """Глобальный limiter и heartbeat переживают перезапуски процесса."""

    __tablename__ = "worker_state"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    heartbeat: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    next_request_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
