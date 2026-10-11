"""Initial schema: tickets, classifications, dead_letters.

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

STATUSES = ("queued", "processing", "awaiting_review", "routed", "rejected", "failed")


def _timestamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade() -> None:
    op.create_table(
        "tickets",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("external_id", sa.String(128), nullable=False, unique=True),
        sa.Column("subject", sa.String(512)),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("source", sa.String(64)),
        sa.Column("status", sa.Enum(*STATUSES, name="ticket_status"), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text),
        _timestamp("created_at"),
        _timestamp("updated_at"),
    )
    op.create_index("ix_tickets_status_created_at", "tickets", ["status", "created_at"])

    op.create_table(
        "classifications",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "ticket_id",
            sa.Integer,
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("routed_to", sa.String(64)),
        sa.Column("decided_by", sa.String(32), nullable=False),
        sa.Column("prompt_version", sa.String(32), nullable=False),
        _timestamp("created_at"),
    )

    op.create_table(
        "dead_letters",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "ticket_id",
            sa.Integer,
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("error", sa.Text, nullable=False),
        _timestamp("created_at"),
    )


def downgrade() -> None:
    op.drop_table("dead_letters")
    op.drop_table("classifications")
    op.drop_index("ix_tickets_status_created_at", table_name="tickets")
    op.drop_table("tickets")
    sa.Enum(name="ticket_status").drop(op.get_bind())
