# app/infrastructure/database/migrations/versions/0002_company_fields.py
"""Добавляет КПП, числовую выручку и отдельные checkpoints дозаполнения без сброса данных."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    """Расширяет существующую схему; статусы, основной cursor и счётчики не меняются."""
    op.add_column("companies", sa.Column("kpp", sa.String(32), nullable=False, server_default=""))
    op.add_column("companies", sa.Column("revenue_mln", sa.Numeric(26, 8), nullable=True))
    for name in ("kpp_status", "revenue_status"):
        op.add_column(
            "companies", sa.Column(name, sa.String(32), nullable=False, server_default="pending")
        )
    op.add_column(
        "companies", sa.Column("kpp_attempts", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("jobs", sa.Column("source_url", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("revenue_backfill_next_url", sa.Text(), nullable=True))
    op.add_column(
        "jobs",
        sa.Column(
            "revenue_backfill_complete", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "jobs",
        sa.Column("revenue_backfill_pages", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "revenue_backfill_pages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "job_id", sa.String(36), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.UniqueConstraint("job_id", "url"),
        sa.UniqueConstraint("job_id", "fingerprint"),
    )
    op.create_index("ix_revenue_backfill_pages_job_id", "revenue_backfill_pages", ["job_id"])


def downgrade():
    """Откат новых полей требует отдельного решения оператора и резервной копии."""
    op.drop_table("revenue_backfill_pages")
    for name in (
        "revenue_backfill_pages",
        "revenue_backfill_complete",
        "revenue_backfill_next_url",
        "source_url",
    ):
        op.drop_column("jobs", name)
    for name in ("kpp_attempts", "revenue_status", "kpp_status", "revenue_mln", "kpp"):
        op.drop_column("companies", name)
