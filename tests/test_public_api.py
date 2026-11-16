# tests/test_public_api.py
"""Проверенная JSON-схема, opaque pagination, нормализация и общий HTTP pipeline."""

import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.config import Settings
from app.domain.models import SourceError, StructureError
from app.infrastructure.http.client import HttpClient, RateLimiter
from app.infrastructure.http.source import PublicSource
from app.infrastructure.parsers import pages
from scripts.smoke import MemoryLimiterStore, checkpoint, cooldown

URL = "https://leaders-project.ru/companies/projecting?size=100"
CARD = "https://leaders-project.ru/companies/0012345678"


def company():
    """Создаёт синтетическую карточку: данные реальных компаний не сохраняются."""
    return {
        "inn": "0012345678",
        "leaders": [
            {"type": "individual", "name": "ИВАНОВ ИВАН", "position": "ГЕНЕРАЛЬНЫЙ ДИРЕКТОР"}
        ],
        "founders": [{"name": "НАЗВАНИЕ УЧРЕДИТЕЛЯ"}],
        "contacts": [
            {"type": "phone", "value": "+7 (800) 111-22-33"},
            {"type": "phone", "value": "+78001112233"},
            {"type": "fax", "value": "+7 800 222 33 44 доб. 12"},
            {"type": "email", "value": "FIRST@EXAMPLE.TEST"},
            {"type": "email", "value": "mailto:second@example.test?subject=hello"},
            {"type": "website", "value": "https://example.test"},
            {"type": "website", "value": "https://vk.com/example"},
            {"type": "office", "value": "/companies/0012345678/offices/1"},
        ],
        "website": "https://example.test",
    }


def listing(number):
    """Сохраняет ИНН строкой, URL и порядок поступления API items."""
    return {
        "name": f"Компания {number}",
        "inn": str(1000000000 + number),
        "url": f"/companies/{1000000000 + number}",
    }


@pytest.mark.parametrize("count", [1, 100, 351])
def test_api_catalogue_follows_opaque_cursor_and_keeps_last_page(count):
    """Собирает все страницы и не вычисляет непрозрачный cursor из числа записей."""
    current, offset, collected = URL, 0, []
    while current:
        end = min(offset + 100, count)
        cursor = f"opaque+/={end}" if end < count else None
        body = {
            "items": [listing(i) for i in range(offset, end)],
            "total": count,
            "nextCursor": cursor,
        }
        page = pages.catalog(json.dumps(body), current)
        collected.extend(page.companies)
        if page.next_url:
            assert parse_qs(urlsplit(page.next_url).query)["cursor"] == [cursor]
            assert "offset" not in parse_qs(urlsplit(page.next_url).query)
        current, offset = page.next_url, end
    assert len(collected) == count and len({item.detail_url for item in collected}) == count
    assert collected[-1].inn == str(1000000000 + count - 1)


def test_api_catalogue_duplicates_and_repeated_cursor():
    """Дедупликация внутри ответа не скрывает цикл в следующем cursor."""
    body = {"items": [listing(0), listing(0)], "total": 2, "nextCursor": "opaque"}
    first = pages.catalog(json.dumps(body), URL)
    assert len(first.companies) == 1
    with pytest.raises(StructureError, match="cursor"):
        pages.catalog(json.dumps(body), first.next_url)


@pytest.mark.parametrize(
    "body",
    [
        {"items": []},
        {"items": [], "total": 10, "nextCursor": None},
        {"items": [listing(0)], "total": 1, "nextCursor": ""},
    ],
)
def test_api_catalogue_structure_is_not_successful_emptiness(body):
    """Неполный ответ не превращается в зелёный пустой каталог."""
    with pytest.raises(StructureError):
        pages.catalog(json.dumps(body), URL)


def test_api_company_keeps_leaders_separate_from_founders_and_all_contacts():
    """Проверяет реальные типы полей на синтетических данных, включая добавочный."""
    body = json.dumps(company())
    detail = pages.detail(body, "0012345678")
    contacts = pages.contacts(body)
    assert detail.director == "ИВАНОВ ИВАН" and detail.inn == "0012345678"
    assert contacts.phones == ["+78001112233", "+78002223344;ext=12"]
    assert contacts.emails == ["first@example.test", "second@example.test"]
    assert contacts.websites == ["https://example.test"]


