# scripts/deploy_checkpoint.py
"""Пауза и структурированный snapshot задания; совместим с образами до миграции 0002."""

import argparse
import asyncio
import json
from uuid import UUID

from sqlalchemy import select

from app.bootstrap import build_services
from app.config import Settings
from app.domain.models import RUNNING
from app.infrastructure.database.models import Job

KEYS = (
    "id",
    "status",
    "stage",
    "next_url",
    "pages",
    "found",
    "processed",
    "pending",
    "discovery_complete",
    "expected_total",
    "created_at",
    "started_at",
)


async def run(args):
    """Работает внутри существующего API-контейнера, используя его credentials БД."""
    engine, repository, _ = build_services(Settings())
    try:
        job_id = str(UUID(args.job_id)) if args.job_id else None
        if job_id is None:
            async with repository.sessions() as session:
                job_id = await session.scalar(select(Job.id).where(Job.slot == 1))
        if job_id is None:
            raise SystemExit(
                "Нет незавершённого задания; остановитесь и проверьте выбранный проект"
            )
        job = await repository.get_job(job_id)
        if args.action == "pause":
            if job["status"] not in RUNNING | {"paused"}:
                raise SystemExit("Задание уже завершено; автоматическое создание нового запрещено")
            await repository.control(job_id, "pause")
        summary = await repository.summary(job_id)
        if (args.action == "pause" or args.require_paused) and summary["status"] != "paused":
            raise SystemExit("Не подтверждена пауза задания")
        print(
            job_id
            if args.print_id
            else json.dumps({key: summary[key] for key in KEYS}, ensure_ascii=False, default=str)
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["pause", "snapshot"])
    parser.add_argument("--job-id")
    parser.add_argument("--print-id", action="store_true")
    parser.add_argument("--require-paused", action="store_true")
    asyncio.run(run(parser.parse_args()))
