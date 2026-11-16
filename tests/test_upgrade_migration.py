# tests/test_upgrade_migration.py
"""Реальная PostgreSQL: обновление схемы 0001 с данными и продолжение старого cursor."""

import asyncio
import json
import os
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.application.collector import Collector
from app.infrastructure.database.models import Base
from app.infrastructure.parsers import pages


@pytest.mark.postgres
async def test_migration_keeps_existing_job_companies_limiter_and_cursor(postgres):
    """DDL только добавляет поля; старые контакты и все контрольные точки сохраняются."""
    engine, repository = postgres  # Fixture допускает только отдельную БД leaders_test.
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    environment = os.environ | {"DATABASE_URL": os.environ["TEST_DATABASE_URL"], "PYTHONUTF8": "1"}

    async def migrate(revision):
        """Запускает штатный Alembic в отдельном процессе, как при Docker-деплое."""
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", "upgrade", revision],
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        assert result.returncode == 0, result.stderr

    await migrate("0001")
    job_id = str(uuid4())
    cursor = "https://leaders-project.ru/api/v1/companies?role=designer&sort=rating&dir=desc&legalStatus=active&limit=100&cursor=saved-opaque-cursor"
    source_url = "https://leaders-project.ru/companies/projecting?size=100"
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO jobs(id,slot,status,stage,created_at,next_url,pages,expected_total,"
                "discovery_complete,duration_seconds,timed_companies) "
                "VALUES(:id,1,'paused','discovering',CURRENT_TIMESTAMP,:cursor,1,2,false,10,1)"
            ),
            {"id": job_id, "cursor": cursor},
        )
        await connection.execute(
            text(
                "INSERT INTO companies(id,job_id,detail_url,ordinal,name,inn,director,"
                "director_status,phones,emails,websites,detail_status,contacts_status,"
                "detail_attempts,contacts_attempts,inn_conflict,updated_at) "
                "VALUES(100,:id,'https://leaders-project.ru/companies/1000000000',"
                "1,'OLD NAME','1000000000','OLD DIRECTOR','ok',"
                "'[\"+70000000000\"]','[\"old@example.test\"]','[\"https://old.test\"]',"
                "'ok','ok',2,3,false,CURRENT_TIMESTAMP)"
            ),
            {"id": job_id},
        )
        await connection.execute(
            text("INSERT INTO catalog_pages(job_id,url,fingerprint) VALUES(:id,:url,:fingerprint)"),
            {"id": job_id, "url": source_url, "fingerprint": "a" * 64},
        )
        await connection.execute(
            text(
                "INSERT INTO worker_state(id,heartbeat,next_request_at) "
                "VALUES(1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP + INTERVAL '1 hour')"
            )
        )
        before_job = (
            (await connection.execute(text("SELECT * FROM jobs WHERE id=:id"), {"id": job_id}))
            .mappings()
            .one()
        )
        before_company = (
            (await connection.execute(text("SELECT * FROM companies WHERE id=100")))
            .mappings()
            .one()
        )
        limiter = await connection.scalar(
            text("SELECT next_request_at FROM worker_state WHERE id=1")
        )
    await migrate("head")
    async with engine.connect() as connection:
        after_job = (
            (await connection.execute(text("SELECT * FROM jobs WHERE id=:id"), {"id": job_id}))
            .mappings()
            .one()
        )
        after_company = (
            (await connection.execute(text("SELECT * FROM companies WHERE id=100")))
            .mappings()
            .one()
        )
        assert all(after_job[key] == value for key, value in before_job.items())
        assert all(after_company[key] == value for key, value in before_company.items())
        assert after_company["kpp"] == "" and after_company["revenue_mln"] is None
        assert after_company["kpp_status"] == after_company["revenue_status"] == "pending"
        assert (
            await connection.scalar(text("SELECT next_request_at FROM worker_state WHERE id=1"))
            == limiter
        )
        assert await connection.scalar(text("SELECT count(*) FROM catalog_pages")) == 1
    await repository.control(job_id, "resume")
    calls = []

    class Source:
        """Прерывает тест при попытке начать индексирование с первой страницы."""

        async def preflight(self):
            """Проверка доступа не обращается к сайту в интеграционном тесте."""

        async def load(self, url, stage):
            """Отдаёт только страницу сохранённого cursor."""
            calls.append(url)
            assert url == cursor and stage == "discovering"
            return json.dumps(
                {
                    "items": [
                        {
                            "name": "NEW COMPANY",
                            "inn": "1000000001",
                            "url": "/companies/1000000001",
                            "revenue": 0,
                        }
                    ],
                    "nextCursor": None,
                    "total": 2,
                }
            ), 1

    async def checkpoint():
        """Разрешает только этот контролируемый шаг после resume."""

    await Collector(repository, Source(), pages, checkpoint).step(await repository.get_job(job_id))
    summary = await repository.summary(job_id)
    assert calls == [cursor] and summary["pages"] == 2 and summary["found"] == 2
    assert summary["discovery_complete"]
    old = next(
        row for row in (await repository.companies(job_id, 1, 25))["items"] if row["id"] == 100
    )
    assert old["phones"] == ["+70000000000"] and old["director"] == "OLD DIRECTOR"
    assert old["contacts_attempts"] == 3
