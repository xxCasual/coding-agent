"""Add runs.review JSONB for Reviewer status, findings, and dispositions.

Revision ID: 0003_run_review
Revises: 0002_run_lease
Create Date: 2026-09-10
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0003_run_review"
down_revision: Union[str, Sequence[str], None] = "0002_run_lease"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("review", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "review")
