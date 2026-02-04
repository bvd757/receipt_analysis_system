"""add receipt versioning

Revision ID: 1f98f8b3715b
Revises: 31d86a5ba28d
Create Date: 2026-02-04 01:40:47.801148

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1f98f8b3715b'
down_revision: Union[str, Sequence[str], None] = '31d86a5ba28d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # receipts.version
    with op.batch_alter_table("receipts") as batch:
        batch.add_column(
            sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1"))
        )

    # receipt_tasks.receipt_version
    with op.batch_alter_table("receipt_tasks") as batch:
        batch.add_column(
            sa.Column("receipt_version", sa.Integer(), nullable=False, server_default=sa.text("1"))
        )
        batch.create_index("ix_receipt_tasks_receipt_version", ["receipt_version"])

    # на всякий случай: если где-то остались NULL (обычно не будет из-за server_default)
    op.execute("UPDATE receipt_tasks SET receipt_version = 1 WHERE receipt_version IS NULL")


def downgrade() -> None:
    with op.batch_alter_table("receipt_tasks") as batch:
        batch.drop_index("ix_receipt_tasks_receipt_version")
        batch.drop_column("receipt_version")

    with op.batch_alter_table("receipts") as batch:
        batch.drop_column("version")
