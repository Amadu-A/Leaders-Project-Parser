# tests/test_http.py
"""Детерминированные проверки retries, deadlines, общего limiter и ограничений."""

import asyncio
from datetime import timedelta
from email.utils import format_datetime

import httpx
import pytest

from app.config import Settings
from app.domain.models import Interrupted, SourceError, utcnow
from app.infrastructure.http.client import HttpClient, RateLimiter, retry_after


class ClockStore:
    """Фальшивые часы и durable-время limiter без ожидания wall clock."""

    def __init__(self):
        """Создаёт общий store, который можно передать новому limiter после сбоя."""
        self.now = utcnow()
        self.next_at = self.now
        self.calls = []
        self.cooldowns = []
        self.interrupt = False

    async def sleep(self, seconds):
        """Продвигает часы вместо реального ожидания."""
        self.now += timedelta(seconds=seconds)

    async def limiter_time(self):
        """Возвращает сохранённую границу всех запросов."""
        return self.next_at

    async def save_limiter(self, value):
        """Имитирует сохранение limiter между экземплярами клиента."""
        self.next_at = value

    async def checkpoint(self):
        """Прерывает ожидание при пользовательской паузе."""
        if self.interrupt:
            raise Interrupted

    async def cooldown(self, until):
        """Регистрирует глобальный cooldown для проверки UI-состояния."""
        self.cooldowns.append(until)


def make_client(store, handler, **overrides):
    """Внедряет transport и часы в тот же клиент, который использует worker."""
    settings = Settings(_env_file=None, request_min_delay=7, request_max_delay=12, **overrides)
    limiter = RateLimiter(
        settings, store, store.checkpoint, sleep=store.sleep, now=lambda: store.now
    )

    async def transport(request):
        """Запоминает время каждого фактического запроса, включая retries."""
        store.calls.append(store.now)
        return await handler(request)

    return HttpClient(settings, limiter, store.cooldown, httpx.MockTransport(transport))


@pytest.mark.parametrize(
    "status,expected,attempts",
    [(403, "access", 1), (404, "not_found", 1), (500, "http", 4), (408, "http", 4)],
)
async def test_http_failures(status, expected, attempts):
    """403/404 не повторяются, временные ошибки имеют ограниченное число retries."""
    store = ClockStore()

    async def respond(request):
        """Возвращает заданный HTTP-статус без реального сайта."""
        return httpx.Response(status, text="error")

    client = make_client(store, respond)
    try:
        with pytest.raises(SourceError) as caught:
            await client.get("https://leaders-project.ru/companies/1000000000")
        assert caught.value.kind == expected
        assert caught.value.attempts == attempts
        assert len(store.calls) == attempts
        assert all(
            (b - a).total_seconds() >= 7 for a, b in zip(store.calls, store.calls[1:], strict=False)
        )
    finally:
        await client.close()


async def test_retry_after_global_and_restart():
    """Повторный 429 останавливает сбор; cooldown переживает создание нового клиента."""
    store = ClockStore()

    async def respond(request):
        """Имитирует более строгий Retry-After, чем стартовый cooldown."""
        return httpx.Response(429, headers={"Retry-After": "2400"})

    client = make_client(store, respond)
    try:
        with pytest.raises(SourceError) as caught:
            await client.get("https://leaders-project.ru/companies/1000000000")
        assert caught.value.pause and len(store.calls) == 2
        assert (store.calls[1] - store.calls[0]).total_seconds() >= 2400
        assert len(store.cooldowns) == 2
    finally:
        await client.close()
    now = store.now

    async def success(request):
        """Возвращает успешный ответ после перезапуска."""
        return httpx.Response(200)

    client = make_client(store, success)
    try:
        await client.get("https://leaders-project.ru/companies/1000000001")
        assert (store.calls[-1] - now).total_seconds() >= 2400
    finally:
        await client.close()


async def test_network_timeout_and_captcha():
    """Сетевая ошибка проходит limiter, CAPTCHA требует ручного продолжения."""
    store = ClockStore()

    async def timeout(request):
        """Симулирует connect/read timeout httpx."""
        raise httpx.ReadTimeout("timeout")

    client = make_client(store, timeout, max_retries=1)
    try:
        with pytest.raises(SourceError, match="сетевого"):
            await client.get("https://leaders-project.ru/companies/1000000000")
        assert len(store.calls) == 2
    finally:
        await client.close()

    async def captcha(request):
        """Возвращает challenge вместо HTML компании."""
        return httpx.Response(
            200, text='<html><div id="captcha"></div></html>', headers={"content-type": "text/html"}
        )

    client = make_client(store, captcha)
    try:
        with pytest.raises(SourceError) as caught:
            await client.get("https://leaders-project.ru/companies/1000000000")
        assert caught.value.kind == "captcha" and caught.value.pause
    finally:
        await client.close()


