"""Persist frozen task modes and review targets. Existing runs remain develop."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005_run_task_mode"
down_revision = "0004_tool_replay_category"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("task_mode", sa.String(16), nullable=False, server_default="develop"))
    op.add_column("runs", sa.Column("review_target", postgresql.JSONB(), nullable=True))
    op.create_check_constraint("ck_runs_task_mode", "runs", "task_mode IN ('develop', 'review', 'plan')")


def downgrade() -> None:
    op.drop_constraint("ck_runs_task_mode", "runs", type_="check")
    op.drop_column("runs", "review_target")
    op.drop_column("runs", "task_mode")
