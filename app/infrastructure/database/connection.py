# app/infrastructure/database/connection.py
"""Сборка SQLAlchemy engine и эксклюзивная сессионная блокировка worker."""

from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def create_database(url: str):
    """Создаёт небольшой пул; production использует async psycopg и PostgreSQL."""
    options = {"pool_pre_ping": True}
    if not url.startswith("sqlite"):
        options["connect_args"] = {"connect_timeout": 10, "options": "-c statement_timeout=30000"}
    engine = create_async_engine(url, **options)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def worker_lock(engine):
    """Держит advisory lock на выделенном соединении до завершения worker."""
    async with engine.connect() as connection:
        acquired = await connection.scalar(text("SELECT pg_try_advisory_lock(78164423)"))
        await connection.commit()
        try:
            yield connection if acquired else None
        finally:
            if acquired:
                await connection.execute(text("SELECT pg_advisory_unlock(78164423)"))
                await connection.commit()


async def ping(connection):
    """Проверяет, что соединение владельца lock по-прежнему живо, без reconnect."""
    await connection.execute(text("SELECT 1"))
    await connection.commit()
