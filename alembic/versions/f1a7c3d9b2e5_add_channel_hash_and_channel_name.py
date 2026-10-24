"""Add packet.channel_hash and channel_name table (#16)

Revision ID: f1a7c3d9b2e5
Revises: e8f2c4b6d9a1
Create Date: 2026-10-24 00:00:00.000000

Existing packets get their channel recomputed by startdb on the next start
(mqtt_store.recompute_packet_channels), which needs the configured keys.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1a7c3d9b2e5"
down_revision: str | None = "e8f2c4b6d9a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("packet", sa.Column("channel_hash", sa.Integer(), nullable=True))
    op.create_table(
        "channel_name",
        sa.Column("channel_hash", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("first_seen_us", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("channel_hash", "name"),
    )


def downgrade() -> None:
    op.drop_table("channel_name")
    with op.batch_alter_table("packet") as batch_op:
        batch_op.drop_column("channel_hash")
