# app/infrastructure/database/worker_health.py
"""Healthcheck worker по PostgreSQL heartbeat без файлов и обращения к источнику."""

import asyncio

from sqlalchemy import select

from app.config import Settings
from app.domain.models import utcnow
from app.infrastructure.database.connection import create_database
from app.infrastructure.database.models import WorkerState
from app.infrastructure.http.client import aware


async def healthy():
    """Проверяет свежесть heartbeat; устаревший или отсутствующий — unhealthy."""
    engine, sessions = create_database(Settings().db_url())
    try:
        async with sessions() as session:
            value = await session.scalar(select(WorkerState.heartbeat).where(WorkerState.id == 1))
            return value is not None and (utcnow() - aware(value)).total_seconds() < 60
    finally:
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(healthy()) else 1)
