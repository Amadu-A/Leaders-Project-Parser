# app/infrastructure/http/source.py
"""Публичные страницы: HTTP-first, один JS-browser и строгая проверка robots."""

import asyncio
import re
from contextlib import suppress
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
from playwright.async_api import Error as BrowserError
from playwright.async_api import async_playwright

from app.domain.models import SourceError, StructureError, utcnow
from app.infrastructure.http.client import aware, check_response
from app.infrastructure.source_urls import catalogue_api_url

COMPANY_LINKS_JS = """nodes => nodes.map(n => n.getAttribute('href')).filter(
    h => h && h.startsWith('/companies/') && /^[0-9]{10,12}$/.test(h.slice(11))
).join('|')"""
READY_JS = """() => [...document.querySelectorAll('a[href]')].some(n => {
    const h = n.getAttribute('href');
    return h && h.startsWith('/companies/') && /^[0-9]{10,12}$/.test(h.slice(11));
})"""
CHANGED_JS = """previous => {
    const value = [...document.querySelectorAll('a[href]')].map(n => n.getAttribute('href'))
      .filter(h => h && h.startsWith('/companies/') && /^[0-9]{10,12}$/.test(h.slice(11)))
      .join('|');
    return value && value !== previous;
}"""


class PublicSource:
    """Не обращается к закрытым путям API и не обходит ограничения источника."""

    def __init__(self, settings, http):
        """Получает общий HTTP-клиент; браузер создаётся лениво один раз."""
        self.settings, self.http = settings, http
        self.policy = None
        self.playwright = self.browser = self.context = self.page = None
        self.cache = {}
        self.route_error = None
        self.render = settings.source_mode == "browser"
        self.generation = 0
        self.document_path = urlsplit(settings.source_url).path
        self.company_cache = None

    @property
    def request_count(self):
        """Показывает фактические HTTP-попытки, включая API, redirect и retries."""
        return self.http.request_count

    def allowed(self, url):
        """Учитывает разрешение владельца только для публичных read-only API страницы."""
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.netloc != "leaders-project.ru":
            return False
        path = parts.path
        approved = self.settings.source_api_access_approved and (
            path == "/api/v1/companies"
            or path.startswith("/api/v1/companies/")
            or path in {"/api/v1/company-classifiers/current", "/api/v1/portal/modules"}
            or path.startswith("/api/v1/public/")
        )
        return approved or bool(
            self.policy and self.policy.can_fetch(self.settings.user_agent, url)
        )

    def prepare_job(self, explicit_retry: bool, prior_cooldown: bool):
        """Восстанавливает ограничение после рестарта и открывает gate только по команде UI."""
        if explicit_retry:
            self.http.reset_access_gate()
        elif prior_cooldown:
            self.http.rate_limit_hits = max(self.http.rate_limit_hits, 1)

    async def preflight(self):
        """Проверяет robots, crawl-delay и публичные условия до любых карточек."""
        response, _ = await self.http.get("https://leaders-project.ru/robots.txt")
        if response.status_code != 200 or "user-agent" not in response.text.lower():
            raise SourceError("Не удалось проверить robots.txt", "robots", pause=True)
        policy = RobotFileParser()
        policy.parse(response.text.splitlines())
        self.policy = policy
        self.http.allowed = self.allowed
        delay = policy.crawl_delay(self.settings.user_agent) or policy.crawl_delay("*") or 0
        rate = policy.request_rate(self.settings.user_agent) or policy.request_rate("*")
        if rate:
            delay = max(delay, rate.seconds / rate.requests)
        self.settings.request_min_delay = max(self.settings.request_min_delay, delay)
        self.settings.request_max_delay = max(self.settings.request_max_delay, delay)
        if not self.http.allowed(self.settings.source_url):
            raise SourceError("Каталог запрещён robots.txt", "robots", pause=True)
        response, _ = await self.http.get("https://leaders-project.ru/legal/terms")
        content = BeautifulSoup(response.text, "html.parser").get_text(" ", strip=True).lower()
        restrictions = (
            "автоматизированный сбор запрещ",
            "запрещается автоматизирован",
            "запрещено автоматизирован",
            "запрещается парсинг",
            "scraping is prohibited",
        )
        if any(marker in content for marker in restrictions):
            raise SourceError(
                "Условия источника ограничивают автоматический сбор", "terms", pause=True
            )

    async def load(self, url: str, stage: str) -> tuple[str, int]:
        """Предпочитает разрешённый публичный API; HTML/JS остаётся отдельным режимом."""
        if (
            stage == "discovering"
            and self.settings.source_mode != "browser"
            and self.settings.source_api_access_approved
            and (api_url := catalogue_api_url(url))
        ):
            response, attempts = await self.http.get(api_url)
            if "application/json" not in response.headers.get("content-type", ""):
                raise StructureError("Публичный API каталога не вернул JSON")
            return response.text, attempts
        if (
            stage in {"detail", "contacts"}
            and self.settings.source_mode != "browser"
            and self.settings.source_api_access_approved
        ):
            return await self._company_api(url, stage)
        if self.render:
            return await self._guarded_render(url, stage), 1
        response, attempts = await self.http.get(url)
        html = response.text
        soup = BeautifulSoup(html, "html.parser")
        selector = {
            "discovering": 'a[href^="/companies/"]',
            "detail": "section#a-requisites",
            "contacts": "h1",
        }[stage]
        present = bool(soup.select_one(selector))
        if stage == "discovering":
            import re

            present = any(
                re.fullmatch(r"/companies/\d{10,12}", a.get("href", ""))
                for a in soup.select("a[href]")
            )
        if not present and self.settings.source_mode == "auto":
            self.render = True
            html = await self._guarded_render(url, stage)
        return html, attempts

    async def _company_api(self, url: str, stage: str) -> tuple[str, int]:
        """Читает подтверждённый публичный bundle карточки и контактов одним GET."""
        parts = urlsplit(url)
        match = re.fullmatch(r"/companies/(\d{10,12})(?:/contacts)?", parts.path)
        if parts.scheme != "https" or parts.netloc != "leaders-project.ru" or not match:
            raise StructureError("Некорректный URL публичной карточки")
        api_url = "https://leaders-project.ru/api/v1/companies/" + match[1]
        if stage == "contacts" and self.company_cache and self.company_cache[0] == api_url:
            _, body, attempts = self.company_cache
            self.company_cache = None
            return body, attempts
        self.company_cache = None
        response, attempts = await self.http.get(api_url)
        if "application/json" not in response.headers.get("content-type", ""):
            raise StructureError("Публичный API карточки не вернул JSON")
        body = response.text
        self.company_cache = (api_url, body, attempts) if stage == "detail" else None
        return body, attempts

    async def _guarded_render(self, url, stage):
        """Прерывает ожидание DOM при блокировке источника, паузе и SIGTERM."""
        self.route_error = None
        task = asyncio.create_task(self._render(url, stage))
        loop = asyncio.get_running_loop()
        checked_at = loop.time()
        deadline = checked_at + self.settings.browser_render_timeout
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=min(1, max(0.01, deadline - loop.time())))
                if self.route_error:
                    raise self.route_error
                await self.http.limiter.checkpoint()
                now = loop.time()
                until = aware(await self.http.limiter.repository.limiter_time())
                if (until - utcnow()).total_seconds() > self.settings.request_max_delay + 1:
                    # Retry-After/cooldown не должен превратиться в ошибку структуры DOM.
                    deadline += now - checked_at
                checked_at = now
                if not task.done() and now >= deadline:
                    raise StructureError("JavaScript-страница не отдала ожидаемые данные")
            return await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _route(self, route):
        """Проводит browser requests через общий limiter; блокирует disallowed API."""
        request = route.request
        generation = self.generation
        parts = urlsplit(request.url)
        if (
            parts.netloc != "leaders-project.ru"
            or request.method != "GET"
            or request.resource_type in {"image", "media", "font"}
        ):
            await route.abort()
            return
        if (
            request.headers.get("next-router-prefetch")
            or request.headers.get("purpose") == "prefetch"
            or request.headers.get("next-router-segment-prefetch")
            or (
                request.headers.get("rsc")
                and parts.path.startswith("/companies/")
                and parts.path != self.document_path
            )
        ):
            await route.abort()
            return
        # Ненужная авторизация/аналитика не требуется для публичной страницы.
        if parts.path in {"/api/v1/me", "/api/auth/session"}:
            await route.fulfill(status=401, content_type="application/json", body="{}")
            return
        try:
            if self.route_error:
                await route.abort()
                return
            if request.url in self.cache:
                body, content_type = self.cache[request.url]
            else:
                headers = {
                    key: value
                    for key, value in request.headers.items()
                    if key.lower() in {"accept", "rsc", "next-router-state-tree", "next-url"}
                }
                response, _ = await self.http.get(request.url, headers=headers)
                body = response.content
                content_type = response.headers.get("content-type", "application/octet-stream")
                if request.resource_type in {"script", "stylesheet"} and len(self.cache) < 100:
                    self.cache[request.url] = (body, content_type)
            if generation == self.generation:
                await route.fulfill(status=200, body=body, content_type=content_type)
            else:
                with suppress(BrowserError):
                    await route.abort()
        except Exception as error:
            if generation == self.generation:
                self.route_error = self.route_error or error
            with suppress(BrowserError):
                await route.abort()

    async def idle(self):
        """Убирает страницу на паузе, чтобы её фоновые таймеры не обращались к сайту."""
        self.company_cache = None
        if self.page and not self.page.is_closed() and self.page.url != "about:blank":
            self.generation += 1
            with suppress(BrowserError):
                await self.page.goto("about:blank", wait_until="commit", timeout=10000)

    async def _catalog_number(self):
        """Читает активную страницу из aria-current вместо вычисления по URL."""
        active = self.page.locator('nav[aria-label="Страницы компаний"] [aria-current="page"]')
        await active.wait_for(state="attached", timeout=0)
        return int((await active.inner_text()).strip())

    async def _advance_catalog(self, desired):
        """Нажимает Next, дожидаясь смены номера и самих организаций."""
        current = await self._catalog_number()
        if current > desired:
            raise StructureError("Каталог открыл страницу после сохранённого checkpoint")
        while current < desired:
            previous = await self.page.locator('a[href^="/companies/"]').evaluate_all(
                COMPANY_LINKS_JS
            )
            button = self.page.get_by_role("button", name="Следующая страница", exact=True)
            if await button.is_disabled():
                raise StructureError("Следующая страница недоступна до конца checkpoint")
            await button.click()
            current += 1
            await self.page.locator(
                'nav[aria-label="Страницы компаний"] '
                f'button[aria-label="Страница {current}"][aria-current="page"]'
            ).wait_for(state="attached", timeout=0)
            await self.page.wait_for_function(
                CHANGED_JS,
                arg=previous,
                timeout=0,
            )
            if self.route_error:
                raise self.route_error

    async def _render(self, url: str, stage: str) -> str:
        """Рендерит одну вкладку без persistent profile, HTML-архивов и screenshot."""
        self.generation += 1
        self.document_path = urlsplit(url).path
        if not self.page or self.page.is_closed():
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch()
            self.context = await self.browser.new_context(service_workers="block")
            await self.context.route("**/*", self._route)
            self.page = await self.context.new_page()
            self.page.set_default_timeout(0)
            self.page.set_default_navigation_timeout(0)
        self.route_error = None
        selector = {
            "discovering": 'nav[aria-label="Страницы компаний"] [aria-current="page"]',
            "detail": "section#a-requisites",
            "contacts": "h1",
        }[stage]
        try:
            parts = urlsplit(url)
            base = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
            cursor = int(parts.fragment[8:]) if parts.fragment.startswith("lp-page=") else None
            if not cursor or self.page.url != base:
                await self.page.goto(
                    base,
                    wait_until="domcontentloaded",
                    timeout=0,
                )
            if cursor:
                await self._advance_catalog(cursor)
            if self.route_error:
                raise self.route_error
            await self.page.locator(selector).first.wait_for(state="attached", timeout=0)
            if stage in {"detail", "contacts"}:
                # H1/реквизиты могут появиться до отдельного запроса контактов/руководства.
                await self.page.wait_for_load_state("networkidle", timeout=0)
            if self.route_error:
                raise self.route_error
            if stage == "discovering":
                await self.page.wait_for_function(
                    READY_JS,
                    timeout=0,
                )
            html = BeautifulSoup(await self.page.content(), "html.parser")
            location = html.new_tag(
                "meta", attrs={"name": "leaders-parser-location", "content": self.page.url}
            )
            html.append(location)
            html = str(html)
            check_response(200, html, 1)
            return html
        except (BrowserError, TimeoutError) as error:
            if self.route_error:
                raise self.route_error from error
            raise StructureError("JavaScript-страница не отдала ожидаемые данные") from error

    async def close(self):
        """Закрывает ephemeral context и HTTP-сессию даже после ошибки рендеринга."""
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        await self.http.close()
