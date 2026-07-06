"""Initial cluster builder schema."""

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

credential_kind = sa.Enum("PROXMOX", "SSH", name="credentialkind")
cluster_status = sa.Enum("DRAFT", "PLANNED", "APPLYING", "READY", "FAILED", "DESTROYED", name="clusterstatus")
job_kind = sa.Enum("PLAN", "APPLY", "VERIFY", "DESTROY_PLAN", "DESTROY", name="jobkind")
job_status = sa.Enum("QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", name="jobstatus")


def upgrade() -> None:
    op.create_table("users", sa.Column("id", sa.String(36), primary_key=True), sa.Column("username", sa.String(100), nullable=False, unique=True), sa.Column("password_hash", sa.Text(), nullable=False), sa.Column("enabled", sa.Boolean(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("credentials", sa.Column("id", sa.String(36), primary_key=True), sa.Column("name", sa.String(120), nullable=False), sa.Column("kind", credential_kind, nullable=False), sa.Column("encrypted_payload", sa.Text(), nullable=False), sa.Column("public_data", sa.JSON(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("name", "kind"))
    op.create_table("clusters", sa.Column("id", sa.String(36), primary_key=True), sa.Column("name", sa.String(63), nullable=False, unique=True), sa.Column("status", cluster_status, nullable=False), sa.Column("config", sa.JSON(), nullable=False), sa.Column("config_hash", sa.String(64), nullable=False), sa.Column("planned_hash", sa.String(64)), sa.Column("destroy_planned_hash", sa.String(64)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("jobs", sa.Column("id", sa.String(36), primary_key=True), sa.Column("cluster_id", sa.String(36), sa.ForeignKey("clusters.id", ondelete="CASCADE"), nullable=False), sa.Column("kind", job_kind, nullable=False), sa.Column("status", job_status, nullable=False), sa.Column("requested_config_hash", sa.String(64), nullable=False), sa.Column("log", sa.Text(), nullable=False), sa.Column("error", sa.Text()), sa.Column("cancel_requested", sa.Boolean(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("started_at", sa.DateTime(timezone=True)), sa.Column("finished_at", sa.DateTime(timezone=True)))
    op.create_index("ix_jobs_cluster_id", "jobs", ["cluster_id"])
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_table("audit_events", sa.Column("id", sa.String(36), primary_key=True), sa.Column("action", sa.String(120), nullable=False), sa.Column("object_type", sa.String(80), nullable=False), sa.Column("object_id", sa.String(36)), sa.Column("details", sa.JSON(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("jobs")
    op.drop_table("clusters")
    op.drop_table("credentials")
    op.drop_table("users")
    for enum in (job_status, job_kind, cluster_status, credential_kind):
        enum.drop(op.get_bind(), checkfirst=True)
