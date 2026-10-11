import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TicketStatus(enum.StrEnum):
    queued = "queued"
    processing = "processing"
    awaiting_review = "awaiting_review"
    routed = "routed"
    rejected = "rejected"
    failed = "failed"


class Category(enum.StrEnum):
    billing = "billing"
    technical = "technical"
    account = "account"
    feature_request = "feature_request"
    complaint = "complaint"
    other = "other"


class Ticket(Base):
    __tablename__ = "tickets"
    # Lets a reaper find work stuck in `processing` without a full scan.
    __table_args__ = (Index("ix_tickets_status_created_at", "status", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # The UNIQUE constraint, not application code, is the idempotency guarantee:
    # check-then-insert races between two requests; the constraint cannot.
    external_id: Mapped[str] = mapped_column(String(128), unique=True)
    subject: Mapped[str | None] = mapped_column(String(512))
    body: Mapped[str] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[TicketStatus] = mapped_column(
        Enum(TicketStatus, name="ticket_status"), default=TicketStatus.queued
    )
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    classification: Mapped["Classification | None"] = relationship(
        back_populates="ticket", lazy="selectin"
    )


class Classification(Base):
    __tablename__ = "classifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), unique=True
    )
    category: Mapped[str] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    routed_to: Mapped[str | None] = mapped_column(String(64))
    decided_by: Mapped[str] = mapped_column(String(32))  # llm | cache | rules
    prompt_version: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    ticket: Mapped[Ticket] = relationship(back_populates="classification")


class DeadLetter(Base):
    __tablename__ = "dead_letters"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"))
    attempts: Mapped[int]
    error: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
