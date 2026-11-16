# scripts/smoke.py
"""Ручной smoke 3 компаний с production limiter; ничего не пишет на диск."""

import argparse
import asyncio
import re
import sys
from urllib.parse import urlsplit

from playwright.async_api import Error as BrowserError

from app.config import Settings
from app.domain.models import Listing, SourceError, utcnow
from app.infrastructure.http.client import HttpClient, RateLimiter
from app.infrastructure.http.source import PublicSource
from app.infrastructure.parsers import pages


class MemoryLimiterStore:
    """Держит limiter smoke-процесса в RAM, без production PostgreSQL."""

    def __init__(self):
        """Задаёт начальное время единственного разрешённого потока."""
        self.next_at = utcnow()

    async def limiter_time(self):
        """Возвращает общую границу следующего запроса."""
        return self.next_at

    async def save_limiter(self, value):
        """Обновляет границу для HTML, ресурсов и всех повторов."""
        self.next_at = value


async def checkpoint():
    """Позволяет штатную отмену smoke через Ctrl+C без управляющего задания."""


async def cooldown(until):
    """Показывает причину продолжительного ожидания без данных компаний."""
    print("HTTP 429, ожидание до", until.isoformat(), flush=True)


async def catalogue(source, url):
    """Проверяет реальный переход между двумя непересекающимися списками."""
    print("Загрузка каталога с единым limiter", flush=True)
    html, _ = await source.load(url, "discovering")
    first = pages.catalog(html, url)
    print("Каталог:", len(first.companies), "общее число:", first.total, flush=True)
    if first.next_url:
        html, _ = await source.load(first.next_url, "discovering")
        print(
            "Переход второй страницы:",
            source.page.url if source.page else "серверный nextCursor публичного API",
            flush=True,
        )
        second = pages.catalog(html, first.next_url)
        assert not {c.detail_url for c in first.companies} & {
            c.detail_url for c in second.companies
        }
        print("Вторая страница:", len(second.companies), flush=True)
    return first.companies


async def main(inns=None):
    """Проверяет каталог и три пары страниц; --inn повторяет только выбранные карточки."""
    settings = Settings()
    limiter = RateLimiter(settings, MemoryLimiterStore(), checkpoint)
    source = PublicSource(settings, HttpClient(settings, limiter, cooldown))

    async def monitor():
        """Показывает счётчики DOM и запросов, не раскрывая значения полей и контакты."""
        previous = None
        while True:
            await asyncio.sleep(30)
            state = {"requests": source.request_count}
            if source.page and not source.page.is_closed():
                state["path"] = urlsplit(source.page.url).path
                try:
                    async with asyncio.timeout(3):
                        state.update(
                            await source.page.evaluate("""() => ({
                              requisites:document.querySelectorAll('section#a-requisites').length,
                              leaders:document.querySelectorAll('section#a-leaders').length,
                              contacts:document.querySelectorAll(
                                'section[aria-label="Как связаться с компанией"]').length
                            })""")
                        )
                except (TimeoutError, BrowserError):
                    pass
            if state != previous:
                print("Ход пробы:", state, flush=True)
                previous = state

    monitoring = asyncio.create_task(monitor())
    try:
        print("Проверка robots и условий", flush=True)
        await source.preflight()
        companies = (
            [Listing("", inn, "https://leaders-project.ru/companies/" + inn) for inn in inns]
            if inns
            else await catalogue(source, settings.source_url)
        )
        for index, company in enumerate(companies[:3], 1):
            print("Проверка организации", index, "— карточка", flush=True)
            html, _ = await source.load(company.detail_url, "detail")
            detail = pages.detail(html, company.inn)
            print("Проверка организации", index, "— контакты", flush=True)
            html, _ = await source.load(company.detail_url + "/contacts", "contacts")
            contacts = pages.contacts(html)
            print(
                "Результат",
                index,
                "руководитель:",
                detail.director_status,
                "контакты:",
                contacts.status,
                "число значений:",
                len(contacts.phones),
                len(contacts.emails),
                len(contacts.websites),
                flush=True,
            )
    except SourceError as error:
        print(
            "Проба прервана:",
            {
                "kind": error.kind,
                "http": error.status,
                "attempts": error.attempts,
                "url": error.url,
            },
            flush=True,
        )
        raise
    finally:
        monitoring.cancel()
        await asyncio.gather(monitoring, return_exceptions=True)
        await source.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inn", action="append", help="Повторить пробу трёх конкретных ИНН")
    arguments = parser.parse_args()
    if arguments.inn and (
        len(set(arguments.inn)) != 3
        or len(arguments.inn) != 3
        or any(not re.fullmatch(r"\d{10,12}", inn) for inn in arguments.inn)
    ):
        parser.error("Укажите ровно три разных ИНН, каждый из 10–12 цифр")
    asyncio.run(main(arguments.inn))
