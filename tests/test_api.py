# tests/test_api.py
"""Контракт API: авторизация, CSRF, идемпотентное управление и memory download."""

import base64
from io import BytesIO

import httpx
from openpyxl import load_workbook

from app.api.main import create_app
from app.config import Settings
from app.domain.models import CatalogPage, Listing


async def test_api_full_lifecycle(repository):
    """Проверяет реальный routing с изолированным repository, без worker-сайта."""
    settings = Settings(_env_file=None, app_password="test-password-is-long-enough")
    app = create_app(settings, repository)
    token = base64.b64encode(b"operator:test-password-is-long-enough").decode()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            assert (await client.get("/health")).status_code == 200
            assert (await client.get("/ready")).status_code == 200
            assert (await client.get("/")).status_code == 401
            client.headers["Authorization"] = "Basic " + token
            page = await client.get("/")
            assert page.status_code == 200 and "Leaders Project Parser" in page.text
            assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
            assert (await client.post("/api/jobs")).status_code == 403
            client.headers["X-Requested-With"] = "LeadersParser"
            assert (
                await client.post("/api/jobs", headers={"Origin": "http://evil.test"})
            ).status_code == 403
            job = (await client.post("/api/jobs")).json()
            path = "/api/jobs/" + job["id"]
            assert (await client.post("/api/jobs")).json()["id"] == job["id"]
            assert (await client.get("/api/jobs/latest")).json()["id"] == job["id"]
            await repository.save_page(
                job["id"],
                settings.source_url,
                CatalogPage(
                    [
                        Listing(
                            "Тестовая компания",
                            "0012345678",
                            "https://leaders-project.ru/companies/0012345678",
                        )
                    ],
                    None,
                    1,
                ),
            )
            results = (
                await client.get(path + "/companies", params={"q": "001234", "size": 1})
            ).json()
            assert results["total"] == 1 and results["items"][0]["inn"] == "0012345678"
            assert (await client.get(path + "/companies?size=37000")).status_code == 422
            assert (await client.get("/api/jobs/not-a-uuid")).status_code == 422
            assert (await client.delete(path)).status_code == 409
            assert (await client.post(path + "/pause")).json()["status"] == "paused"
            assert (await client.post(path + "/resume")).json()["status"] == "queued"
            assert (await client.post(path + "/cancel")).json()["status"] == "cancelled"
            for _ in range(2):
                response = await client.get(path + "/download")
                assert (
                    response.status_code == 200
                    and "attachment" in response.headers["content-disposition"]
                )
                assert response.headers["cache-control"] == "no-store"
                book = load_workbook(BytesIO(response.content))
                assert book["Компании"]["C2"].value == "0012345678"
                assert "Ошибки" in book.sheetnames
            assert (await client.delete(path)).status_code == 204
            assert (await client.delete(path)).status_code == 204
            assert (await client.get(path)).status_code == 404


async def test_readiness_failure_is_redacted():
    """Отказ БД возвращает 503 без credentials, SQL и внутренних сообщений."""

    class BrokenStore:
        """Имитирует недоступную обязательную PostgreSQL."""

        async def ready(self):
            """Выбрасывает ошибку, содержимое которой нельзя отдавать пользователю."""
            raise RuntimeError("postgres://secret:password@host")

    app = create_app(Settings(_env_file=None, app_password="long-test-password"), BrokenStore())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            response = await client.get("/ready")
            assert response.status_code == 503 and "secret" not in response.text
