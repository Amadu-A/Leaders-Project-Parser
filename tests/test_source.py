# tests/test_source.py
"""Политика доступа и настоящий JS-render/pagination на изолированном MockTransport."""

import asyncio
import os
import sys

import httpx
import pytest

from app.config import Settings
from app.infrastructure.http.client import HttpClient, RateLimiter
from app.infrastructure.http.source import PublicSource
from scripts.smoke import MemoryLimiterStore, checkpoint, cooldown


def test_owner_permission_is_scoped():
    """Разрешение публичного API не открывает /admin, /auth или другие домены."""
    from urllib.robotparser import RobotFileParser

    source = PublicSource(Settings(_env_file=None), None)
    source.policy = RobotFileParser()
    source.policy.parse(["User-agent: *", "Disallow: /api", "Disallow: /admin"])
    assert source.allowed("https://leaders-project.ru/api/v1/companies")
    assert not source.allowed("https://leaders-project.ru/api/v1/admin")
    assert not source.allowed("https://leaders-project.ru/admin")
    source.settings.source_api_access_approved = False
    assert not source.allowed("https://leaders-project.ru/api/v1/companies")


@pytest.mark.skipif(
    not os.getenv("TEST_BROWSER"), reason="Set TEST_BROWSER=1 for browser integration"
)
def test_browser_button_pagination_and_restart():
    """Запускает browser-тест с subprocess-совместимым event loop Windows."""
    factory = asyncio.ProactorEventLoop if sys.platform == "win32" else asyncio.new_event_loop
    with asyncio.Runner(loop_factory=factory) as runner:
        runner.run(browser_pagination())


async def browser_pagination():
    """Реальный Chromium проходит кнопки и восстанавливает URL без предположения page=."""
    from app.infrastructure.parsers import pages

    html = """<!doctype html><html><head><meta charset="utf-8"></head><body><main><h1>Каталог</h1>
    <p>Всего 217 компаний</p><div id="rows"></div><nav aria-label="Страницы компаний"></nav></main>
    <script>
    let current = Number(new URL(location.href).searchParams.get("position") || "1");
    function render() {
      const count = current === 3 ? 17 : 100;
      document.querySelector("#rows").replaceChildren();
      for (let i = 0; i < count; i++) {
        const a = document.createElement("a");
        a.href = "/companies/" + (1000000000+(current-1)*100+i);
        a.textContent = "Компания " + i; document.querySelector("#rows").append(a);
      }
      const nav = document.querySelector("nav"); nav.replaceChildren();
      const active = document.createElement("button"); active.textContent = current;
      active.setAttribute("aria-label", "Страница " + current);
      active.setAttribute("aria-current", "page");
      const next = document.createElement("button");
      next.setAttribute("aria-label", "Следующая страница");
      next.disabled = current === 3;
      next.onclick = () => {
        current++; history.pushState(null,"","?size=100&position="+current); render();
      };
      nav.append(active,next);
    } render();
    </script></body></html>"""

    async def respond(request):
        """Возвращает только fixture: ни один запрос не выходит в интернет."""
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path == "/legal/terms":
            return httpx.Response(200, text="Public test catalogue")
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    def build():
        """Собирает production loader с реальным browser и полностью fake HTTP."""
        settings = Settings(
            _env_file=None,
            request_min_delay=0,
            request_max_delay=0,
            source_mode="browser",
            browser_render_timeout=10,
        )
        limiter = RateLimiter(settings, MemoryLimiterStore(), checkpoint)
        return PublicSource(
            settings, HttpClient(settings, limiter, cooldown, httpx.MockTransport(respond))
        )

    url = "https://leaders-project.ru/companies/projecting?size=100"
    source = build()
    try:
        await source.preflight()
        first = pages.catalog((await source.load(url, "discovering"))[0], url)
        second = pages.catalog(
            (await source.load(first.next_url, "discovering"))[0], first.next_url
        )
        assert len(first.companies) == len(second.companies) == 100
        assert "position=2" in second.next_url
        assert {c.inn for c in first.companies}.isdisjoint(c.inn for c in second.companies)
        await source.idle()
        assert source.page.url == "about:blank"
        resumed = pages.catalog(
            (await source.load(second.next_url, "discovering"))[0], second.next_url
        )
        assert len(resumed.companies) == 17 and resumed.next_url is None
    finally:
        await source.close()
    restarted = build()
    try:
        await restarted.preflight()
        third = pages.catalog(
            (await restarted.load(second.next_url, "discovering"))[0], second.next_url
        )
        assert len(third.companies) == 17 and third.next_url is None
        assert third.companies[-1].inn == "1000000216"
    finally:
        await restarted.close()


@pytest.mark.skipif(
    not os.getenv("TEST_BROWSER"), reason="Set TEST_BROWSER=1 for browser integration"
)
def test_browser_allows_current_document_rsc_but_blocks_prefetch():
    """Данные активной Next.js-карточки не блокируются вместе с prefetch соседних."""
    factory = asyncio.ProactorEventLoop if sys.platform == "win32" else asyncio.new_event_loop
    with asyncio.Runner(loop_factory=factory) as runner:
        runner.run(browser_current_document_rsc())


async def browser_current_document_rsc():
    """Реальная вкладка получает секцию только после read-only RSC-запроса."""
    requested = []
    url = "https://leaders-project.ru/companies/1000000000"
    html = """<!doctype html><html><body><h1>Компания</h1><main></main><script>
    fetch(location.pathname+'?_rsc=required', {headers:{RSC:'1'}})
      .then(r=>r.text()).then(text=>document.querySelector('main').innerHTML=text);
    fetch('/companies/1000000001?_rsc=neighbor', {headers:{RSC:'1'}}).catch(()=>{});
    fetch(location.pathname+'?_rsc=prefetch',
      {headers:{RSC:'1','next-router-prefetch':'1'}}).catch(()=>{});
    </script></body></html>"""

    async def respond(request):
        """Возвращает только синтетические данные и фиксирует настоящие запросы."""
        requested.append(str(request.url))
        if request.headers.get("rsc"):
            return httpx.Response(200, text='<section id="a-requisites">ИНН 1000000000</section>')
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    settings = Settings(
        _env_file=None,
        request_min_delay=0,
        request_max_delay=0,
        source_mode="browser",
        browser_render_timeout=3,
    )
    limiter = RateLimiter(settings, MemoryLimiterStore(), checkpoint)
    source = PublicSource(
        settings, HttpClient(settings, limiter, cooldown, httpx.MockTransport(respond))
    )
    try:
        result, _ = await source.load(url, "detail")
        assert 'id="a-requisites"' in result
        assert any("_rsc=required" in target for target in requested)
        assert not any(
            "_rsc=neighbor" in target or "_rsc=prefetch" in target for target in requested
        )
    finally:
        await source.close()
