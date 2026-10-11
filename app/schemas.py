from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models import TicketStatus


class TicketIn(BaseModel):
    external_id: str = Field(min_length=1, max_length=128)
    body: str = Field(min_length=1)
    subject: str | None = Field(default=None, max_length=512)
    source: str | None = Field(default=None, max_length=64)


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
