from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.llm import ROUTES
from app.models import TicketStatus


class TicketIn(BaseModel):
    external_id: str = Field(min_length=1, max_length=128)
    body: str = Field(min_length=1)
    subject: str | None = Field(default=None, max_length=512)
    source: str | None = Field(default=None, max_length=64)


class Approval(BaseModel):
    routed_to: str | None = Field(
        default=None, description="Override the suggested team", examples=["billing-team"]
    )

    @field_validator("routed_to")
    @classmethod
    def known_team(cls, team: str | None) -> str | None:
        if team is not None and team not in ROUTES.values():
            raise ValueError(f"unknown team; expected one of {sorted(ROUTES.values())}")
        return team


class Queued(BaseModel):
    ticket_id: int
    status: TicketStatus
    location: str


class WebhookAccepted(Queued):
    duplicate: bool


class DeadLetterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ticket_id: int
    attempts: int
    error: str
    created_at: datetime


class ClassificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    category: str
    summary: str
    confidence: float
    routed_to: str | None
    decided_by: str
    prompt_version: str


class TicketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    external_id: str
    subject: str | None
    body: str
    source: str | None
    status: TicketStatus
    attempts: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    classification: ClassificationOut | None
