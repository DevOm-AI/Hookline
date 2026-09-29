import uuid
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import AwareDatetime
from sqlalchemy import Uuid, exists, func, literal, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.api.deliveries import replay_dead
from app.api.deps import DbSession, require_api_key
from app.api.schemas import (
    EndpointCreate,
    EndpointCreated,
    EndpointOut,
    EndpointStats,
    EndpointUpdate,
    EndpointUpdated,
    RecoveredDeliveries,
    ReplayedDeliveries,
    StatusCounts,
)
from app.core.config import Settings, get_settings
from app.core.security import generate_endpoint_secret
from app.core.url_safety import UnsafeURLError, ensure_public_url
from app.models import Delivery, DeliveryStatus, Endpoint, Event

router = APIRouter(
    prefix="/endpoints",
    tags=["endpoints"],
    dependencies=[Depends(require_api_key)],
)

STATS_WINDOW = timedelta(hours=24)
RECOVER_BATCH_SIZE = 1000
# An event whose transaction began just before a pause has an earlier created_at, yet its
# fan-out can run after the pause commits and skip the endpoint. The default recovery window
# starts this much earlier to cover it; events that already have a delivery are skipped.
PAUSE_RACE_MARGIN = timedelta(minutes=1)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_endpoint(
    body: EndpointCreate,
    db: DbSession,
    settings: Annotated[Settings, Depends(get_settings)],
) -> EndpointCreated:
    url = str(body.url)
    try:
        # Localhost receivers are only for local development.
        ensure_public_url(
            url, allow_loopback=settings.debug, allowed_hosts=settings.allowed_internal_hosts
        )
    except UnsafeURLError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    endpoint = Endpoint(
        url=url,
        event_types=body.event_types,
        secret=generate_endpoint_secret(),
    )
    db.add(endpoint)
    db.commit()
    return EndpointCreated.model_validate(endpoint)


@router.get("")
def list_endpoints(db: DbSession) -> list[EndpointOut]:
    endpoints = db.scalars(select(Endpoint).order_by(Endpoint.created_at.desc(), Endpoint.id))
    return [EndpointOut.model_validate(endpoint) for endpoint in endpoints]


# Declared before /{endpoint_id}, which would otherwise match "stats" and reject it as a bad id.
@router.get("/stats")
def endpoint_stats(db: DbSession) -> list[EndpointStats]:
    """Per endpoint: deliveries created in the last 24 hours by status, the success rate, and
    how many are dead in all, whatever their age: what replay-dead would replay."""
    dead_totals = (
        select(Delivery.endpoint_id, func.count().label("dead_total"))
        .where(Delivery.status == DeliveryStatus.DEAD)
        .group_by(Delivery.endpoint_id)
        .subquery()
    )
    # One statement, so an endpoint created meanwhile can't show up in one part and not another.
    rows = db.execute(
        select(
            Endpoint.id,
            func.coalesce(dead_totals.c.dead_total, 0),
            Delivery.status,
            func.count(Delivery.id),
        )
        .outerjoin(
            Delivery,
            (Delivery.endpoint_id == Endpoint.id)
            & (Delivery.created_at >= func.now() - STATS_WINDOW),
        )
        .outerjoin(dead_totals, dead_totals.c.endpoint_id == Endpoint.id)
        .group_by(Endpoint.id, dead_totals.c.dead_total, Delivery.status)
        .order_by(Endpoint.id)
    )
    counts: dict[uuid.UUID, StatusCounts] = {}
    dead_total: dict[uuid.UUID, int] = {}
    for endpoint_id, dead, delivery_status, count in rows:
        by_status = counts.setdefault(endpoint_id, {})
        dead_total[endpoint_id] = dead
        # An endpoint with no deliveries in the window comes back once, with a NULL status.
        if delivery_status is not None:
            by_status[delivery_status] = count
    return [
        EndpointStats(
            endpoint_id=endpoint_id,
            deliveries=by_status,
            success_rate=_success_rate(by_status),
            dead_total=dead_total[endpoint_id],
        )
        for endpoint_id, by_status in counts.items()
    ]


def _success_rate(by_status: StatusCounts) -> float | None:
    # Pending and in-progress deliveries haven't finished, so they count for neither side.
    succeeded = by_status.get(DeliveryStatus.SUCCEEDED, 0)
    finished = succeeded + by_status.get(DeliveryStatus.DEAD, 0)
    return succeeded / finished if finished else None


@router.get("/{endpoint_id}")
def get_endpoint(endpoint_id: uuid.UUID, db: DbSession) -> EndpointOut:
    return EndpointOut.model_validate(_get_or_404(db, endpoint_id))


