# tests/test_backfill.py
"""Дозаполнение старого задания не переиндексирует записи и не переписывает контакты."""

import json
from decimal import Decimal

from sqlalchemy import func, select, update

from app.application.collector import Collector
from app.domain.models import CatalogPage, Contacts, Detail, Listing, SourceError
from app.infrastructure.database.models import Company, Job, PageCheckpoint
from app.infrastructure.parsers import pages
from app.infrastructure.source_urls import next_catalogue_api_url

URL = "https://leaders-project.ru/companies/projecting?size=100"


def listing(number, revenue=None):
    """Создаёт карточку со стабильным URL для тестирования повторных проходов."""
    return Listing(
        f"Company {number}",
        str(1000000000 + number),
        f"https://leaders-project.ru/companies/{1000000000 + number}",
        revenue_mln=revenue,
    )


def item(number, revenue):
    """Возвращает синтетический формат API, в котором revenue выражен в рублях."""
    return {
        "name": f"Changed name {number}",
        "inn": str(1000000000 + number),
        "url": f"/companies/{1000000000 + number}",
        "revenue": revenue,
    }


async def checkpoint():
    """Не прерывает разрешённый тестовый шаг."""


class Source:
    """Fake источник записывает порядок запросов и исключает внешний трафик."""

    def __init__(self, responses):
        """Сохраняет сценарии ответов по URL и этапу."""
        self.responses, self.calls = responses, []

    async def preflight(self):
        """Имитирует разрешённый источник без массовых запросов."""

    async def load(self, url, stage):
        """Делает повторную обработку контактов заметной ошибкой теста."""
        self.calls.append((url, stage))
        value = self.responses[url]
        if isinstance(value, Exception):
            raise value
        return json.dumps(value), 1


async def test_upgrade_continues_index_cursor_then_only_backfills_revenue(repository):
    """Первый запрос после обновления идёт к сохранённой странице, не к началу."""
    job = await repository.create_job(URL)
    cursor = next_catalogue_api_url(URL, "saved-page-two")
    await repository.save_page(job["id"], URL, CatalogPage([listing(0)], cursor, 2))
    async with repository.sessions.begin() as session:
        await session.execute(
            update(Company)
            .where(Company.job_id == job["id"])
            .values(revenue_status="pending", director="OLD DIRECTOR", phones=["+70000000000"])
        )
    source = Source(
        {
            cursor: {"items": [item(1, 0)], "total": 2, "nextCursor": None},
            URL: {"items": [item(0, -2000000)], "total": 2, "nextCursor": "unused"},
        }
    )
    collector = Collector(repository, source, pages, checkpoint)
    await collector.step(await repository.get_job(job["id"]))
    assert source.calls == [(cursor, "discovering")]
    indexed = await repository.summary(job["id"])
    assert indexed["pages"] == 2 and indexed["found"] == 2 and indexed["next_url"] is None
    await collector.step(await repository.get_job(job["id"]))
    result = await repository.summary(job["id"])
    assert result["pages"] == 2 and result["found"] == 2 and result["next_url"] is None
    rows = sorted(
        (await repository.companies(job["id"], 1, 25))["items"], key=lambda row: row["ordinal"]
    )
    assert rows[0]["name"] == "Company 0" and rows[0]["director"] == "OLD DIRECTOR"
    assert rows[0]["phones"] == ["+70000000000"] and rows[0]["revenue_mln"] == Decimal(-2)
    assert rows[1]["revenue_mln"] == Decimal(0)
    assert result["revenue_backfill_complete"] and result["revenue_backfill_pages"] == 1


async def test_kpp_backfill_never_reprocesses_contacts_or_director(repository):
    """Готовая карточка получает только КПП; старые данные и попытки сохранены."""
    job = await repository.create_job(URL)
    await repository.save_page(job["id"], URL, CatalogPage([listing(0)], None, 1))
    row = await repository.next_company(job["id"])
    await repository.save_result(row["id"], "detail", Detail(row["inn"], "OLD DIRECTOR", "ok"))
    await repository.save_result(
        row["id"],
        "contacts",
        Contacts(["+70000000000"], ["old@example.test"], ["https://old.test"]),
    )
    async with repository.sessions.begin() as session:
        await session.execute(
            update(Company).where(Company.id == row["id"]).values(kpp_status="pending")
        )
    source = Source(
        {
            row["detail_url"]: {
                "inn": row["inn"],
                "kpp": "001234567",
                "contacts": "must not be parsed",
                "leaders": "must not be parsed",
            }
        }
    )
    collector = Collector(repository, source, pages, checkpoint)
    await collector.step(await repository.get_job(job["id"]))
    await collector.step(await repository.get_job(job["id"]))
    result = (await repository.companies(job["id"], 1, 25))["items"][0]
    assert source.calls == [(row["detail_url"], "detail")]
    assert result["kpp"] == "001234567" and result["director"] == "OLD DIRECTOR"
    assert result["phones"] == ["+70000000000"] and result["emails"] == ["old@example.test"]
    assert result["detail_attempts"] == result["contacts_attempts"] == 1
    assert (await repository.summary(job["id"]))["status"] == "completed"


async def test_revenue_backfill_cursor_survives_pause_and_new_collector(repository):
    """Повторная попытка продолжает отдельный cursor, сохраняя основной checkpoint."""
    job = await repository.create_job(URL)
    await repository.save_page(job["id"], URL, CatalogPage([listing(0), listing(1)], None, 2))
    async with repository.sessions.begin() as session:
        await session.execute(
            update(Company).where(Company.job_id == job["id"]).values(revenue_status="pending")
        )
    cursor = next_catalogue_api_url(URL, "field-page-two")
    source = Source(
        {
            URL: {"items": [item(0, 0)], "total": 2, "nextCursor": "field-page-two"},
            cursor: SourceError("Temporary network error", "network", attempts=4),
        }
    )
    collector = Collector(repository, source, pages, checkpoint)
    await collector.step(await repository.get_job(job["id"]))
    await collector.step(await repository.get_job(job["id"]))
    paused = await repository.get_job(job["id"])
    assert paused["status"] == "paused" and paused["revenue_backfill_next_url"] == cursor
    assert paused["next_url"] is None and paused["pages"] == 1
    await repository.control(job["id"], "resume")
    source.responses[cursor] = {"items": [item(1, None)], "total": 2, "nextCursor": None}
    await Collector(repository, source, pages, checkpoint).step(await repository.get_job(job["id"]))
    result = await repository.get_job(job["id"])
    assert result["revenue_backfill_complete"] and result["revenue_backfill_pages"] == 2
    assert result["next_url"] is None and result["pages"] == 1
    assert (await repository.summary(job["id"]))["errors"] == 0


async def test_field_backfill_requeues_existing_completed_id_only(repository):
    """Дозаполнение завершённого задания не создаёт job и не сбрасывает основной индекс."""
    job = await repository.create_job(URL)
    await repository.save_page(job["id"], URL, CatalogPage([listing(0)], None, 1))
    async with repository.sessions.begin() as session:
        await session.execute(
            update(Job).where(Job.id == job["id"]).values(status="completed", slot=None)
        )
    result = await repository.control(job["id"], "backfill-fields")
    assert result["id"] == job["id"] and result["status"] == "queued"
    assert result["pages"] == 1 and result["next_url"] is None
    async with repository.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Job)) == 1
        assert await session.scalar(select(func.count()).select_from(PageCheckpoint)) == 1
