# Import every model module here so Alembic autogenerate sees its tables.
from app.models.base import Base

__all__ = ["Base"]
