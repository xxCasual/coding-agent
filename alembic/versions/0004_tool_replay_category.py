"""Add tool_executions.replay_category for recovery policy.

Revision ID: 0004_tool_replay_category
Revises: 0003_run_review
Create Date: 2026-09-10
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004_tool_replay_category"
down_revision: Union[str, Sequence[str], None] = "0003_run_review"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tool_executions",
        sa.Column("replay_category", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tool_executions", "replay_category")
