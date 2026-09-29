import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select

from app.api.deliveries import replay_dead
from app.api.deps import DbSession, require_api_key
from app.api.schemas import (
    EndpointCreate,
    EndpointCreated,
    EndpointOut,
    EndpointStats,
    EndpointUpdate,
    ReplayedDeliveries,
    StatusCounts,
)
from app.core.config import Settings, get_settings
from app.core.security import generate_endpoint_secret
from app.core.url_safety import UnsafeURLError, ensure_public_url
from app.models import Delivery, DeliveryStatus, Endpoint

router = APIRouter(
    prefix="/endpoints",
    tags=["endpoints"],
    dependencies=[Depends(require_api_key)],
)

STATS_WINDOW = timedelta(hours=24)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_endpoint(
    body: EndpointCreate,
    db: DbSession,
    settings: Annotated[Settings, Depends(get_settings)],
) -> EndpointCreated:
    url = str(body.url)
    try:
        # Localhost receivers are only for local development.
        ensure_public_url(url, allow_loopback=settings.debug)
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
    """Per endpoint: deliveries created in the last 24 hours by status, and the success rate."""
    # One query, so an endpoint created meanwhile can't show up in one half and not the other.
    rows = db.execute(
        select(Endpoint.id, Delivery.status, func.count(Delivery.id))
        .outerjoin(
            Delivery,
            (Delivery.endpoint_id == Endpoint.id)
            & (Delivery.created_at >= func.now() - STATS_WINDOW),
        )
        .group_by(Endpoint.id, Delivery.status)
        .order_by(Endpoint.id)
    )
    counts: dict[uuid.UUID, StatusCounts] = {}
    for endpoint_id, delivery_status, count in rows:
        by_status = counts.setdefault(endpoint_id, {})
        # An endpoint with no deliveries in the window comes back once, with a NULL status.
        if delivery_status is not None:
            by_status[delivery_status] = count
    return [
        EndpointStats(
            endpoint_id=endpoint_id,
            deliveries=by_status,
            success_rate=_success_rate(by_status),
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
def update_endpoint(endpoint_id: uuid.UUID, body: EndpointUpdate, db: DbSession) -> EndpointOut:
    endpoint = _get_or_404(db, endpoint_id)
    endpoint.is_active = body.is_active
    db.commit()
    return EndpointOut.model_validate(endpoint)


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


def _get_or_404(db: DbSession, endpoint_id: uuid.UUID) -> Endpoint:
    endpoint = db.get(Endpoint, endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint not found")
    return endpoint
