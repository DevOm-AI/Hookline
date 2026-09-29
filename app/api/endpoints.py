import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select

from app.api.deps import DbSession, require_api_key
from app.api.schemas import EndpointCreate, EndpointCreated, EndpointOut, EndpointUpdate
from app.core.security import generate_endpoint_secret
from app.models import Endpoint

router = APIRouter(
    prefix="/endpoints",
    tags=["endpoints"],
    dependencies=[Depends(require_api_key)],
)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_endpoint(body: EndpointCreate, db: DbSession) -> EndpointCreated:
    endpoint = Endpoint(
        url=str(body.url),
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


def _get_or_404(db: DbSession, endpoint_id: uuid.UUID) -> Endpoint:
    endpoint = db.get(Endpoint, endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint not found")
    return endpoint
