"""Document optional container registry settings in cluster JSON configs.

Revision ID: 0007_container_registry
Revises: 0006_applied_cluster_state

Cluster configuration is intentionally persisted as one JSON document. The
Pydantic schema supplies backward-compatible defaults for documents created
before this revision, so no physical database change or JSON rewrite is
required. In particular, not rewriting existing JSON preserves the invariant
between ``clusters.config`` and its authorization/state hashes.
"""

revision = "0007_container_registry"
down_revision = "0006_applied_cluster_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
