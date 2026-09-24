"""Add run lease, heartbeat, and attention columns.

Revision ID: 0002_run_lease
Revises: 0001_task_entities
Create Date: 2026-09-09
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0002_run_lease"
down_revision: Union[str, Sequence[str], None] = "0001_task_entities"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("owner_id", sa.String(length=128), nullable=True))
    op.add_column("runs", sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runs", sa.Column("attention", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_index("ix_runs_dispatch_pending", "runs", ["dispatch_pending"])


def downgrade() -> None:
    op.drop_index("ix_runs_dispatch_pending", table_name="runs")
    op.drop_column("runs", "attention")
    op.drop_column("runs", "heartbeat_at")
    op.drop_column("runs", "lease_until")
    op.drop_column("runs", "owner_id")
