# tests/test_web.py
"""Критичные UI-действия в реальном браузере на изолированном локальном API."""

import asyncio
import os
import socket
import sys

import pytest
import uvicorn
from playwright.async_api import async_playwright, expect

from app.api.main import create_app
from app.config import Settings
from app.domain.models import CatalogPage, Contacts, Detail, Listing
from app.infrastructure.database.connection import create_database
from app.infrastructure.database.models import Base
from app.infrastructure.database.repository import Repository


@pytest.mark.skipif(not os.getenv("TEST_BROWSER"), reason="Set TEST_BROWSER=1 for UI integration")
def test_dashboard_search_contacts_and_controls(tmp_path):
    """Проверяет реальную загрузку JS, auth, таблицу, поиск и управляющие кнопки."""
    factory = asyncio.ProactorEventLoop if sys.platform == "win32" else asyncio.new_event_loop
    with asyncio.Runner(loop_factory=factory) as runner:
        runner.run(dashboard_flow("sqlite+aiosqlite:///" + (tmp_path / "ui.db").as_posix()))


async def dashboard_flow(database_url):
    """Поднимает API с отдельными SQLite-соединениями, не запуская collector."""
    # RAM StaticPool делит одно соединение: rollback polling мог отменять параллельный resume.
    # Временная тестовая БД изолирует транзакции, как production PostgreSQL.
    engine, sessions = create_database(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    repository = Repository(sessions)
    settings = Settings(_env_file=None, status_poll_interval=1)
    job = await repository.create_job(settings.source_url)
    companies = [
        Listing(
            f"Компания {i}",
            str(1000000000 + i),
            f"https://leaders-project.ru/companies/{1000000000 + i}",
        )
        for i in range(2)
    ]
    await repository.save_page(job["id"], settings.source_url, CatalogPage(companies, None, 2))
    first = await repository.next_company(job["id"])
    await repository.save_result(first["id"], "detail", Detail(first["inn"], "ИВАНОВ ИВАН", "ok"))
    await repository.save_result(
        first["id"],
        "contacts",
        Contacts(["+78001112233"], ["test@example.test"], ["https://example.test"]),
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(create_app(settings, repository), log_level="error", access_log=False)
    )
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        while not server.started:
            if serving.done():
                await serving
            await asyncio.sleep(0.01)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            context = await browser.new_context()
            page = await context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(f"http://127.0.0.1:{port}")
            await expect(page.locator('[data-metric="found"]')).to_have_text("2")
            await expect(page.locator("[data-companies] tr")).to_have_count(2)
            await page.get_by_role("searchbox").fill("1000000000")
            await expect(page.locator("[data-companies] tr")).to_have_count(1)
            await page.get_by_role("button", name="Контакты и ошибки: Компания 0").click()
            await expect(page.locator("[data-details]")).to_be_visible()
            await expect(page.locator("[data-details-content]")).to_contain_text(
                "test@example.test"
            )
            await page.get_by_role("button", name="Закрыть контакты").click()
            await page.locator('[data-action="pause"]').click()
            await expect(page.locator("[data-status]")).to_have_text("На паузе")
            await page.locator('[data-action="resume"]').click()
            await expect(page.locator("[data-status]")).to_have_text("В очереди")
            assert not errors
            await browser.close()
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 10)
        listener.close()
        await engine.dispose()
