"""Mock receiver: a webhook endpoint for Hookline's load and chaos tests.

It verifies each request's Hookline-Signature, records every Hookline-Event-Id, and can be
told to fail a share of requests or to answer slowly. The chaos test compares the event ids
it received with the events sent: they must match.

A test tool, not part of Hookline: the control routes have no auth, so never expose it.
State lives in memory, in one process: run a single uvicorn worker.

    uvicorn receiver.main:app --port 9000
"""

import asyncio
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import FastAPI, Header, Request, Response, status
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.signing import SIGNATURE_HEADER, verify_signature

FailPercent = Annotated[float, Field(ge=0, le=100)]
# Past Hookline's 10s request timeout, so timeouts can be tested too.
DelayMs = Annotated[int, Field(ge=0, le=60_000)]


class Behaviour(BaseSettings):
    """How the receiver answers. Starts from RECEIVER_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="RECEIVER_")

    # The endpoint's signing secret, as POST /endpoints returned it. Unset = every request 401.
    secret: str | None = Field(default=None, repr=False)
    # Share of validly signed requests answered 500, which Hookline retries.
    fail_percent: FailPercent = 0
    # Wait before answering every validly signed request.
    delay_ms: DelayMs = 0


class BehaviourUpdate(BaseModel):
    """PATCH /config body: only the fields sent change; null leaves a field as it is."""

    secret: Annotated[str, Field(min_length=1)] | None = None
    fail_percent: FailPercent | None = None
    delay_ms: DelayMs | None = None


class BehaviourOut(BaseModel):
    """GET and PATCH /config: the behaviour, with the secret reduced to whether it is set."""

    secret_set: bool
    fail_percent: float
    delay_ms: int


@dataclass
class Received:
    """Every validly signed request for one event."""

    # When the first one arrived, answered 2xx or not: Hookline's first delivery attempt.
    first_seen_at: datetime
    attempts: int = 0
    # Attempts answered 2xx. More than one is a duplicate (at-least-once delivery).
    delivered: int = 0


class Stats(BaseModel):
    """GET /stats."""

    requests: int = Field(description="Every request, signed or not.")
    rejected: int = Field(description="Answered 401 or 400: bad signature or no event id.")
    failed: int = Field(description="Answered 500 on purpose (fail_percent).")
    delivered: int = Field(description="Answered 2xx, duplicates included.")
    unique_events: int = Field(description="Event ids delivered at least once.")
    duplicates: int = Field(description="2xx answers beyond the first for an event id.")
    undelivered_events: int = Field(description="Event ids seen, but never answered 2xx.")


class Records:
    """Everything received since the last reset."""

    def __init__(self) -> None:
        self.events: dict[str, Received] = {}
        self.requests = 0
        self.rejected = 0
        self.failed = 0

    def stats(self) -> Stats:
        """Totals over every request, and over event ids."""
        delivered = sum(received.delivered for received in self.events.values())
        unique = sum(1 for received in self.events.values() if received.delivered)
        return Stats(
            requests=self.requests,
            rejected=self.rejected,
            failed=self.failed,
            delivered=delivered,
            unique_events=unique,
            duplicates=delivered - unique,
            undelivered_events=len(self.events) - unique,
        )


class Receiver:
    """The receiver's state: how it answers, and what it has received."""

    def __init__(self, behaviour: Behaviour) -> None:
        self.behaviour = behaviour
        self.random = random.Random()
        self.records = Records()

    def reset(self) -> None:
        """Start new records. A request still in flight finishes into the old ones."""
        self.records = Records()


def create_app(behaviour: Behaviour | None = None) -> FastAPI:
    """The receiver app, answering as `behaviour` says (default: RECEIVER_* env vars)."""
    app = FastAPI(title="Hookline mock receiver")
    # Every handler is async (sync ones would run in a threadpool) and none awaits between
    # reading and updating this state, so the single event loop needs no locks.
    receiver = Receiver(behaviour or Behaviour())
    app.state.receiver = receiver

    # The only POST route. Hookline delivers with POST, so an endpoint registered at any other
    # path of this receiver gets a 405 and can't change its config or records.
    @app.post("/webhook")
    async def webhook(
        request: Request,
        signature: Annotated[str | None, Header(alias=SIGNATURE_HEADER)] = None,
        event_id: Annotated[str | None, Header(alias="Hookline-Event-Id")] = None,
    ) -> Response:
        """Verify the signature, record the event id, then answer as configured."""
        # Read once, before any await: a reset mid-request leaves this request's counts in
        # the records it started in, not half in the new ones.
        records = receiver.records
        records.requests += 1
        # The raw bytes: re-serialised JSON could differ and fail the check.
        body = await request.body()
        secret = receiver.behaviour.secret
        if secret is None or signature is None or not verify_signature(secret, body, signature):
            records.rejected += 1
            return Response(status_code=status.HTTP_401_UNAUTHORIZED)
        if not event_id:
            records.rejected += 1
            return Response(status_code=status.HTTP_400_BAD_REQUEST)

        received = records.events.get(event_id)
        if received is None:
            received = records.events[event_id] = Received(first_seen_at=datetime.now(UTC))
        received.attempts += 1

        # Read before sleeping, so a PATCH mid-request doesn't change this answer.
        behaviour = receiver.behaviour
        if behaviour.delay_ms:
            await asyncio.sleep(behaviour.delay_ms / 1000)
        if receiver.random.random() * 100 < behaviour.fail_percent:
            records.failed += 1
            return Response(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
        received.delivered += 1
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/config")
    async def get_config() -> BehaviourOut:
        """How the receiver answers now. The secret itself is never shown."""
        behaviour = receiver.behaviour
        return BehaviourOut(
            secret_set=behaviour.secret is not None,
            fail_percent=behaviour.fail_percent,
            delay_ms=behaviour.delay_ms,
        )

    @app.patch("/config")
    async def update_config(update: BehaviourUpdate) -> BehaviourOut:
        """Change how the receiver answers; fields left out keep their value."""
        # Replaced, not changed in place: a request in flight keeps the one it read.
        receiver.behaviour = receiver.behaviour.model_copy(
            update=update.model_dump(exclude_none=True)
        )
        return await get_config()

    @app.get("/stats")
    async def get_stats() -> Stats:
        """Totals since the last reset."""
        return receiver.records.stats()

    @app.get("/received")
    async def get_received() -> dict[str, Received]:
        """Every event id seen, with when it first arrived and how often."""
        return receiver.records.events

    @app.delete("/received", status_code=status.HTTP_204_NO_CONTENT)
    async def reset() -> None:
        """Forget what was received; the config stays."""
        receiver.reset()

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness, for the compose healthcheck."""
        return {"status": "ok"}

    return app


app = create_app()