@router.patch("/{endpoint_id}")
def update_endpoint(endpoint_id: uuid.UUID, body: EndpointUpdate, db: DbSession) -> EndpointUpdated:
    """Pause or resume. Resuming sends what was waiting, but not the events that arrived
    while paused: recover those with POST /endpoints/{id}/recover, from `recover_since`."""
    endpoint = _get_or_404(db, endpoint_id)
    recover_from = None
    if body.is_active:
        if not endpoint.is_active:
            recover_from = _default_recover_since(endpoint)
        endpoint.paused_at = None
        # A fresh start: otherwise the old failing streak would pause it again within a minute.
        endpoint.failing_since = None
        endpoint.auto_paused_at = None
    elif endpoint.is_active:
        # Only when it actually pauses: pausing again mustn't move the start of the gap.
        endpoint.paused_at = func.now()
    endpoint.is_active = body.is_active
    db.commit()
    return EndpointUpdated(
        **EndpointOut.model_validate(endpoint).model_dump(), recover_since=recover_from
    )


@router.delete("/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_endpoint(endpoint_id: uuid.UUID, db: DbSession) -> Response:
    # Its deliveries and attempts go with it (ON DELETE CASCADE).
    db.delete(_get_or_404(db, endpoint_id))
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{endpoint_id}/replay-dead")
def replay_dead_deliveries(endpoint_id: uuid.UUID, db: DbSession) -> ReplayedDeliveries:
    """Send every dead delivery of this endpoint again, e.g. once it's back after an outage.

    If the endpoint is paused, they wait as pending until it's resumed.
    """
    _get_or_404(db, endpoint_id)
    replayed = db.scalars(replay_dead(Delivery.endpoint_id == endpoint_id)).all()
    db.commit()
    return ReplayedDeliveries(replayed=len(replayed))


@router.post("/{endpoint_id}/recover")
def recover_deliveries(
    endpoint_id: uuid.UUID,
    db: DbSession,
    since: Annotated[
        AwareDatetime | None,
        Query(description="Default: while paused, a minute before paused_at."),
    ] = None,
) -> RecoveredDeliveries:
    """Create pending deliveries for events that arrived while this endpoint was paused.

    Every event of a subscribed type created since `since` that has no delivery for this
    endpoint gets one. Safe to repeat: the unique (event_id, endpoint_id) constraint and ON
    CONFLICT DO NOTHING make a second run create nothing. If the endpoint is still paused,
    they wait as pending until it's resumed.
    """
    endpoint = _get_or_404(db, endpoint_id)
    if since is None:
        since = _default_recover_since(endpoint)
        if since is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Endpoint isn't paused: pass `since` (recover_since from the resume)",
            )
    created = _create_missing_deliveries(db, endpoint, since)
    return RecoveredDeliveries(created=created, since=since)


def _default_recover_since(endpoint: Endpoint) -> datetime | None:
    if endpoint.paused_at is None:
        return None
    # Never before it existed: older events were never meant for it.
    return max(endpoint.paused_at - PAUSE_RACE_MARGIN, endpoint.created_at)


def _create_missing_deliveries(
    db: Session, endpoint: Endpoint, since: datetime, batch_size: int = RECOVER_BATCH_SIZE
) -> int:
    """Insert the missing deliveries a batch at a time, committing each batch.

    A long pause can leave many events behind; one transaction for all of them would hold
    its locks and grow its WAL for as long as it runs. Each batch picks up where the last
    one stopped, in (created_at, id) order, so rows inserted meanwhile don't shift it.
    """
    missing = (
        select(Event.created_at, Event.id)
        .where(
            Event.type.in_(endpoint.event_types),
            Event.created_at >= since,
            ~exists().where(Delivery.event_id == Event.id, Delivery.endpoint_id == endpoint.id),
        )
        .order_by(Event.created_at, Event.id)
        .limit(batch_size)
    )
    created = 0
    after = None
    while True:
        batch = db.execute(
            missing if after is None else missing.where(tuple_(Event.created_at, Event.id) > after)
        ).all()
        if not batch:
            return created
        # include_defaults=False: ids, status and next_attempt_at come from server defaults,
        # as in fan-out. A concurrent recover or fan-out may insert a row first; skip it.
        inserted = db.scalars(
            pg_insert(Delivery)
            .from_select(
                ["event_id", "endpoint_id"],
                select(Event.id, literal(endpoint.id, Uuid)).where(
                    Event.id.in_([event_id for _, event_id in batch])
                ),
                include_defaults=False,
            )
            .on_conflict_do_nothing(index_elements=["event_id", "endpoint_id"])
            # Only rows actually inserted come back, so skipped conflicts aren't counted.
            .returning(Delivery.id)
        ).all()
        db.commit()
        created += len(inserted)
        if len(batch) < batch_size:
            return created
        after = tuple(batch[-1])


def _get_or_404(db: DbSession, endpoint_id: uuid.UUID) -> Endpoint:
    endpoint = db.get(Endpoint, endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint not found")
    return endpoint
