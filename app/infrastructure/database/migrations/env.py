# app/infrastructure/database/migrations/env.py
"""Запускает версионированные PostgreSQL-миграции с advisory lock от гонки startup."""

from alembic import context
from sqlalchemy import create_engine, text

from app.config import Settings
from app.infrastructure.database.models import Base

engine = create_engine(Settings().db_url())
with engine.connect() as connection:
    connection.execute(text("SELECT pg_advisory_lock(78164424)"))
    connection.commit()
    try:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
    finally:
        connection.execute(text("SELECT pg_advisory_unlock(78164424)"))
        connection.commit()
engine.dispose()
