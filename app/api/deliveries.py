import base64
import binascii
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import ColumnElement, Row, Select, Update, func, select, true, tuple_, update
from sqlalchemy.orm import Session

from app.api.deps import DbSession, require_api_key
from app.api.schemas import DeliveryOut
from app.models import Delivery, DeliveryAttempt, DeliveryStatus

router = APIRouter(
    prefix="/deliveries",
    tags=["deliveries"],
    dependencies=[Depends(require_api_key)],
)


@router.get("")
def list_deliveries(
    db: DbSession,
    response: Response,
    status_: Annotated[DeliveryStatus | None, Query(alias="status")] = None,
    endpoint_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: Annotated[
        str | None, Query(description="X-Next-Cursor from the previous page.")
    ] = None,
) -> list[DeliveryOut]:
    """Newest first, each with its last attempt. `?status=dead` is the dead-letter list.

    X-Total-Count is how many match the filters in all. While more pages remain,
    X-Next-Cursor is set: pass it as `cursor` to get the next one.
    """
    filters = []
    if status_ is not None:
        filters.append(Delivery.status == status_)
    if endpoint_id is not None:
        filters.append(Delivery.endpoint_id == endpoint_id)
    response.headers["X-Total-Count"] = str(
        db.scalar(select(func.count()).select_from(Delivery).where(*filters))
    )

    query = _with_last_attempt(select(Delivery)).where(*filters)
    if cursor is not None:
        # Keyset, not offset: replays taking rows off the list don't shift the next page.
        query = query.where(tuple_(Delivery.created_at, Delivery.id) < _decode_cursor(cursor))
    rows = db.execute(
        query.order_by(Delivery.created_at.desc(), Delivery.id.desc()).limit(limit + 1)
    ).all()
    # One row past the page tells whether there's another page, without a second count.
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1][0]
        response.headers["X-Next-Cursor"] = _encode_cursor(last.created_at, last.id)
    return [_to_out(row) for row in rows]


def _encode_cursor(created_at: datetime, delivery_id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(f"{created_at.isoformat()}|{delivery_id}".encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        created_at, delivery_id = base64.urlsafe_b64decode(cursor).decode().split("|")
        return datetime.fromisoformat(created_at), uuid.UUID(delivery_id)
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid cursor"
        ) from exc


@router.post("/{delivery_id}/replay")
def replay_delivery(delivery_id: uuid.UUID, db: DbSession) -> DeliveryOut:
    """Send a dead delivery again, with a fresh set of attempts. Past attempts stay logged."""
    replayed = db.scalar(replay_dead(Delivery.id == delivery_id))
    if replayed is None:
        # Nothing matched: tell a missing delivery apart from one that isn't dead.
        current = db.get(Delivery, delivery_id)
        if current is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delivery not found")
        # Pending or in_progress is already being sent; succeeded was received.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Only dead deliveries can be replayed; this one is {current.status}",
        )
    db.commit()
    return _get_out(db, delivery_id)


def replay_dead(*criteria: ColumnElement[bool]) -> Update:
    """UPDATE that sets matching dead deliveries back to pending, due now, returning their ids.

    Conditional on status = dead in the same statement, so two replays of one delivery can't
    both succeed, and a delivery a worker holds (in_progress) is never touched.
    """
    return (
        update(Delivery)
        .where(Delivery.status == DeliveryStatus.DEAD, *criteria)
        .values(
            status=DeliveryStatus.PENDING,
            attempt_count=0,
            # The database clock, like the scheduler that compares against it.
            next_attempt_at=func.now(),
            locked_until=None,
        )
        .returning(Delivery.id)
    )


def _with_last_attempt(query: Select) -> Select:
    # LATERAL: one index lookup per delivery on (delivery_id, created_at) for its newest attempt.
    last = (
        select(
            DeliveryAttempt.status_code.label("last_status_code"),
            DeliveryAttempt.error.label("last_error"),
            DeliveryAttempt.created_at.label("last_attempt_at"),
        )
        .where(DeliveryAttempt.delivery_id == Delivery.id)
        .order_by(DeliveryAttempt.created_at.desc())
        .limit(1)
        .lateral()
    )
    return query.add_columns(
        last.c.last_status_code, last.c.last_error, last.c.last_attempt_at
    ).outerjoin(last, true())


def _to_out(row: Row) -> DeliveryOut:
    delivery, last_status_code, last_error, last_attempt_at = row
    return DeliveryOut.model_validate(delivery).model_copy(
        update={
            "last_status_code": last_status_code,
            "last_error": last_error,
            "last_attempt_at": last_attempt_at,
        }
    )


def _get_out(db: Session, delivery_id: uuid.UUID) -> DeliveryOut:
    # populate_existing: the identity map may hold the row as it was before the UPDATE.
    query = _with_last_attempt(select(Delivery)).where(Delivery.id == delivery_id)
    return _to_out(db.execute(query.execution_options(populate_existing=True)).one())
