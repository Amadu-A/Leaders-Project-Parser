# tests/test_render_deadline.py
"""Глобальная пауза источника не должна расходовать дедлайн ожидания JavaScript DOM."""

import asyncio
from datetime import timedelta

import pytest

from app.config import Settings
from app.domain.models import StructureError, utcnow
from app.infrastructure.http.client import HttpClient, RateLimiter
from app.infrastructure.http.source import PublicSource
from scripts.smoke import MemoryLimiterStore, checkpoint, cooldown


@pytest.mark.parametrize("cooling_down", [False, True])
async def test_render_deadline_excludes_global_cooldown(cooling_down):
    """Обычный зависший DOM прерывается; Retry-After сохраняет возможность завершения."""
    settings = Settings(
        _env_file=None, request_min_delay=0, request_max_delay=0, browser_render_timeout=0.02
    )
    store = MemoryLimiterStore()
    if cooling_down:
        store.next_at = utcnow() + timedelta(seconds=10)
    limiter = RateLimiter(settings, store, checkpoint)
    source = PublicSource(settings, HttpClient(settings, limiter, cooldown))

    async def render(url, stage):
        """Имитирует задержку DOM без реального браузера или сети."""
        await asyncio.sleep(0.08)
        return "ready"

    source._render = render
    try:
        if cooling_down:
            assert await source._guarded_render("https://leaders-project.ru/", "detail") == "ready"
        else:
            with pytest.raises(StructureError):
                await source._guarded_render("https://leaders-project.ru/", "detail")
    finally:
        await source.close()
