from datetime import datetime
from sqlalchemy import String, DateTime, Float, ForeignKey, Text, func, Integer, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


class Receipt(Base):
    __tablename__ = "receipts"

    id: Mapped[int] = mapped_column(primary_key=True)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="processing", nullable=False)

    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    merchant: Mapped[str | None] = mapped_column(String(255), nullable=True)
    purchase_datetime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    total: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)

    image_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    total_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    raw_ocr_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_llm_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    detected_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False, server_default="OTHER")

    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    user = relationship("User", back_populates="receipts")

    items = relationship(
        "ReceiptItem",
        back_populates="receipt",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
