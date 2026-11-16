# app/bootstrap.py
"""Composition root: единственное место сборки конкретных адаптеров сервиса."""

from app.application.jobs import Jobs
from app.infrastructure.database.connection import create_database
from app.infrastructure.database.repository import Repository
from app.infrastructure.excel.exporter import build_workbook


def build_services(settings):
    """Собирает repository и сценарии; подключения открываются лениво."""
    engine, sessions = create_database(settings.db_url())
    repository = Repository(sessions)
    return engine, repository, Jobs(repository, settings.source_url, build_workbook)
