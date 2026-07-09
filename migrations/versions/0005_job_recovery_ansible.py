"""Add job heartbeat and ansible rerun jobs."""

from alembic import op
import sqlalchemy as sa

revision = "0005_job_recovery_ansible"
down_revision = "0004_manifest_delete_job"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE jobkind ADD VALUE IF NOT EXISTS 'ANSIBLE'")
    op.add_column("jobs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "heartbeat_at")
    # PostgreSQL enum values intentionally remain; removing enum values requires type recreation.
