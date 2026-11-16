# app/api/main.py
"""Тонкий HTTP transport: авторизация, CSRF, Jinja UI и application endpoints."""

import base64
import binascii
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.application.jobs import Jobs
from app.bootstrap import build_services
from app.config import Settings
from app.domain.models import Conflict, NotFound
from app.infrastructure.excel.exporter import build_workbook
from app.observability import configure_logging

WEB = Path(__file__).resolve().parents[1] / "web"


def service(request: Request) -> Jobs:
    """Внедряет заранее собранный application service в endpoint."""
    return request.app.state.jobs


JobService = Annotated[Jobs, Depends(service)]


def create_app(settings: Settings | None = None, repository=None) -> FastAPI:
    """Создаёт приложение; тесты внедряют repository без production соединений."""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(application):
        """Проверяет обязательные credentials и закрывает пул при завершении API."""
        if len(settings.app_password.get_secret_value()) < 16:
            raise RuntimeError("APP_PASSWORD must contain at least 16 characters")
        configure_logging(settings.log_level)
        engine = None
        if repository is None:
            engine, store, jobs = build_services(settings)
        else:
            store, jobs = repository, Jobs(repository, settings.source_url, build_workbook)
        application.state.repository, application.state.jobs = store, jobs
        yield
        if engine:
            await engine.dispose()

    application = FastAPI(
        title="Leaders Project Parser",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    templates = Jinja2Templates(directory=WEB / "templates")

    @application.middleware("http")
    async def security(request: Request, call_next):
        """Защищает UI/API Basic Auth и custom-header CSRF; health не раскрывает данные."""
        if request.url.path not in {"/health", "/ready"}:
            valid = False
            auth = request.headers.get("authorization", "")
            if auth.startswith("Basic "):
                try:
                    username, password = (
                        base64.b64decode(auth[6:], validate=True).decode().split(":", 1)
                    )
                    valid = secrets.compare_digest(
                        username.encode(), settings.app_username.encode()
                    ) & secrets.compare_digest(
                        password.encode(), settings.app_password.get_secret_value().encode()
                    )
                except (ValueError, UnicodeError, binascii.Error):
                    pass
            if not valid:
                return Response(
                    status_code=401, headers={"WWW-Authenticate": 'Basic realm="Leaders Parser"'}
                )
            if request.method in {"POST", "DELETE", "PUT", "PATCH"}:
                origin = request.headers.get("origin")
                if request.headers.get("x-requested-with") != "LeadersParser" or (
                    origin and origin != str(request.base_url).rstrip("/")
                ):
                    return JSONResponse(
                        {"detail": "Запрос отклонён проверкой CSRF"}, status_code=403
                    )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @application.exception_handler(NotFound)
    async def not_found(request: Request, error: NotFound):
        """Преобразует отсутствие задания в безопасный ответ API."""
        return JSONResponse({"detail": str(error)}, status_code=404)

    @application.exception_handler(Conflict)
    async def conflict(request: Request, error: Conflict):
        """Отображает отказ перехода состояния без внутренних подробностей БД."""
        return JSONResponse({"detail": str(error)}, status_code=409)

    @application.get("/health")
    async def health():
        """Показывает liveness независимо от состояния PostgreSQL."""
        return {"status": "ok"}

    @application.get("/ready")
    async def ready(jobs: JobService):
        """Проверяет реальную доступность схемы PostgreSQL без утечки DSN."""
        try:
            await jobs.repository.ready()
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ready"}

    @application.get("/")
    async def index(request: Request):
        """Отдаёт интерфейс без загрузки компаний в HTML."""
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "source_url": settings.source_url,
                "poll_seconds": settings.status_poll_interval,
                "retention_days": settings.retention_days,
            },
        )

    @application.post("/api/jobs")
    async def create(jobs: JobService):
        """Ставит единственное задание в очередь идемпотентно."""
        return await jobs.create()

    @application.get("/api/jobs/latest")
    async def latest(jobs: JobService):
        """Восстанавливает экран после закрытия вкладки."""
        return await jobs.repository.latest()

    @application.get("/api/jobs/{job_id}")
    async def status(job_id: UUID, jobs: JobService):
        """Возвращает серверные счётчики, ETA и последние события."""
        return await jobs.repository.summary(str(job_id))

    @application.post("/api/jobs/{job_id}/{action}")
    async def control(job_id: UUID, action: str, jobs: JobService):
        """Делегирует pause/resume/cancel/retry-failed общему application сценарию."""
        if action not in {"pause", "resume", "cancel", "retry-failed"}:
            raise NotFound("Действие не найдено")
        return await jobs.control(str(job_id), action)

    @application.get("/api/jobs/{job_id}/companies")
    async def companies(
        job_id: UUID,
        jobs: JobService,
        page: int = Query(1, ge=1),
        size: int = Query(25, ge=1, le=100),
        q: str = Query("", max_length=200),
        errors_only: bool = False,
    ):
        """Отдаёт ограниченную страницу с контактами и причинами ошибок."""
        return await jobs.repository.companies(str(job_id), page, size, q, errors_only)

    @application.get("/api/jobs/{job_id}/download")
    async def download(job_id: UUID, jobs: JobService):
        """Передаёт созданный в RAM XLSX; повторное скачивание формирует его заново."""
        data = await jobs.export(str(job_id))
        date = datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat()
        filename = f"leaders_project_companies_{date}.xlsx"
        return Response(
            data,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @application.delete("/api/jobs/{job_id}", status_code=204)
    async def delete_job(job_id: UUID, jobs: JobService):
        """Удаляет структурированные данные только остановленного задания."""
        await jobs.repository.delete_job(str(job_id))

    application.mount("/static", StaticFiles(directory=WEB / "static"), name="static")
    return application


app = create_app()
