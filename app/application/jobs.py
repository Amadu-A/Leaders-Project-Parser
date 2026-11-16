# app/application/jobs.py
"""Сценарии управления заданиями и создания отчёта без HTTP/ORM-зависимостей."""

import asyncio

from app.application.ports import JobStore
from app.observability import timed


class Jobs:
    """Общий application service для API, интерфейса и экспорта."""

    def __init__(self, repository: JobStore, source_url: str, exporter):
        """Принимает persistence и memory-only exporter из composition root."""
        self.repository, self.source_url, self.exporter = repository, source_url, exporter
        self.export_lock = asyncio.Lock()

    @timed("create_job")
    async def create(self) -> dict:
        """Идемпотентно ставит задание в очередь; HTTP-запрос не запускает worker."""
        return await self.repository.create_job(self.source_url)

    @timed("control_job")
    async def control(self, job_id: str, action: str) -> dict:
        """Применяет управляющее действие в атомарной транзакции repository."""
        return await self.repository.control(job_id, action)

    @timed("export_companies")
    async def export(self, job_id: str) -> bytes:
        """Создаёт повторно скачиваемый XLSX только в RAM с ограничением concurrency."""
        async with self.export_lock:
            await self.repository.get_job(job_id)
            rows = [row async for row in self.repository.export_rows(job_id)]
            issues = await self.repository.issues(job_id)
            return await asyncio.to_thread(self.exporter, rows, issues)
