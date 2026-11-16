# app/application/collector.py
"""Последовательная оркестрация: полный индекс, затем независимые страницы компаний."""

from time import perf_counter

from app.application.backfill import FieldBackfill
from app.application.ports import CollectorStore, Parsers, Source
from app.domain.models import CatalogPage, Interrupted, SourceError, StructureError


class Collector:
    """Обрабатывает ровно один durable шаг; состояние не зависит от вкладки UI."""

    def __init__(
        self,
        repository: CollectorStore,
        source: Source,
        parsers: Parsers,
        checkpoint,
        probe_count: int = 0,
        catalogue_url: str | None = None,
    ):
        """Внедряет инфраструктуру через контракты и проверку пользовательской остановки."""
        self.repository, self.source, self.parsers = repository, source, parsers
        self.checkpoint = checkpoint
        self.preflight_done = set()
        self.probe_count = probe_count
        self.backfill = FieldBackfill(repository, source, parsers, checkpoint, catalogue_url)

    async def _probe(self, url: str) -> CatalogPage:
        """Проверяет 3 карточки до массового индекса; результаты пробы не выдаются за сбор."""
        html, _ = await self.source.load(url, "discovering")
        page = self.parsers.catalog(html, url)
        successful = 0
        for company in page.companies[:5]:
            for stage in ("detail", "contacts"):
                await self.checkpoint()
                target = company.detail_url + ("/contacts" if stage == "contacts" else "")
                try:
                    html, _ = await self.source.load(target, stage)
                    if stage == "detail":
                        self.parsers.detail(html, company.inn)
                    else:
                        self.parsers.contacts(html)
                except SourceError as error:
                    error.url = error.url or target
                    if error.status == 404:
                        break
                    raise
            else:
                successful += 1
            if successful >= min(self.probe_count, len(page.companies)):
                return page
        raise StructureError(
            "Не удалось проверить три пробные организации; массовый сбор не запущен"
        )

    async def step(self, job: dict) -> None:
        """Восстанавливает первый незавершённый шаг и сохраняет checkpoint атомарно."""
        job_id = job["id"]
        probe_page = None
        await self.checkpoint()
        if job_id not in self.preflight_done:
            await self.repository.set_stage(job_id, "preflight")
            try:
                await self.source.preflight()
                if self.probe_count and not job["pages"] and not job["discovery_complete"]:
                    probe_page = await self._probe(job["next_url"])
            except SourceError as error:
                await self.repository.record_issue(
                    job_id, None, error.url or job["next_url"] or "", "preflight", error
                )
                return
            self.preflight_done.add(job_id)
            await self.repository.resolve_preflight(job_id)
        if not job["discovery_complete"]:
            await self.repository.set_stage(job_id, "discovering")
            try:
                if probe_page is None:
                    html, _ = await self.source.load(job["next_url"], "discovering")
                    page = self.parsers.catalog(html, job["next_url"])
                else:
                    page = probe_page
                await self.checkpoint()
                await self.repository.save_page(job_id, job["next_url"], page)
            except SourceError as error:
                await self.repository.record_issue(
                    job_id, None, error.url or job["next_url"], "discovering", error
                )
            return
        if await self.backfill.revenue(job):
            return
        await self.repository.set_stage(job_id, "processing")
        company = await self.repository.next_company(job_id)
        if company is None:
            if await self.backfill.kpp(job):
                return
            await self.repository.finish(job_id)
            return
        started = perf_counter()
        for stage in ("detail", "contacts"):
            if company[f"{stage}_status"] != "pending":
                continue
            await self.checkpoint()
            url = company["detail_url"] + ("/contacts" if stage == "contacts" else "")
            try:
                html, attempts = await self.source.load(url, stage)
                result = (
                    self.parsers.detail(html, company["inn"])
                    if stage == "detail"
                    else self.parsers.contacts(html)
                )
                await self.checkpoint()
                await self.repository.save_result(company["id"], stage, result, attempts)
            except SourceError as error:
                await self.repository.record_issue(
                    job_id, company["id"], error.url or url, stage, error
                )
                if error.pause:
                    raise Interrupted from error
        await self.repository.record_duration(job_id, perf_counter() - started)
