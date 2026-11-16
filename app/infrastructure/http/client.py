# app/infrastructure/http/client.py
"""Одна HTTP-сессия, общий durable limiter, deadlines и ограниченные retries."""

import asyncio
import random
from datetime import UTC, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import httpx

from app.domain.models import SourceError, utcnow


def aware(value):
    """Нормализует timezone SQLite-fixture; PostgreSQL возвращает aware UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def retry_after(value: str | None, now=None) -> float:
    """Поддерживает Retry-After как секунды либо HTTP-date, без уменьшения паузы."""
    if not value:
        return 0
    try:
        return max(0, float(value))
    except ValueError:
        try:
            return max(0, (parsedate_to_datetime(value) - (now or utcnow())).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0


def check_response(status: int, body: str, attempts: int):
    """Останавливает сбор при access barriers и различает 404 от временных сбоев."""
    lowered = body.lower()
    if status in {401, 403}:
        raise SourceError("Доступ к источнику ограничен", "access", status, attempts, pause=True)
    if any(
        marker in lowered
        for marker in (
            "<title>just a moment",
            "cf-chl-",
            "g-recaptcha",
            'id="captcha"',
            "подтвердите, что вы человек",
            "проверка, что вы не робот",
        )
    ):
        raise SourceError("Источник запросил CAPTCHA", "captcha", status, attempts, pause=True)
    if status == 404:
        raise SourceError("Страница не найдена", "not_found", status, attempts)


class RateLimiter:
    """Сериализует каждый запрос, включая retries и browser subrequests."""

    def __init__(self, settings, repository, checkpoint, sleep=asyncio.sleep, now=utcnow):
        """Внедряет часы и interruptible checkpoint для воспроизводимых тестов."""
        self.settings, self.repository = settings, repository
        self.checkpoint, self.sleep, self.now = checkpoint, sleep, now
        self.lock = asyncio.Lock()

    async def wait(self, until):
        """Ожидает порциями, чтобы pause/cancel/SIGTERM не ждали конца cooldown."""
        while (remaining := (aware(until) - self.now()).total_seconds()) > 0:
            await self.checkpoint()
            await self.sleep(min(remaining, 1))
        await self.checkpoint()

    async def before(self):
        """Сохраняет резерв задержки до отправки: crash не обнуляет limiter."""
        await self.wait(await self.repository.limiter_time())
        await self.repository.save_limiter(
            self.now() + timedelta(seconds=self.settings.request_max_delay)
        )

    async def after(self):
        """Назначает случайный интервал после завершения любого ответа или сбоя."""
        delay = random.uniform(self.settings.request_min_delay, self.settings.request_max_delay)
        # Резерв before нужен при crash; завершённый запрос заменяет его настоящим jitter.
        # Cooldown/backoff записывается затем под тем же lock, поэтому он не сокращается.
        await self.repository.save_limiter(self.now() + timedelta(seconds=delay))

    async def postpone(self, seconds):
        """Продлевает глобальную сохранённую паузу с учётом Retry-After."""
        until = self.now() + timedelta(seconds=seconds)
        await self.repository.save_limiter(max(aware(await self.repository.limiter_time()), until))
        return until


class HttpClient:
    """Получает только исходный домен; redirect также проходит отдельный limiter."""

    def __init__(self, settings, limiter, cooldown, transport=None):
        """Создаёт одну переиспользуемую сессию с явными connect/read/pool timeout."""
        self.settings, self.limiter, self.cooldown = settings, limiter, cooldown
        self.client = httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent},
            follow_redirects=False,
            timeout=httpx.Timeout(
                connect=settings.http_connect_timeout,
                read=settings.http_read_timeout,
                write=settings.http_read_timeout,
                pool=settings.http_pool_timeout,
            ),
            limits=httpx.Limits(
                max_connections=1,
                max_keepalive_connections=1,
                keepalive_expiry=max(60, settings.request_max_delay + 5),
            ),
            transport=transport,
        )
        self.allowed = lambda url: True
        self.rate_limit_hits = 0
        self.blocked_error: SourceError | None = None
        self.request_count = 0

    def reset_access_gate(self):
        """Разрешает явный пользовательский retry, не обнуляя durable cooldown."""
        self.rate_limit_hits = 0
        self.blocked_error = None

    async def close(self):
        """Освобождает все keepalive-соединения при завершении worker."""
        await self.client.aclose()

    async def get(self, url: str, redirects: int = 0, headers=None) -> tuple[httpx.Response, int]:
        """Сохраняет URL фактического сбоя для checkpoint и отчёта без HTML/payload."""
        try:
            return await self._request_get(url, redirects, headers)
        except SourceError as error:
            error.url = error.url or url
            raise

    async def _request_get(self, url: str, redirects: int = 0, headers=None):
        """Применяет общий deadline, bounded backoff и однократный cooldown 429."""
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.netloc != "leaders-project.ru" or not self.allowed(url):
            raise SourceError("URL запрещён политикой доступа источника", "robots", pause=True)
        response = None
        for attempt in range(1, self.settings.max_retries + 2):
            async with self.limiter.lock:
                if self.blocked_error:
                    original = self.blocked_error
                    blocked = SourceError(
                        str(original), original.kind, original.status, original.attempts, pause=True
                    )
                    blocked.url = original.url
                    raise blocked
                await self.limiter.before()
                try:
                    self.request_count += 1
                    async with asyncio.timeout(self.settings.http_total_timeout):
                        response = await self.client.get(url, headers=headers)
                except (httpx.RequestError, TimeoutError) as error:
                    if attempt > self.settings.max_retries:
                        raise SourceError(
                            "Исчерпаны попытки после сетевого сбоя", "network", attempts=attempt
                        ) from error
                    response = None
                finally:
                    await self.limiter.after()
                if response is not None:
                    body = (
                        response.text
                        if "text/html" in response.headers.get("content-type", "")
                        else ""
                    )
                    try:
                        check_response(response.status_code, body, attempt)
                    except SourceError as error:
                        if error.pause:
                            error.url = url
                            self.blocked_error = error
                        raise
                # Сохранить паузу нужно до освобождения lock: в очереди стоят JS-ресурсы.
                if response is not None and response.status_code == 429:
                    self.rate_limit_hits += 1
                    seconds = max(
                        self.settings.rate_limit_cooldown,
                        retry_after(response.headers.get("retry-after")),
                    )
                    until = await self.limiter.postpone(seconds)
                    if self.rate_limit_hits > 1 or attempt > self.settings.max_retries:
                        blocked = SourceError(
                            "HTTP 429: требуется проверка доступа",
                            "rate_limit",
                            429,
                            attempt,
                            pause=True,
                            retry_after=seconds,
                        )
                        blocked.url = url
                        self.blocked_error = blocked
                elif response is None or response.status_code == 408 or response.status_code >= 500:
                    await self.limiter.postpone(min(60, 2**attempt) + random.uniform(0, 1))
            if response is not None:
                if response.status_code == 429:
                    await self.cooldown(until)
                    if self.blocked_error:
                        raise self.blocked_error
                    continue
                if 300 <= response.status_code < 400:
                    location = urljoin(url, response.headers.get("location", ""))
                    if redirects >= 4 or location == url:
                        raise SourceError("Цикл перенаправлений", "redirect", pause=True)
                    if any(
                        path in urlsplit(location).path for path in ("/login", "/auth", "/register")
                    ):
                        raise SourceError("Источник требует входа", "login", pause=True)
                    return await self.get(location, redirects + 1, headers=headers)
                if response.status_code < 400:
                    return response, attempt
                if response.status_code not in {408} and response.status_code < 500:
                    raise SourceError(
                        "Ошибка ответа источника", "http", response.status_code, attempt
                    )
                if attempt > self.settings.max_retries:
                    raise SourceError(
                        "Исчерпаны попытки HTTP", "http", response.status_code, attempt
                    )
        raise AssertionError("unreachable")
