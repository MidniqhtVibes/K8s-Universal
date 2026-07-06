"""Global allocation and naming preferences."""

from alembic import op
import sqlalchemy as sa

revision = "0003_preferences"
down_revision = "0002_application_bundles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "preferences",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("preferences")

