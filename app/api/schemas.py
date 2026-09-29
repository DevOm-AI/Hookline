import uuid
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, HttpUrl, StringConstraints

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
