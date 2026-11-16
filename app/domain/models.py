# app/domain/models.py
"""Независимые модели результатов и ошибки проверки публичного источника."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

TERMINAL = {"completed", "completed_with_errors", "failed", "cancelled"}
RUNNING = {"queued", "discovering", "processing", "cooldown"}
SUCCESS = {"ok", "missing"}


def utcnow() -> datetime:
    """Возвращает aware UTC для checkpoints и сравнения сроков хранения."""
    return datetime.now(UTC)


@dataclass
class Listing:
    """Ссылка на карточку; URL определяет идентичность, ИНН проверяется отдельно."""

    name: str
    inn: str
    detail_url: str
    ordinal: int = 0
    revenue_mln: Decimal | None = None


@dataclass
class CatalogPage:
    """Результат страницы с подтверждённой следующей ссылкой и общим числом."""

    companies: list[Listing]
    next_url: str | None
    total: int | None = None


@dataclass
class Detail:
    """Проверенные реквизиты и явно обозначенный руководитель, без учредителей."""

    inn: str
    director: str = ""
    director_status: str = "missing"
    kpp: str = ""


@dataclass
class Contacts:
    """Контакты только текущей компании в исходном порядке без повторов."""

    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    websites: list[str] = field(default_factory=list)
    status: str = "ok"


class SourceError(Exception):
    """Описывает безопасную причину сбоя без HTML и персональных контактов."""

    def __init__(
        self,
        message: str,
        kind: str = "source",
        status: int | None = None,
        attempts: int = 1,
        pause: bool = False,
        retry_after: float = 0,
    ):
        """Сохраняет тип сбоя, число запросов и необходимость ручной паузы."""
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.attempts = attempts
        self.pause = pause
        self.retry_after = retry_after
        self.url: str | None = None


class StructureError(SourceError):
    """Не позволяет принять изменившийся HTML за успешный пустой результат."""

    def __init__(self, message: str):
        """Требует проверки селекторов перед продолжением массового сбора."""
        super().__init__(message, kind="structure", pause=True)


class Interrupted(Exception):
    """Прерывает ожидание или операцию по паузе, отмене либо SIGTERM."""


class Conflict(Exception):
    """Сообщает о недопустимом переходе состояния задания."""


class NotFound(Exception):
    """Сигнализирует об отсутствующем задании или компании."""
