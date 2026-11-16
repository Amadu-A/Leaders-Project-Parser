# app/application/backfill.py
"""Отдельное дозаполнение новых полей существующего задания через тот же source."""

from app.domain.models import Interrupted, SourceError, StructureError


class FieldBackfill:
    """Не меняет основной cursor, порядок компаний или результаты контактов."""

    def __init__(self, repository, source, parsers, checkpoint, catalogue_url=None):
        """Принимает те же зависимости и limiter, что использует основной Collector."""
        self.repository, self.source, self.parsers = repository, source, parsers
        self.checkpoint, self.catalogue_url = checkpoint, catalogue_url

    async def revenue(self, job):
        """После текущей индексации обновляет только старые непроверенные суммы."""
        if job["revenue_backfill_complete"]:
            return False
        job_id = job["id"]
        if not await self.repository.revenue_pending(job_id):
            await self.repository.complete_revenue_backfill(job_id)
            return False
        url = (
            job["revenue_backfill_next_url"]
            or job["source_url"]
            or await self.repository.initial_catalogue_url(job_id)
            or self.catalogue_url
        )
        await self.repository.set_stage(job_id, "backfill_revenue")
        try:
            if not url:
                raise StructureError("Не найден исходный URL для дозаполнения выручки")
            await self.checkpoint()
            raw, _ = await self.source.load(url, "discovering")
            page = self.parsers.catalog(raw, url)
            await self.checkpoint()
            await self.repository.save_revenue_backfill(job_id, url, page)
        except SourceError as error:
            await self.repository.record_issue(
                job_id, None, error.url or url or "", "backfill_revenue", error
            )
        return True

    async def kpp(self, job):
        """Запрашивает только реквизиты незаполненного КПП; контакты не обрабатываются."""
        company = await self.repository.next_kpp_company(job["id"])
        if company is None:
            return False
        await self.repository.set_stage(job["id"], "backfill_kpp")
        await self.checkpoint()
        try:
            raw, attempts = await self.source.load(company["detail_url"], "detail")
            value = self.parsers.kpp(raw, company["inn"])
            await self.checkpoint()
            await self.repository.save_kpp(company["id"], value, attempts)
        except SourceError as error:
            await self.repository.record_issue(
                job["id"], company["id"], error.url or company["detail_url"], "kpp", error
            )
            if error.pause:
                raise Interrupted from error
        return True
