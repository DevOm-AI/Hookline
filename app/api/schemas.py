import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, HttpUrl, StringConstraints

from app.models import DeliveryStatus

EventType = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


class EndpointCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    event_types: Annotated[
        list[EventType], Field(min_length=1, max_length=100), AfterValidator(_dedupe)
    ]


class EndpointUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_active: bool = Field(description="false pauses deliveries to this endpoint, true resumes.")


class EndpointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    url: str
    event_types: list[str]
    is_active: bool
    created_at: datetime


class EndpointCreated(EndpointOut):
    secret: str = Field(description="Signing secret. Returned only once, so store it now.")


class EventCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: EventType
    payload: dict[str, Any]


class EventAccepted(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: str
    created_at: datetime


class DeliveryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_id: uuid.UUID
    endpoint_id: uuid.UUID
    status: DeliveryStatus
    attempt_count: int
    next_attempt_at: datetime
    created_at: datetime
    # From the most recent attempt; all None if it hasn't been tried yet.
    last_status_code: int | None = Field(None, description="None if there was no response.")
    last_error: str | None = None
    last_attempt_at: datetime | None = None


class ReplayedDeliveries(BaseModel):
    replayed: int = Field(description="How many dead deliveries were set back to pending.")