async def test_total_deadline_and_interrupt():
    """Общий deadline ограничивает зависший запрос; pause прерывает даже cooldown."""
    store = ClockStore()

    async def hang(request):
        """Имитирует ответ, который не заканчивается в общем deadline."""
        await asyncio.sleep(1)
        return httpx.Response(200)

    client = make_client(store, hang, max_retries=0, http_total_timeout=0.01)
    try:
        with pytest.raises(SourceError):
            await client.get("https://leaders-project.ru/companies/1000000000")
        store.interrupt = True
        with pytest.raises(Interrupted):
            await client.get("https://leaders-project.ru/companies/1000000000")
        assert len(store.calls) == 1
    finally:
        await client.close()


def test_retry_after_formats():
    """Разбирает секунды и абсолютную дату без отрицательных ожиданий."""
    now = utcnow().replace(microsecond=0)
    assert retry_after("19") == 19
    assert retry_after(format_datetime(now + timedelta(seconds=90)), now) == 90
    assert retry_after("invalid") == 0


async def test_redirects_are_limited_and_rate_limited():
    """Каждый redirect — отдельный запрос с паузой; переход на login требует остановки."""
    store = ClockStore()

    async def respond(request):
        """Перенаправляет публичную страницу один раз, затем успешно отвечает."""
        if request.url.path.endswith("1000000000"):
            return httpx.Response(302, headers={"Location": "/companies/1000000001"})
        return httpx.Response(200)

    client = make_client(store, respond)
    try:
        response, _ = await client.get("https://leaders-project.ru/companies/1000000000")
        assert response.status_code == 200 and len(store.calls) == 2
        assert (store.calls[1] - store.calls[0]).total_seconds() >= 7
    finally:
        await client.close()


async def test_queued_browser_requests_cannot_erase_cooldown():
    """Ответ 429 останавливает уже ожидающие ресурсы до записи нового запроса."""

    class Store(ClockStore):
        """Имитирует реальные async round trips PostgreSQL между чтением и записью."""

        async def limiter_time(self):
            """Позволяет другому запросу войти в гонку при ошибочном unlock."""
            await asyncio.sleep(0)
            return self.next_at

        async def save_limiter(self, value):
            """Моделирует отложенную фиксацию durable limiter."""
            await asyncio.sleep(0)
            self.next_at = value

    store = Store()
    first = True

    async def respond(request):
        """Ограничивает один ресурс, пока второй уже ожидает общий lock."""
        nonlocal first
        await asyncio.sleep(0)
        if first:
            first = False
            return httpx.Response(429, headers={"Retry-After": "1800"})
        return httpx.Response(200)

    client = make_client(store, respond)
    try:
        await asyncio.gather(
            client.get("https://leaders-project.ru/companies/1000000000"),
            client.get("https://leaders-project.ru/companies/1000000001"),
        )
        assert len(store.calls) == 3
        assert all((value - store.calls[0]).total_seconds() >= 1800 for value in store.calls[1:])
    finally:
        await client.close()


async def test_repeated_429_on_different_resources_stops_entire_client():
    """Повторное ограничение другого ресурса также останавливает очередь, не только retry URL."""
    store = ClockStore()

    async def respond(request):
        """Каждый ресурс возвращает 429, как при общей блокировке браузера."""
        await asyncio.sleep(0)
        return httpx.Response(429)

    client = make_client(store, respond)
    try:
        results = await asyncio.gather(
            client.get("https://leaders-project.ru/companies/1000000000"),
            client.get("https://leaders-project.ru/companies/1000000001"),
            return_exceptions=True,
        )
        assert len(store.calls) == 2
        assert all(isinstance(error, SourceError) and error.pause for error in results)
        saved = store.next_at
        client.reset_access_gate()
        assert client.blocked_error is None and store.next_at == saved
    finally:
        await client.close()


async def test_successful_request_uses_random_delay_not_crash_reservation(monkeypatch):
    """Семисекундный jitter не заменяется постоянным двенадцатисекундным резервом."""
    store = ClockStore()

    def fixed_delay(minimum, maximum):
        """Выбирает нижнюю границу диапазона для проверки наблюдаемого интервала."""
        return 7

    async def respond(request):
        """Успешно завершает запрос без затрат реального времени."""
        return httpx.Response(200)

    monkeypatch.setattr("app.infrastructure.http.client.random.uniform", fixed_delay)
    client = make_client(store, respond)
    try:
        await client.get("https://leaders-project.ru/companies/1000000000")
        await client.get("https://leaders-project.ru/companies/1000000001")
        assert (store.calls[1] - store.calls[0]).total_seconds() == 7
    finally:
        await client.close()
