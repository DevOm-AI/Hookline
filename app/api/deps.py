from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.core.security import api_key_matches

DbSession = Annotated[Session, Depends(get_db)]

# auto_error=False so a missing header gets the same 401 as a wrong key.
bearer = HTTPBearer(auto_error=False, description="Hookline API key")


def require_api_key(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    # Fails closed: with no API_KEY_HASH configured, nothing gets in.
    if (
        credentials is None
        or settings.api_key_hash is None
        or not api_key_matches(credentials.credentials, settings.api_key_hash)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
