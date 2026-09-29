import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy import Uuid, any_, func, insert, literal, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, joinedload, selectinload

from app.api.deps import DbSession, require_api_key
from app.api.schemas import EventAccepted, EventCreate, EventOut, EventSummary, StatusCounts
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


@router.get("")
def list_events(
    db: DbSession, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[EventSummary]:
    """The most recent events, newest first, each with its delivery counts by status."""
    events = db.scalars(
        select(Event).order_by(Event.created_at.desc(), Event.id).limit(limit)
    ).all()
    counts: dict[uuid.UUID, StatusCounts] = {event.id: {} for event in events}
    rows = db.execute(
        select(Delivery.event_id, Delivery.status, func.count())
        .where(Delivery.event_id.in_(counts))
        .group_by(Delivery.event_id, Delivery.status)
    )
    for event_id, delivery_status, count in rows:
        counts[event_id][delivery_status] = count
    return [
        EventSummary(
            id=event.id, type=event.type, created_at=event.created_at, deliveries=counts[event.id]
        )
        for event in events
    ]


@router.get("/{event_id}")
def get_event(event_id: uuid.UUID, db: DbSession) -> EventOut:
    """The event with every delivery and every attempt: the answer to "did you send it?"."""
    event = db.scalar(
        select(Event)
        .where(Event.id == event_id)
        # A fixed number of queries however many deliveries and attempts there are.
        .options(
            selectinload(Event.deliveries).options(
                joinedload(Delivery.endpoint), selectinload(Delivery.attempts)
            )
        )
    )
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
    return EventOut.model_validate(event)


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
