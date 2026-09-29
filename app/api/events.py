from typing import Annotated

from fastapi import APIRouter, Depends, Header, Response, status
from sqlalchemy import Uuid, any_, insert, literal, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.api.deps import DbSession, require_api_key
from app.api.schemas import EventAccepted, EventCreate
from app.models import Delivery, Endpoint, Event

router = APIRouter(
    prefix="/events",
    tags=["events"],
    dependencies=[Depends(require_api_key)],
)


@router.post("", status_code=status.HTTP_202_ACCEPTED)
def create_event(
    body: EventCreate,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
    db: DbSession,
    response: Response,
) -> EventAccepted:
    """Store the event and its pending deliveries in one transaction; delivery happens later."""
    event = db.scalar(
        pg_insert(Event)
        .values(type=body.type, payload=body.payload, idempotency_key=idempotency_key)
        .on_conflict_do_nothing(index_elements=[Event.idempotency_key])
        .returning(Event)
    )
    if event is None:
        # Key already used: the unique constraint stopped the insert, so return the original.
        original = db.scalars(select(Event).where(Event.idempotency_key == idempotency_key)).one()
        response.headers["Idempotent-Replayed"] = "true"
        return EventAccepted.model_validate(original)

    _fan_out(db, event)
    # Event and deliveries commit together (outbox): never an event that can't be sent.
    db.commit()
    return EventAccepted.model_validate(event)


def _fan_out(db: Session, event: Event) -> None:
    """One pending delivery per active endpoint subscribed to this event type."""
    matching_endpoints = select(literal(event.id, Uuid), Endpoint.id).where(
        Endpoint.is_active, literal(event.type) == any_(Endpoint.event_types)
    )
    # include_defaults=False: a Python-side uuid4 default would be evaluated once and
    # shared by every row, so ids, status and next_attempt_at come from server defaults.
    db.execute(
        insert(Delivery).from_select(
            ["event_id", "endpoint_id"], matching_endpoints, include_defaults=False
        )
    )
