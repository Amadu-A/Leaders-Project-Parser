# tests/test_observability.py
"""Timing сохраняет исключения/отмену и пишет одно итоговое событие без payload."""

import asyncio
import logging

import pytest

from app.observability import timed


@pytest.mark.parametrize("fail", [False, True])
def test_sync_timing(caplog, fail):
    """Проверяет metadata, duration и status синхронного сценария."""

    @timed("test_sync")
    def operation():
        """Выполняет успешный или ошибочный тестовый сценарий."""
        if fail:
            raise ValueError("test")
        return 42

    with caplog.at_level(logging.INFO):
        if fail:
            with pytest.raises(ValueError):
                operation()
        else:
            assert operation() == 42
    record = caplog.records[-1]
    assert record.status == ("error" if fail else "success") and record.duration_ms >= 0
    assert record.operation == "test_sync" and operation.__name__ == "operation"


@pytest.mark.parametrize("failure", [None, ValueError, asyncio.CancelledError])
async def test_async_timing(caplog, failure):
    """CancelledError распространяется и никогда не записывается как success."""

    @timed("test_async")
    async def operation():
        """Сохраняет исключение async-сценария в timing wrapper."""
        if failure:
            raise failure()
        return 42

    with caplog.at_level(logging.INFO):
        if failure:
            with pytest.raises(failure):
                await operation()
        else:
            assert await operation() == 42
    assert caplog.records[-1].status == ("error" if failure else "success")
    assert caplog.records[-1].duration_ms >= 0
