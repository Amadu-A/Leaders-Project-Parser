# app/infrastructure/database/repository.py
"""Короткие атомарные транзакции checkpoints и управления заданиями."""

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.models import (
    RUNNING,
    SUCCESS,
    TERMINAL,
    CatalogPage,
    Conflict,
    Contacts,
    Detail,
    NotFound,
    SourceError,
    StructureError,
    utcnow,
)
from app.infrastructure.database.models import (
    Company,
    Event,
    Issue,
    Job,
    PageCheckpoint,
    WorkerState,
)


def snapshot(row) -> dict:
    """Отделяет данные от ORM session для application и transport слоёв."""
    return {column.key: getattr(row, column.key) for column in row.__table__.columns}


class Repository:
    """Хранит результаты и обеспечивает идемпотентность на уровне БД."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        """Принимает фабрику sessions из composition root."""
        self.sessions = sessions

    async def ready(self) -> bool:
        """Проверяет подключение и наличие миграции, не создавая таблицы."""
        async with self.sessions() as session:
            await session.execute(select(Job.id).limit(1))
        return True

    async def create_job(self, source_url: str) -> dict:
        """Атомарно создаёт одно задание либо возвращает существующий active slot."""
        try:
            async with self.sessions.begin() as session:
                current = await session.scalar(select(Job).where(Job.slot == 1))
                if current:
                    return snapshot(current)
                job = Job(id=str(uuid4()), next_url=source_url)
                session.add(job)
                await session.flush()
                session.add(Event(job_id=job.id, message="Задание создано"))
                return snapshot(job)
        except IntegrityError:
            async with self.sessions() as session:
                return snapshot(await session.scalar(select(Job).where(Job.slot == 1)))

    async def latest(self) -> dict | None:
        """Восстанавливает выбранное задание после перезагрузки вкладки."""
        async with self.sessions() as session:
            row = await session.scalar(select(Job).order_by(Job.created_at.desc()).limit(1))
            return snapshot(row) if row else None

    async def get_job(self, job_id: str) -> dict:
        """Возвращает checkpoint либо предметную ошибку отсутствующего задания."""
        async with self.sessions() as session:
            job = await session.get(Job, job_id)
            if not job:
                raise NotFound("Задание не найдено")
            return snapshot(job)

    async def active(self) -> dict | None:
        """Выбирает единственный runnable checkpoint; paused не запускается сам."""
        async with self.sessions() as session:
            job = await session.scalar(select(Job).where(Job.status.in_(RUNNING), Job.slot == 1))
            return snapshot(job) if job else None

    async def control(self, job_id: str, action: str) -> dict:
        """Сериализует pause/resume/cancel/retry и защищает переходы unique slot."""
        try:
            async with self.sessions.begin() as session:
                job = await session.get(Job, job_id, with_for_update=True)
                if not job:
                    raise NotFound("Задание не найдено")
                if action == "pause" and job.status in RUNNING:
                    job.status = "paused"
                elif action == "resume" and job.status == "paused":
                    job.status = "queued"
                elif action == "cancel" and job.status not in TERMINAL:
                    job.status, job.slot, job.finished_at = "cancelled", None, utcnow()
                elif action == "retry-failed":
                    if job.status in {"completed_with_errors", "failed"}:
                        job.slot, job.status, job.finished_at = 1, "queued", None
                        for field in (Company.detail_status, Company.contacts_status):
                            await session.execute(
                                update(Company)
                                .where(Company.job_id == job_id, field.in_(["error", "not_found"]))
                                .values({field.key: "pending"})
                            )
                    elif job.status not in RUNNING:
                        raise Conflict("Повтор ошибок доступен после завершения")
                elif action not in {"pause", "resume", "cancel"}:
                    raise Conflict("Неизвестное действие")
                session.add(
                    Event(
                        job_id=job_id,
                        message={
                            "pause": "Запрошена пауза",
                            "resume": "Продолжение задания",
                            "cancel": "Задание остановлено",
                            "retry-failed": "Повтор ошибок запрошен",
                        }[action],
                    )
                )
                return snapshot(job)
        except IntegrityError as error:
            raise Conflict("Уже существует другое незавершённое задание") from error

    async def set_stage(self, job_id: str, stage: str) -> None:
        """Меняет этап только runnable-задания, сохраняя пользовательскую паузу."""
        async with self.sessions.begin() as session:
            job = await session.get(Job, job_id, with_for_update=True)
            if not job or job.status not in RUNNING:
                return
            job.stage = stage
            until = job.cooldown_until
            if until and (until if until.tzinfo else until.replace(tzinfo=UTC)) > utcnow():
                job.status = "cooldown"
            else:
                job.status = (
                    "discovering" if stage in {"preflight", "discovering"} else "processing"
                )
                job.cooldown_until = None
            job.started_at = job.started_at or utcnow()

    async def resolve_preflight(self, job_id: str) -> None:
        """Снимает старые ошибки доступа после успешной повторной проверки источника."""
        async with self.sessions.begin() as session:
            await session.execute(
                update(Issue)
                .where(Issue.job_id == job_id, Issue.stage == "preflight")
                .values(resolved=True)
            )

    async def save_page(self, job_id: str, url: str, page: CatalogPage) -> None:
        """Сохраняет весь список страницы и next checkpoint в одной транзакции."""
        fingerprint = hashlib.sha256(
            "\n".join(c.detail_url for c in page.companies).encode()
        ).hexdigest()
        async with self.sessions.begin() as session:
            job = await session.get(Job, job_id, with_for_update=True)
            if not job:
                return
            checkpoint = await session.scalar(
                select(PageCheckpoint).where(
                    PageCheckpoint.job_id == job_id, PageCheckpoint.url == url
                )
            )
            if checkpoint:
                return
            repeated = await session.scalar(
                select(PageCheckpoint).where(
                    PageCheckpoint.job_id == job_id, PageCheckpoint.fingerprint == fingerprint
                )
            )
            if repeated:
                raise StructureError("Повтор содержимого каталога: возможен цикл пагинации")
            known = set(
                (
                    await session.scalars(
                        select(Company.detail_url).where(Company.job_id == job_id)
                    )
                ).all()
            )
            ordinal = (
                await session.scalar(
                    select(func.max(Company.ordinal)).where(Company.job_id == job_id)
                )
                or 0
            )
            for item in page.companies:
                if item.detail_url in known:
                    continue
                ordinal += 1
                conflicts = (
                    await session.scalars(
                        select(Company).where(Company.job_id == job_id, Company.inn == item.inn)
                    )
                ).all()
                company = Company(
                    job_id=job_id,
                    ordinal=ordinal,
                    name=item.name,
                    inn=item.inn,
                    detail_url=item.detail_url,
                    inn_conflict=bool(conflicts),
                )
                session.add(company)
                await session.flush()
                if conflicts:
                    for other in conflicts:
                        other.inn_conflict = True
                    session.add(
                        Issue(
                            job_id=job_id,
                            company_id=company.id,
                            url=item.detail_url,
                            stage="discovering",
                            kind="inn_conflict",
                            message="Разные URL карточек с одинаковым ИНН",
                        )
                    )
                known.add(item.detail_url)
            session.add(PageCheckpoint(job_id=job_id, url=url, fingerprint=fingerprint))
            job.pages += 1
            job.next_url = page.next_url
            job.expected_total = page.total or job.expected_total
            job.last_success = f"Страница каталога {job.pages}: найдено {ordinal}"
            await session.execute(
                update(Issue)
                .where(Issue.job_id == job_id, Issue.url == url, Issue.kind != "inn_conflict")
                .values(resolved=True)
            )
            session.add(Event(job_id=job_id, message=job.last_success))
            if not page.next_url:
                if job.expected_total and ordinal != job.expected_total:
                    # Checkpoint сохраняется, но успешное окончание не подтверждается.
                    job.status = "paused"
                    job.last_error = (
                        f"Каталог сообщил {job.expected_total}, найдено {ordinal}; "
                        "окончание пагинации не подтверждено"
                    )
                    job.next_url = url
                    # Последняя страница будет проверена снова после исправления источника.
                    await session.execute(
                        delete(PageCheckpoint).where(
                            PageCheckpoint.job_id == job_id, PageCheckpoint.url == url
                        )
                    )
                    session.add(
                        Issue(
                            job_id=job_id,
                            url=url,
                            stage="discovering",
                            kind="pagination",
                            message=job.last_error,
                        )
                    )
                else:
                    job.discovery_complete, job.stage = True, "processing"

    async def next_company(self, job_id: str) -> dict | None:
        """Находит первую незавершённую операцию без повторов успешных страниц."""
        async with self.sessions() as session:
            row = await session.scalar(
                select(Company)
                .where(
                    Company.job_id == job_id,
                    or_(Company.detail_status == "pending", Company.contacts_status == "pending"),
                )
                .order_by(Company.ordinal)
                .limit(1)
            )
            return snapshot(row) if row else None

    async def save_result(
        self, company_id: int, stage: str, result: Detail | Contacts, attempts: int = 1
    ) -> None:
        """Фиксирует результат одной страницы и разрешает относящиеся к ней ошибки."""
        async with self.sessions.begin() as session:
            company = await session.get(Company, company_id, with_for_update=True)
            if not company:
                return
            if stage == "detail":
                company.director, company.director_status = result.director, result.director_status
                company.detail_status = "ok"
                company.detail_attempts += attempts
            else:
                company.phones, company.emails = result.phones, result.emails
                company.websites, company.contacts_status = result.websites, result.status
                company.contacts_attempts += attempts
            company.updated_at = utcnow()
            await session.execute(
                update(Issue)
                .where(Issue.company_id == company_id, Issue.stage == stage)
                .values(resolved=True)
            )
            job = await session.get(Job, company.job_id, with_for_update=True)
            action = "карточка проверена" if stage == "detail" else "контакты проверены"
            job.last_success = f"Организация №{company.ordinal}: {action}"

    async def record_issue(
        self, job_id: str, company_id: int | None, url: str, stage: str, error: SourceError
    ) -> None:
        """Сохраняет сбой; блокировка доступа оставляет операцию pending для resume."""
        async with self.sessions.begin() as session:
            job = await session.get(Job, job_id, with_for_update=True)
            if not job:
                return
            issue = await session.scalar(
                select(Issue).where(
                    Issue.job_id == job_id,
                    Issue.url == url,
                    Issue.stage == stage,
                    Issue.kind == error.kind,
                    Issue.resolved.is_(False),
                )
            )
            if issue:
                issue.attempts += error.attempts
                issue.occurred_at = utcnow()
            else:
                session.add(
                    Issue(
                        job_id=job_id,
                        company_id=company_id,
                        url=url,
                        stage=stage,
                        kind=error.kind,
                        message=str(error),
                        http_status=error.status,
                        attempts=error.attempts,
                    )
                )
            job.last_error = str(error)
            if error.pause or company_id is None:
                if job.status in RUNNING:
                    job.status = "paused"
            if company_id and stage in {"detail", "contacts"}:
                company = await session.get(Company, company_id, with_for_update=True)
                if not error.pause:
                    setattr(
                        company, f"{stage}_status", "not_found" if error.status == 404 else "error"
                    )
                setattr(
                    company,
                    f"{stage}_attempts",
                    getattr(company, f"{stage}_attempts") + error.attempts,
                )
            label = {
                "preflight": "Проверка доступа",
                "discovering": "Каталог",
                "detail": "Карточка",
                "contacts": "Контакты",
            }.get(stage, "Проверка")
            session.add(Event(job_id=job_id, message=f"{label}: {error}"))

    async def cooldown(self, job_id: str, until: datetime) -> None:
        """Показывает глобальную паузу 429, не отменяя ручную остановку."""
        async with self.sessions.begin() as session:
            job = await session.get(Job, job_id, with_for_update=True)
            if job and job.status in RUNNING:
                job.status, job.cooldown_until = "cooldown", until
                session.add(Event(job_id=job_id, message="HTTP 429: глобальное ожидание"))

    async def record_duration(self, job_id: str, seconds: float) -> None:
        """Обновляет ETA только по законченным попыткам обработки организаций."""
        async with self.sessions.begin() as session:
            await session.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(
                    duration_seconds=Job.duration_seconds + seconds,
                    timed_companies=Job.timed_companies + 1,
                )
            )

    async def finish(self, job_id: str) -> None:
        """Зелёный результат возможен лишь после полного списка без открытых ошибок."""
        async with self.sessions.begin() as session:
            job = await session.get(Job, job_id, with_for_update=True)
            if not job or job.status not in RUNNING or not job.discovery_complete:
                return
            pending = await session.scalar(
                select(func.count())
                .select_from(Company)
                .where(
                    Company.job_id == job_id,
                    or_(Company.detail_status == "pending", Company.contacts_status == "pending"),
                )
            )
            if pending:
                return
            errors = await session.scalar(
                select(func.count())
                .select_from(Issue)
                .where(Issue.job_id == job_id, Issue.resolved.is_(False))
            )
            job.status = "completed_with_errors" if errors else "completed"
            job.finished_at, job.slot = utcnow(), None
            session.add(
                Event(
                    job_id=job_id, message=("Завершено с ошибками" if errors else "Сбор завершён")
                )
            )

    async def summary(self, job_id: str) -> dict:
        """Считает достоверный прогресс на сервере без загрузки всех компаний."""
        job = await self.get_job(job_id)
        async with self.sessions() as session:
            condition = Company.job_id == job_id
            total = await session.scalar(select(func.count()).select_from(Company).where(condition))
            done_filter = and_(
                Company.detail_status.in_(SUCCESS),
                Company.contacts_status.in_(SUCCESS),
                Company.inn_conflict.is_(False),
            )
            done = await session.scalar(
                select(func.count()).select_from(Company).where(condition, done_filter)
            )
            pending = await session.scalar(
                select(func.count())
                .select_from(Company)
                .where(
                    condition,
                    or_(Company.detail_status == "pending", Company.contacts_status == "pending"),
                )
            )
            errors = await session.scalar(
                select(func.count())
                .select_from(Issue)
                .where(Issue.job_id == job_id, Issue.resolved.is_(False))
            )
            events = (
                await session.scalars(
                    select(Event).where(Event.job_id == job_id).order_by(Event.id.desc()).limit(30)
                )
            ).all()
            avg = (
                job["duration_seconds"] / job["timed_companies"] if job["timed_companies"] else None
            )
            return job | {
                "found": total,
                "processed": done,
                "pending": pending,
                "remaining": total - done,
                "errors": errors,
                "average_seconds": avg,
                "eta_seconds": avg * pending if avg and job["timed_companies"] >= 5 else None,
                "events": [snapshot(event) for event in events],
            }

    async def companies(
        self, job_id: str, page: int, size: int, query: str = "", errors_only: bool = False
    ) -> dict:
        """Серверная пагинация с экранированным поиском по названию и ИНН."""
        await self.get_job(job_id)
        async with self.sessions() as session:
            criteria = [Company.job_id == job_id]
            if query:
                pattern = (
                    "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                )
                criteria.append(
                    or_(
                        Company.name.ilike(pattern, escape="\\"),
                        Company.inn.ilike(pattern, escape="\\"),
                    )
                )
            if errors_only:
                criteria.append(
                    or_(
                        Company.detail_status.in_(["error", "not_found"]),
                        Company.contacts_status.in_(["error", "not_found"]),
                        Company.inn_conflict.is_(True),
                        Company.id.in_(
                            select(Issue.company_id).where(
                                Issue.job_id == job_id,
                                Issue.resolved.is_(False),
                                Issue.company_id.is_not(None),
                            )
                        ),
                    )
                )
            total = await session.scalar(select(func.count()).select_from(Company).where(*criteria))
            rows = (
                await session.scalars(
                    select(Company)
                    .where(*criteria)
                    .order_by(Company.updated_at.desc(), Company.ordinal)
                    .offset((page - 1) * size)
                    .limit(size)
                )
            ).all()
            items = [snapshot(row) for row in rows]
            ids = [row.id for row in rows]
            issues = (
                (
                    await session.scalars(
                        select(Issue).where(Issue.company_id.in_(ids), Issue.resolved.is_(False))
                    )
                ).all()
                if ids
                else []
            )
            for item in items:
                item["issues"] = [
                    snapshot(issue) for issue in issues if issue.company_id == item["id"]
                ]
            return {"items": items, "total": total, "page": page, "size": size}

    async def export_rows(self, job_id: str) -> AsyncIterator[dict]:
        """Потоково читает согласованный snapshot; готовых XLSX в БД нет."""
        async with self.sessions() as session:
            if session.bind.dialect.name == "postgresql":
                await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            stream = await session.stream_scalars(
                select(Company)
                .where(Company.job_id == job_id)
                .order_by(Company.ordinal)
                .execution_options(yield_per=500)
            )
            async for row in stream:
                yield snapshot(row)

    async def issues(self, job_id: str) -> list[dict]:
        """Возвращает нерешённые ошибки, включая ошибки каталога без company_id."""
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(Issue)
                    .where(Issue.job_id == job_id, Issue.resolved.is_(False))
                    .order_by(Issue.id)
                )
            ).all()
            return [snapshot(row) for row in rows]

    async def delete_job(self, job_id: str) -> None:
        """Удаляет данные только терминального задания с каскадами PostgreSQL."""
        async with self.sessions.begin() as session:
            job = await session.get(Job, job_id, with_for_update=True)
            if not job:
                return
            if job.status not in TERMINAL:
                raise Conflict("Сначала остановите задание")
            await session.delete(job)

    async def cleanup(self, days: int) -> None:
        """Чистит только завершённые задания и ограничивает журнал 200 событиями."""
        async with self.sessions.begin() as session:
            await session.execute(
                delete(Job).where(
                    Job.status.in_(TERMINAL), Job.finished_at < utcnow() - timedelta(days=days)
                )
            )
            jobs = (await session.scalars(select(Job.id))).all()
            for job_id in jobs:
                keep = (
                    select(Event.id)
                    .where(Event.job_id == job_id)
                    .order_by(Event.id.desc())
                    .limit(200)
                )
                await session.execute(
                    delete(Event).where(Event.job_id == job_id, Event.id.not_in(keep))
                )

    async def limiter_time(self) -> datetime:
        """Читает сохранённую границу следующего запроса после рестарта."""
        async with self.sessions() as session:
            state = await session.get(WorkerState, 1)
            return state.next_request_at if state else utcnow()

    async def save_limiter(self, next_at: datetime) -> None:
        """Сохраняет limiter глобально; единственный worker владеет advisory lock."""
        async with self.sessions.begin() as session:
            state = await session.get(WorkerState, 1)
            if state:
                state.next_request_at = next_at
            else:
                session.add(WorkerState(id=1, next_request_at=next_at))

    async def heartbeat(self) -> None:
        """Обновляет readiness worker независимо от ожидания rate limiter."""
        async with self.sessions.begin() as session:
            state = await session.get(WorkerState, 1)
            if state:
                state.heartbeat = utcnow()
            else:
                session.add(WorkerState(id=1))
