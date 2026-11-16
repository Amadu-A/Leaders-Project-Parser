# app/worker.py
"""Отдельный durable worker с PostgreSQL polling, advisory lock и SIGTERM."""

import asyncio
import logging
import signal

from app.application.collector import Collector
from app.bootstrap import build_services
from app.config import Settings
from app.domain.models import RUNNING, Interrupted, NotFound, SourceError
from app.infrastructure.database.connection import ping, worker_lock
from app.infrastructure.http.client import HttpClient, RateLimiter
from app.infrastructure.http.source import PublicSource
from app.infrastructure.parsers import pages
from app.observability import configure_logging


async def run():
    """Продолжает checkpoint автоматически после рестарта, сохраняя manual pause."""
    settings = Settings()
    configure_logging(settings.log_level)
    engine, repository, _ = build_services(settings)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stopping.set)
        except NotImplementedError:
            signal.signal(signum, lambda *_: loop.call_soon_threadsafe(stopping.set))
    current = None
    source = None
    try:
        async with worker_lock(engine) as lock:
            if lock is None:
                logging.warning("worker_already_running")
                return
            lock_check = asyncio.Lock()

            async def checkpoint():
                """Проверяет владение lock и управляющее состояние перед каждым запросом."""
                if stopping.is_set():
                    raise Interrupted
                if current is None:
                    raise Interrupted
                async with lock_check:
                    await ping(lock)
                if current:
                    try:
                        job = await repository.get_job(current)
                    except NotFound as error:
                        raise Interrupted from error
                    if job["status"] not in RUNNING:
                        raise Interrupted

            async def cooldown(until):
                """Отражает ожидание источника в durable статусе задания."""
                if current:
                    await repository.cooldown(current, until)

            limiter = RateLimiter(settings, repository, checkpoint)
            source = PublicSource(settings, HttpClient(settings, limiter, cooldown))
            collector = Collector(repository, source, pages, checkpoint, probe_count=3)

            async def heartbeat():
                """Поддерживает healthcheck во время долгих пауз, не делает запросов к сайту."""
                while not stopping.is_set():
                    await repository.heartbeat()
                    try:
                        await asyncio.wait_for(stopping.wait(), 5)
                    except TimeoutError:
                        pass

            await repository.heartbeat()
            async with asyncio.TaskGroup() as group:
                group.create_task(heartbeat())
                cleanup_tick = 0
                while not stopping.is_set():
                    if cleanup_tick % 120 == 0:
                        await repository.cleanup(settings.retention_days)
                    cleanup_tick += 1
                    job = await repository.active()
                    if job and current and current != job["id"]:
                        await source.idle()
                    current = job["id"] if job else None
                    if job:
                        source.prepare_job(job["status"] == "queued", bool(job["cooldown_until"]))
                        try:
                            await collector.step(job)
                        except Interrupted:
                            pass
                        except Exception:
                            logging.exception("worker_operation_failed", extra={"job_id": current})
                            await repository.record_issue(
                                current,
                                None,
                                job["next_url"] or "",
                                job["stage"],
                                SourceError(
                                    "Внутренняя ошибка worker; проверьте логи",
                                    "internal",
                                    pause=True,
                                ),
                            )
                    else:
                        await source.idle()
                        try:
                            await asyncio.wait_for(stopping.wait(), settings.status_poll_interval)
                        except TimeoutError:
                            pass
    finally:
        if source:
            await source.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
