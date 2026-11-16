# app/application/ports.py
"""Контракты сценариев: HTTP, парсеры, persistence и экспорт заменяются через DI."""

from collections.abc import AsyncIterator
from typing import Protocol

from app.domain.models import CatalogPage, Contacts, Detail, SourceError


class JobStore(Protocol):
    """Контракт управления и прогресса без привязки к ORM."""

    async def create_job(self, source_url: str) -> dict:
        """Создаёт одно активное задание идемпотентно."""
        ...

    async def get_job(self, job_id: str) -> dict:
        """Возвращает checkpoint или NotFound."""
        ...

    async def control(self, job_id: str, action: str) -> dict:
        """Атомарно применяет разрешённое действие."""
        ...

    async def summary(self, job_id: str) -> dict:
        """Возвращает агрегированный прогресс."""
        ...

    async def export_rows(self, job_id: str) -> AsyncIterator[dict]:
        """Перечисляет все найденные компании в исходном порядке."""
        yield {}

    async def issues(self, job_id: str) -> list[dict]:
        """Возвращает открытые ошибки для отчёта."""
        ...

    async def ready(self) -> bool:
        """Проверяет доступность обязательной схемы БД."""
        ...

    async def latest(self) -> dict | None:
        """Восстанавливает последнее выбранное задание."""
        ...

    async def companies(
        self, job_id: str, page: int, size: int, query: str = "", errors_only: bool = False
    ) -> dict:
        """Возвращает страницу компаний с серверной фильтрацией."""
        ...

    async def delete_job(self, job_id: str) -> None:
        """Удаляет только терминальное задание либо отклоняет действие."""
        ...


class CollectorStore(Protocol):
    """Атомарные checkpoints последовательной индексации и обогащения."""

    async def set_stage(self, job_id: str, stage: str) -> None:
        """Обновляет этап, сохраняя внешнюю pause/cancel команду."""
        ...

    async def resolve_preflight(self, job_id: str) -> None:
        """Разрешает предыдущий сбой после успешной проверки доступа."""
        ...

    async def save_page(self, job_id: str, url: str, page: CatalogPage) -> None:
        """Фиксирует компании и следующий cursor одной транзакцией."""
        ...

    async def next_company(self, job_id: str) -> dict | None:
        """Находит первую незавершённую операцию."""
        ...

    async def save_result(
        self, company_id: int, stage: str, result: Detail | Contacts, attempts: int = 1
    ) -> None:
        """Идемпотентно сохраняет успешный результат одной страницы."""
        ...

    async def record_issue(
        self, job_id: str, company_id: int | None, url: str, stage: str, error: SourceError
    ) -> None:
        """Регистрирует сбой без удаления предыдущих результатов."""
        ...

    async def record_duration(self, job_id: str, seconds: float) -> None:
        """Добавляет законченный образец длительности для ETA."""
        ...

    async def finish(self, job_id: str) -> None:
        """Завершает только полностью просмотренный список с честным статусом."""
        ...


class Source(Protocol):
    """Источник публичных JSON/HTML-документов с общей политикой доступа и limiter."""

    async def preflight(self) -> None:
        """Проверяет robots и применимые условия до массового сбора."""
        ...

    async def load(self, url: str, stage: str) -> tuple[str, int]:
        """Загружает проверяемую страницу и фактическое число попыток."""
        ...


class Parsers(Protocol):
    """Контракт чистого преобразования публичных JSON/HTML в предметные DTO."""

    def catalog(self, html: str, url: str) -> CatalogPage:
        """Извлекает список и подтверждённую следующую ссылку."""
        ...

    def detail(self, html: str, expected_inn: str) -> Detail:
        """Проверяет ИНН и находит руководителя."""
        ...

    def contacts(self, html: str) -> Contacts:
        """Возвращает все доступные контакты одной компании."""
        ...
