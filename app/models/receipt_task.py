from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class ReceiptTask(Base):
    __tablename__ = "receipt_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)

    receipt_id: Mapped[int] = mapped_column(ForeignKey("receipts.id"), index=True, nullable=False)

    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False, index=True)

    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    receipt_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"), index=True)

    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
