# app/observability.py
"""Единое JSON-логирование без payload и timing значимых сценариев."""

import inspect
import json
import logging
import traceback
from functools import wraps
from time import perf_counter


class JsonFormatter(logging.Formatter):
    """Пишет только разрешённые эксплуатационные поля, исключая контакты и секреты."""

    def format(self, record):
        """Сериализует событие и один traceback на ответственном error boundary."""
        data = {
            "level": record.levelname,
            "event": record.getMessage(),
            "timestamp": self.formatTime(record),
        }
        for name in ("operation", "duration_ms", "status", "job_id"):
            if hasattr(record, name):
                data[name] = getattr(record, name)
        if record.exc_info:
            data["exception_type"] = record.exc_info[0].__name__
            data["traceback"] = "".join(traceback.format_tb(record.exc_info[2]))
            # Тексты исключений драйверов могут содержать credentials/payload.
        return json.dumps(data, ensure_ascii=False)


def configure_logging(level: str) -> None:
    """Настраивает stdout; ротацией управляет Docker logging driver."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    # Не записывать строки поиска и каждый пятисекундный polling в access log.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def timed(operation: str):
    """Измеряет sync/async сценарии monotonic-таймером, сохраняя исключения."""

    def decorate(function):
        """Добавляет одно итоговое событие, не создавая повторный traceback."""

        def report(started, status):
            """Пишет ограниченные поля latency без аргументов функции."""
            logging.getLogger(function.__module__).info(
                "operation_timing",
                extra={
                    "operation": operation,
                    "duration_ms": round((perf_counter() - started) * 1000, 2),
                    "status": status,
                },
            )

        if inspect.iscoroutinefunction(function):

            @wraps(function)
            async def asynchronous(*args, **kwargs):
                """Сохраняет отмену async-операции как ошибочное завершение."""
                started, status = perf_counter(), "error"
                try:
                    result = await function(*args, **kwargs)
                    status = "success"
                    return result
                finally:
                    report(started, status)

            return asynchronous

        @wraps(function)
        def synchronous(*args, **kwargs):
            """Измеряет синхронную значимую операцию без перехвата её ошибок."""
            started, status = perf_counter(), "error"
            try:
                result = function(*args, **kwargs)
                status = "success"
                return result
            finally:
                report(started, status)

        return synchronous

    return decorate