def test_api_missing_fields_differ_from_explicit_empty_data():
    """Пустые списки допустимы, пропавшие ключи требуют проверки структуры."""
    data = company()
    data.update(leaders=[], contacts=[], website=None)
    assert pages.detail(json.dumps(data), data["inn"]).director_status == "missing"
    assert pages.contacts(json.dumps(data)).status == "missing"
    del data["contacts"]
    with pytest.raises(StructureError):
        pages.contacts(json.dumps(data))
    with pytest.raises(SourceError, match="ИНН"):
        pages.detail(json.dumps(company()), "1111111111")


def test_api_explicit_leader_category_supports_other_management_titles():
    """Отдельная категория leaders не теряет явно указанного ликвидатора."""
    data = company()
    data["leaders"][0]["position"] = "ЛИКВИДАТОР"
    assert pages.detail(json.dumps(data), data["inn"]).director == "ИВАНОВ ИВАН"


@pytest.mark.parametrize(
    "change",
    [
        {"inn": 12345678},
        {"leaders": None},
        {"contacts": [{"type": "new_schema", "value": "unknown"}]},
    ],
)
def test_api_malformed_fields_pause_instead_of_losing_values(change):
    """Изменившиеся типы и новые enum не теряются молча при экспорте."""
    data = company() | change
    with pytest.raises(StructureError):
        if "contacts" in change:
            pages.contacts(json.dumps(data))
        else:
            pages.detail(json.dumps(data), "0012345678")


async def test_api_source_reuses_one_bundle_and_restores_pending_contacts():
    """Один GET даёт оба результата; idle и новый процесс не используют старый raw bundle."""
    calls = []

    async def respond(request):
        """Обслуживает только локальный MockTransport, без обращений к источнику."""
        calls.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\nDisallow: /api\n")
        if request.url.path == "/legal/terms":
            return httpx.Response(200, text="Public test terms")
        if request.url.path == "/api/v1/companies":
            cursor = request.url.params.get("cursor")
            return httpx.Response(
                200,
                json={
                    "items": [listing(1 if cursor else 0)],
                    "total": 2,
                    "nextCursor": None if cursor else "opaque+/=",
                },
            )
        return httpx.Response(200, json=company())

    def build():
        """Создаёт независимый source с production limiter и fake HTTP."""
        settings = Settings(_env_file=None, request_min_delay=0, request_max_delay=0)
        return PublicSource(
            settings,
            HttpClient(
                settings,
                RateLimiter(settings, MemoryLimiterStore(), checkpoint),
                cooldown,
                httpx.MockTransport(respond),
            ),
        )

    source = build()
    try:
        await source.preflight()
        first = pages.catalog((await source.load(URL, "discovering"))[0], URL)
        second = pages.catalog(
            (await source.load(first.next_url, "discovering"))[0], first.next_url
        )
        assert first.companies[0].inn != second.companies[0].inn and second.next_url is None
        detail, _ = await source.load(CARD, "detail")
        contacts, _ = await source.load(CARD + "/contacts", "contacts")
        assert detail == contacts
        assert sum("/api/v1/companies/0012345678" in target for target in calls) == 1
        await source.load(CARD, "detail")
        await source.idle()
        await source.load(CARD + "/contacts", "contacts")
        assert sum("/api/v1/companies/0012345678" in target for target in calls) == 3
        assert source.browser is None
    finally:
        await source.close()
    restored = build()
    try:
        await restored.preflight()
        assert pages.contacts((await restored.load(CARD + "/contacts", "contacts"))[0]).phones
        assert restored.browser is None
    finally:
        await restored.close()


async def test_api_access_barrier_never_falls_back_to_browser():
    """403 закрывает общий gate, не пытаясь получить те же данные обходным путём."""
    settings = Settings(_env_file=None, request_min_delay=0, request_max_delay=0)

    async def blocked(request):
        """Имитирует отказ доступа к публичному API."""
        return httpx.Response(403)

    source = PublicSource(
        settings,
        HttpClient(
            settings,
            RateLimiter(settings, MemoryLimiterStore(), checkpoint),
            cooldown,
            httpx.MockTransport(blocked),
        ),
    )
    try:
        with pytest.raises(SourceError) as error:
            await source.load(CARD, "detail")
        assert error.value.pause and source.browser is None
        assert source.request_count == 1
    finally:
        await source.close()
