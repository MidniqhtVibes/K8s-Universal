"""Application bundles, manifest revisions and manifest jobs."""

from alembic import op
import sqlalchemy as sa

revision = "0002_application_bundles"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE jobkind ADD VALUE IF NOT EXISTS 'MANIFEST_VALIDATE'")
    op.execute("ALTER TYPE jobkind ADD VALUE IF NOT EXISTS 'MANIFEST_DIFF'")
    op.execute("ALTER TYPE jobkind ADD VALUE IF NOT EXISTS 'MANIFEST_APPLY'")
    op.add_column("jobs", sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")))
    op.create_table(
        "application_bundles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cluster_id", sa.String(36), sa.ForeignKey("clusters.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(63), nullable=False),
        sa.Column("description", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("cluster_id", "name"),
    )
    op.create_index("ix_application_bundles_cluster_id", "application_bundles", ["cluster_id"])
    op.create_table(
        "manifest_files",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("bundle_id", sa.String(36), sa.ForeignKey("application_bundles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("path", sa.String(160), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("bundle_id", "path"),
    )
    op.create_index("ix_manifest_files_bundle_id", "manifest_files", ["bundle_id"])
    op.create_table(
        "manifest_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("bundle_id", sa.String(36), sa.ForeignKey("application_bundles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("message", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("bundle_id", "version"),
    )
    op.create_index("ix_manifest_revisions_bundle_id", "manifest_revisions", ["bundle_id"])


def downgrade() -> None:
    op.drop_table("manifest_revisions")
    op.drop_table("manifest_files")
    op.drop_table("application_bundles")
    op.drop_column("jobs", "payload")
    # PostgreSQL enum values intentionally remain; removing enum values requires type recreation.
