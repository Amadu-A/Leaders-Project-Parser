# tests/test_jobs.py
"""Транзакционные сценарии pause/resume, дедупликации, восстановления и очистки."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.application.collector import Collector
from app.domain.models import (
    CatalogPage,
    Conflict,
    Contacts,
    Detail,
    Interrupted,
    Listing,
    SourceError,
    utcnow,
)
from app.infrastructure.database.connection import worker_lock
from app.infrastructure.database.models import Job
from app.infrastructure.database.repository import Repository
from app.infrastructure.parsers import pages

URL = "https://leaders-project.ru/companies/projecting?size=100"


def listing(number):
    """Создаёт уникальную синтетическую карточку, не обращаясь к источнику."""
    inn = str(1000000000 + number)
    return Listing(f"Компания {number}", inn, "https://leaders-project.ru/companies/" + inn)


async def indexed(repository, count=1):
    """Создаёт полностью обнаруженный список для сценариев обработки."""
    job = await repository.create_job(URL)
    await repository.save_page(
        job["id"], URL, CatalogPage([listing(i) for i in range(count)], None, count)
    )
    return job["id"]


async def test_controls_and_duplicate_click(repository):
    """Повторные start/pause/resume/cancel не создают второй активный сбор."""
    first = await repository.create_job(URL)
    assert (await repository.create_job(URL))["id"] == first["id"]
    await repository.control(first["id"], "pause")
    assert await repository.active() is None
    assert (await repository.create_job(URL))["id"] == first["id"]
    await repository.control(first["id"], "pause")
    await repository.control(first["id"], "resume")
    await repository.control(first["id"], "resume")
    assert (await repository.active())["id"] == first["id"]
    with pytest.raises(Conflict):
        await repository.delete_job(first["id"])
    await repository.control(first["id"], "cancel")
    await repository.control(first["id"], "cancel")
    assert (await repository.create_job(URL))["id"] != first["id"]


async def test_catalog_checkpoints_dedupe_and_last_page(repository):
    """Сохраняет последнюю страницу и не дублирует повторное обнаружение URL."""
    job_id = (await repository.create_job(URL))["id"]
    next_url = URL + "&page=2"
    first = CatalogPage([listing(i) for i in range(100)], next_url, 117)
    await repository.save_page(job_id, URL, first)
    await repository.save_page(job_id, URL, first)
    await repository.save_page(
        job_id, next_url, CatalogPage([listing(i) for i in range(99, 117)], None, 117)
    )
    summary = await repository.summary(job_id)
    assert summary["found"] == 117 and summary["discovery_complete"]
    assert summary["pages"] == 2 and summary["next_url"] is None
    assert (await repository.companies(job_id, 1, 100))["total"] == 117
    assert len((await repository.companies(job_id, 2, 100))["items"]) == 17


async def test_pagination_shortfall_and_inn_conflict(repository):
    """Не выдаёт неполный индекс за успех и не удаляет конфликтующие карточки."""
    job_id = (await repository.create_job(URL))["id"]
    first, other = listing(0), listing(1)
    other.inn = first.inn
    await repository.save_page(job_id, URL, CatalogPage([first, other], None, 3))
    summary = await repository.summary(job_id)
    assert summary["status"] == "paused" and not summary["discovery_complete"]
    assert summary["found"] == 2 and summary["errors"] == 2
    rows = (await repository.companies(job_id, 1, 25))["items"]
    assert all(row["inn_conflict"] for row in rows)


async def test_restart_resumes_only_pending_operation(repository, fixture_html):
    """Новый Collector продолжает контакты, сохраняя уже завершённую карточку."""
    job_id = (await repository.create_job(URL))["id"]
    company = Listing(
        "Энергопрогресс", "1657043757", "https://leaders-project.ru/companies/1657043757"
    )
    await repository.save_page(job_id, URL, CatalogPage([company], None, 1))
    row = await repository.next_company(job_id)
    await repository.save_result(row["id"], "detail", Detail(company.inn, "ДИРЕКТОР", "ok"))
    calls = []

    class FakeSource:
        """Публичный source-port; повтор detail считается ошибкой сценария."""

        async def preflight(self):
            """Имитирует успешную проверку доступа после рестарта."""

        async def load(self, url, stage):
            """Отдаёт только оставшиеся контакты из fixture."""
            calls.append(stage)
            assert stage == "contacts"
            return fixture_html("contacts.html"), 1

    async def checkpoint():
        """Не вмешивается в успешную восстановленную обработку."""

    restarted = Collector(Repository(repository.sessions), FakeSource(), pages, checkpoint)
    await restarted.step(await repository.get_job(job_id))
    await restarted.step(await repository.get_job(job_id))
    summary = await repository.summary(job_id)
    assert calls == ["contacts"] and summary["status"] == "completed"
    assert summary["processed"] == 1
    assert len((await repository.companies(job_id, 1, 25))["items"][0]["phones"]) == 5


async def test_partial_error_retry_and_export_visibility(repository):
    """Ошибка страницы оставляет данные доступными и повторяет только ошибочный шаг."""
    job_id = await indexed(repository)
    row = await repository.next_company(job_id)
    await repository.save_result(row["id"], "detail", Detail(row["inn"], "Иванов", "ok"))
    await repository.record_issue(
        job_id,
        row["id"],
        row["detail_url"] + "/contacts",
        "contacts",
        SourceError("Сбой контактов", "network", attempts=4),
    )
    await repository.finish(job_id)
    summary = await repository.summary(job_id)
    assert summary["status"] == "completed_with_errors" and summary["processed"] == 0
    rows = [row async for row in repository.export_rows(job_id)]
    assert len(rows) == 1 and rows[0]["director"] == "Иванов"
    await repository.control(job_id, "retry-failed")
    row = await repository.next_company(job_id)
    assert row["detail_status"] == "ok" and row["contacts_status"] == "pending"
    await repository.save_result(row["id"], "contacts", Contacts())
    await repository.finish(job_id)
    assert (await repository.summary(job_id))["status"] == "completed"


async def test_pause_survives_worker_save_and_cleanup(repository):
    """Поздний checkpoint не снимает пользовательскую паузу, active не очищается."""
    job_id = await indexed(repository)
    await repository.control(job_id, "pause")
    await repository.set_stage(job_id, "processing")
    await repository.finish(job_id)
    await repository.cleanup(7)
    assert (await repository.get_job(job_id))["status"] == "paused"
    await repository.control(job_id, "cancel")
    async with repository.sessions.begin() as session:
        await session.execute(
            update(Job).where(Job.id == job_id).values(finished_at=utcnow() - timedelta(days=8))
        )
    await repository.cleanup(7)
    assert await repository.latest() is None


@pytest.mark.postgres
async def test_postgres_concurrency_and_lock(postgres):
    """Реальная PostgreSQL сериализует start и эксклюзивное владение worker."""
    engine, repository = postgres
    results = await asyncio.gather(*(repository.create_job(URL) for _ in range(5)))
    assert len({job["id"] for job in results}) == 1
    async with worker_lock(engine) as first:
        assert first is not None
        async with worker_lock(engine) as second:
            assert second is None
    async with worker_lock(engine) as recovered:
        assert recovered is not None
    async with repository.sessions() as session:
        assert len((await session.scalars(select(Job))).all()) == 1


@pytest.mark.postgres
async def test_postgres_checkpoint_survives_reconnection(postgres):
    """Результаты доступны после закрытия пула и сборки нового repository."""
    engine, repository = postgres
    job_id = await indexed(repository, 2)
    row = await repository.next_company(job_id)
    await repository.save_result(row["id"], "detail", Detail(row["inn"]))
    await engine.dispose()
    restored = Repository(repository.sessions)
    row = await restored.next_company(job_id)
    assert row["ordinal"] == 1 and row["detail_status"] == "ok"
    assert row["contacts_status"] == "pending"
    assert (await restored.summary(job_id))["found"] == 2


async def test_interruption_does_not_advance_checkpoint(repository):
    """Прерывание после HTTP до save не создаёт ложного успешного результата."""
    job_id = await indexed(repository)
    calls = 0

    async def checkpoint():
        """Имитирует SIGTERM непосредственно перед записью результата."""
        nonlocal calls
        calls += 1
        if calls >= 3:
            raise Interrupted

    class Source:
        """Отдаёт HTML, который не требует реального HTTP."""

        async def preflight(self):
            """Разрешает проверку без внешних запросов."""

        async def load(self, url, stage):
            """Возвращает результат запроса перед отменой."""
            return "", 1

    class Parser:
        """Изолирует checkpoint-сценарий от селекторов HTML."""

        def detail(self, html, inn):
            """Возвращает распознанную карточку для проверки отмены save."""
            return Detail(inn)

    with pytest.raises(Interrupted):
        await Collector(repository, Source(), Parser(), checkpoint).step(
            await repository.get_job(job_id)
        )
    assert (await repository.next_company(job_id))["detail_status"] == "pending"


async def test_collector_indexes_every_page_before_details(repository):
    """Сценарий полностью сохраняет индекс до первой карточки, включая последний список."""
    job_id = (await repository.create_job(URL))["id"]
    calls = []

    class Source:
        """Изолированный source-port с двумя списками и успешными карточками."""

        async def preflight(self):
            """Разрешает публичный сбор в тестовом источнике."""

        async def load(self, url, stage):
            """Фиксирует порядок операций вместо выполнения сетевых запросов."""
            calls.append(stage)
            if stage != "discovering":
                assert (await repository.summary(job_id))["found"] == 3
            return url, 1

    class Parser:
        """Даёт предметные результаты, не связывая тест оркестрации с HTML."""

        def catalog(self, html, url):
            """Отдаёт второй cursor только для первой страницы."""
            return (
                CatalogPage([listing(0), listing(1)], URL + "&cursor=next", 3)
                if url == URL
                else CatalogPage([listing(2)], None, 3)
            )

        def detail(self, html, inn):
            """Сохраняет успешную проверку реквизитов без руководителя."""
            return Detail(inn)

        def contacts(self, html):
            """Отмечает подтверждённое отсутствие контактных данных."""
            return Contacts(status="missing")

    async def checkpoint():
        """Имитирует отсутствие внешней команды pause/cancel."""

    collector = Collector(repository, Source(), Parser(), checkpoint)
    for _ in range(6):
        await collector.step(await repository.get_job(job_id))
    assert calls[:2] == ["discovering", "discovering"]
    assert calls[2:] == ["detail", "contacts"] * 3
    assert (await repository.summary(job_id))["status"] == "completed"


async def test_collector_preflight_failure_can_be_resumed(repository):
    """Успешная повторная проверка снимает старую preflight-ошибку и продолжает индекс."""
    job_id = (await repository.create_job(URL))["id"]

    class Source:
        """Один раз ограничивает доступ, затем предоставляет пустые реквизиты."""

        blocked = True

        async def preflight(self):
            """Имитирует временный 403 до явного пользовательского resume."""
            if self.blocked:
                raise SourceError("Ограничение доступа", "access", 403, pause=True)

        async def load(self, url, stage):
            """Возвращает единственную страницу после разрешения проблемы."""
            return "", 1

    class Parser:
        """Ограничивает проверку только предварительным этапом и индексацией."""

        def catalog(self, html, url):
            """Отдаёт полностью обнаруженный индекс из одной карточки."""
            return CatalogPage([listing(0)], None, 1)

    async def checkpoint():
        """Не меняет состояние за пределами действий теста."""

    source = Source()
    collector = Collector(repository, source, Parser(), checkpoint)
    await collector.step(await repository.get_job(job_id))
    assert (await repository.summary(job_id))["status"] == "paused"
    assert (await repository.summary(job_id))["errors"] == 1
    source.blocked = False
    await repository.control(job_id, "resume")
    await collector.step(await repository.get_job(job_id))
    result = await repository.summary(job_id)
    assert result["discovery_complete"] and result["errors"] == 0


async def test_paused_access_issue_is_visible_in_errors_filter(repository):
    """403 остаётся pending для resume, но виден в таблице ошибок и счётчике попыток."""
    job_id = await indexed(repository)
    company = await repository.next_company(job_id)
    await repository.record_issue(
        job_id,
        company["id"],
        company["detail_url"],
        "detail",
        SourceError("Доступ ограничен", "access", 403, pause=True),
    )
    errors = await repository.companies(job_id, 1, 25, errors_only=True)
    assert errors["total"] == 1
    assert errors["items"][0]["detail_status"] == "pending"
    assert errors["items"][0]["detail_attempts"] == 1
    assert errors["items"][0]["issues"][0]["http_status"] == 403


async def test_late_worker_results_after_cancel_and_delete_are_safe(repository):
    """Поздний HTTP-ответ после удаления отменённого задания не восстанавливает данные."""
    job_id = await indexed(repository)
    company = await repository.next_company(job_id)
    await repository.control(job_id, "cancel")
    await repository.delete_job(job_id)
    await repository.set_stage(job_id, "processing")
    await repository.save_page(job_id, URL, CatalogPage([listing(1)], None, 1))
    await repository.save_result(company["id"], "detail", Detail(company["inn"]))
    await repository.record_issue(
        job_id,
        company["id"],
        company["detail_url"],
        "detail",
        SourceError("Поздний сбой", "network"),
    )
    await repository.finish(job_id)
    assert await repository.latest() is None


async def test_preflight_probe_checks_three_companies_before_bulk(repository):
    """Проба подтверждает доступность полей, но не увеличивает счётчик обработанных."""
    job_id = (await repository.create_job(URL))["id"]
    calls = []

    class Source:
        """Даёт три синтетические карточки для проверки предварительного gate."""

        async def preflight(self):
            """Подтверждает правила доступа тестового источника."""

        async def load(self, url, stage):
            """Сохраняет порядок ограниченной пробы без реального HTTP."""
            calls.append(stage)
            return "", 1

    class Parser:
        """Отделяет сценарий пробы от независимых HTML-селекторов."""

        def catalog(self, html, url):
            """Возвращает полный маленький каталог из трёх организаций."""
            return CatalogPage([listing(i) for i in range(3)], None, 3)

        def detail(self, html, inn):
            """Подтверждает проверку реквизитов одной пробной карточки."""
            return Detail(inn)

        def contacts(self, html):
            """Подтверждает распознанную контактную страницу."""
            return Contacts()

    async def checkpoint():
        """Разрешает все операции изолированной пробы."""

    await Collector(repository, Source(), Parser(), checkpoint, probe_count=3).step(
        await repository.get_job(job_id)
    )
    assert calls == ["discovering"] + ["detail", "contacts"] * 3
    summary = await repository.summary(job_id)
    assert summary["found"] == 3 and summary["processed"] == 0


@pytest.mark.parametrize("unavailable", [2, 3])
async def test_probe_requires_three_valid_pairs_among_five(repository, unavailable):
    """Две доступные пары из пяти не открывают массовый сбор, три — открывают."""
    job_id = (await repository.create_job(URL))["id"]

    class Source:
        """Имитирует удалённые карточки, не ошибку доступа ко всему источнику."""

        async def preflight(self):
            """Подтверждает правила доступа до пробы."""

        async def load(self, url, stage):
            """Первые кандидаты недоступны; остальные имеют обе страницы."""
            if stage == "detail" and int(url.rsplit("/", 1)[1]) < 1000000000 + unavailable:
                raise SourceError("Страница удалена", "not_found", 404)
            return "", 1

    class Parser:
        """Даёт пять кандидатов для ограниченной предварительной проверки."""

        def catalog(self, html, url):
            """Возвращает полный малый индекс без дополнительной пагинации."""
            return CatalogPage([listing(i) for i in range(5)], None, 5)

        def detail(self, html, inn):
            """Подтверждает реквизиты доступной карточки."""
            return Detail(inn)

        def contacts(self, html):
            """Подтверждает проверенную контактную страницу."""
            return Contacts()

    async def checkpoint():
        """Разрешает шаги изолированной пробы."""

    await Collector(repository, Source(), Parser(), checkpoint, probe_count=3).step(
        await repository.get_job(job_id)
    )
    summary = await repository.summary(job_id)
    assert summary["found"] == (5 if unavailable == 2 else 0)
    assert summary["status"] == ("discovering" if unavailable == 2 else "paused")
