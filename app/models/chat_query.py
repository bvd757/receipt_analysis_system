from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class ChatQuery(Base):
    __tablename__ = "chat_queries"

    id: Mapped[int] = mapped_column(primary_key=True)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)

    question: Mapped[str] = mapped_column(Text, nullable=False)
    chosen_source: Mapped[str] = mapped_column(String(32), nullable=False)  # canned | llm

    generated_sql: Mapped[str | None] = mapped_column(Text, nullable=True)
    sandbox_sql: Mapped[str | None] = mapped_column(Text, nullable=True)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
