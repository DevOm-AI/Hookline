import uuid
from datetime import datetime
from typing import Annotated

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, mapped_column

# Fixed constraint names (e.g. uq_events_idempotency_key) so later migrations
# can drop or rename them reliably instead of guessing Postgres' generated names.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# UUIDs are made in Python so the id is known before insert; the server default
# covers raw SQL inserts. Ids are public (URLs, Hookline-Event-Id), so no counters.
UUIDPk = Annotated[
    uuid.UUID,
    mapped_column(primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()),
]
CreatedAt = Annotated[datetime, mapped_column(server_default=func.now())]


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {datetime: DateTime(timezone=True)}
