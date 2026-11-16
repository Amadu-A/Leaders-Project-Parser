# app/infrastructure/database/field_backfill.py
"""Транзакции дозаполнения новых полей, изолированные от исходного индекса."""

import hashlib

from sqlalchemy import func, or_, select, update

from app.domain.models import RUNNING, CatalogPage, StructureError, utcnow
from app.infrastructure.database.models import (
    Company,
    Issue,
    Job,
    PageCheckpoint,
    RevenueBackfillPage,
)


class FieldBackfillRepository:
    """Использует ту же фабрику sessions; не создаёт задания и не сбрасывает их cursor."""

    async def revenue_pending(self, job_id):
        """Считает только непроверенные новые поля, включая старые записи миграции."""
        async with self.sessions() as session:
            return await session.scalar(
                select(func.count())
                .select_from(Company)
                .where(Company.job_id == job_id, Company.revenue_status == "pending")
            )

    async def initial_catalogue_url(self, job_id):
        """Восстанавливает источник старого задания по первому сохранённому checkpoint."""
        async with self.sessions() as session:
            return await session.scalar(
                select(PageCheckpoint.url)
                .where(PageCheckpoint.job_id == job_id)
                .order_by(PageCheckpoint.id)
                .limit(1)
            )

    async def complete_revenue_backfill(self, job_id):
        """Завершает пустой проход, не меняя основной прогресс."""
        async with self.sessions.begin() as session:
            job = await session.get(Job, job_id, with_for_update=True)
            if job and job.status in RUNNING:
                job.revenue_backfill_complete = True
                job.revenue_backfill_next_url = None

    async def save_revenue_backfill(self, job_id: str, url: str, page: CatalogPage):
        """Обновляет только выручку уже существующих URL и отдельный checkpoint атомарно."""
        fingerprint = hashlib.sha256(
            "\n".join(c.detail_url for c in page.companies).encode()
        ).hexdigest()
        async with self.sessions.begin() as session:
            job = await session.get(Job, job_id, with_for_update=True)
            if not job or job.status not in RUNNING:
                return
            repeated = await session.scalar(
                select(RevenueBackfillPage.id).where(
                    RevenueBackfillPage.job_id == job_id,
                    or_(
                        RevenueBackfillPage.url == url,
                        RevenueBackfillPage.fingerprint == fingerprint,
                    ),
                )
            )
            if repeated:
                raise StructureError("Повтор страницы при дозаполнении выручки")
            by_url = {item.detail_url: item for item in page.companies}
            rows = (
                await session.scalars(
                    select(Company).where(
                        Company.job_id == job_id,
                        Company.revenue_status == "pending",
                        Company.detail_url.in_(by_url),
                    )
                )
            ).all()
            for company in rows:
                company.revenue_mln = by_url[company.detail_url].revenue_mln
                company.revenue_status = "ok" if company.revenue_mln is not None else "missing"
                company.updated_at = utcnow()
            await session.flush()
            session.add(RevenueBackfillPage(job_id=job_id, url=url, fingerprint=fingerprint))
            job.revenue_backfill_pages += 1
            job.revenue_backfill_next_url = page.next_url
            job.last_success = (
                f"Выручка: дозаполнено {len(rows)} записей на странице {job.revenue_backfill_pages}"
            )
            await session.execute(
                update(Issue)
                .where(
                    Issue.job_id == job_id,
                    Issue.stage == "backfill_revenue",
                    Issue.url == url,
                    Issue.company_id.is_(None),
                )
                .values(resolved=True)
            )
            remaining = await session.scalar(
                select(func.count())
                .select_from(Company)
                .where(Company.job_id == job_id, Company.revenue_status == "pending")
            )
            if not remaining or page.next_url is None:
                job.revenue_backfill_complete = True
                job.revenue_backfill_next_url = None
                pending = (
                    (
                        await session.scalars(
                            select(Company).where(
                                Company.job_id == job_id, Company.revenue_status == "pending"
                            )
                        )
                    ).all()
                    if remaining
                    else []
                )
                for company in pending:
                    company.revenue_status = "error"
                    session.add(
                        Issue(
                            job_id=job_id,
                            company_id=company.id,
                            url=company.detail_url,
                            stage="backfill_revenue",
                            kind="not_in_catalog",
                            message="Компания не найдена в текущем каталоге; выручка не проверена",
                        )
                    )
            if rows:
                await session.execute(
                    update(Issue)
                    .where(
                        Issue.company_id.in_([row.id for row in rows]),
                        Issue.stage == "backfill_revenue",
                    )
                    .values(resolved=True)
                )

    async def next_kpp_company(self, job_id):
        """Выбирает непроверенный КПП без изменения статусов карточки или контактов."""
        async with self.sessions() as session:
            row = await session.scalar(
                select(Company)
                .where(Company.job_id == job_id, Company.kpp_status == "pending")
                .order_by(Company.ordinal)
                .limit(1)
            )
            return (
                {column.key: getattr(row, column.key) for column in row.__table__.columns}
                if row
                else None
            )

    async def save_kpp(self, company_id: int, kpp: str, attempts: int):
        """Записывает только КПП, статус нового поля и его отдельный счётчик попыток."""
        async with self.sessions.begin() as session:
            company = await session.get(Company, company_id, with_for_update=True)
            if not company:
                return
            company.kpp = kpp
            company.kpp_status = "ok" if kpp else "missing"
            company.kpp_attempts += attempts
            company.updated_at = utcnow()
            await session.execute(
                update(Issue)
                .where(Issue.company_id == company_id, Issue.stage == "kpp")
                .values(resolved=True)
            )
            job = await session.get(Job, company.job_id, with_for_update=True)
            job.last_success = f"Организация №{company.ordinal}: КПП дозаполнен"
