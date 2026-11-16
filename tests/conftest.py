# tests/conftest.py
"""Изолированные БД и предоставленные HTML-fixtures; реальный сайт в тестах не вызывается."""

import asyncio
import os
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import make_url

from app.infrastructure.database.connection import create_database
from app.infrastructure.database.models import Base
from app.infrastructure.database.repository import Repository


def pytest_asyncio_loop_factories():
    """Selector поддерживает psycopg; browser-тест создаёт отдельный subprocess loop."""
    return {"selector": asyncio.SelectorEventLoop}


@pytest.fixture
def fixture_html():
    """Читает примеры, полученные от пользователя, без запросов к источнику."""

    def read(name):
        """Возвращает содержимое одного известного HTML-примера."""
        return (Path(__file__).parent / "fixtures" / name).read_text(encoding="utf-8")

    return read


@pytest_asyncio.fixture
async def repository():
    """Создаёт RAM-only SQLite для проверки транзакционных сценариев offline."""
    engine, sessions = create_database("sqlite+aiosqlite://")

    @event.listens_for(engine.sync_engine, "connect")
    def foreign_keys(connection, record):
        """Включает каскады, соответствующие контракту PostgreSQL."""
        connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield Repository(sessions)
    await engine.dispose()


@pytest_asyncio.fixture
async def postgres():
    """Использует только явно указанную одноразовую PostgreSQL test database."""
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    target = make_url(url)
    if target.database != "leaders_test" or target.get_backend_name() != "postgresql":
        raise RuntimeError("Use an isolated database named leaders_test")
    engine, sessions = create_database(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield engine, Repository(sessions)
    await engine.dispose()
