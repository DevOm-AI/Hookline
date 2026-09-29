from pathlib import Path

import yaml

from app.workers.scheduler import LOCK_DURATION

COMPOSE_FILE = Path(__file__).resolve().parent.parent / "docker-compose.yml"


def worker_service() -> dict:
    return yaml.safe_load(COMPOSE_FILE.read_text())["services"]["worker"]


def seconds(duration: str) -> int:
    """A compose duration like "60s" or "1m"."""
    units = {"s": 1, "m": 60}
    return int(duration[:-1]) * units[duration[-1]]


def test_worker_gets_as_long_as_its_lock_to_finish_on_docker_stop():
    """Docker's default 10s before SIGKILL would cut a send that uses its timeouts short."""
    assert seconds(worker_service()["stop_grace_period"]) >= LOCK_DURATION.total_seconds()


def test_sigterm_reaches_celery_directly():
    """Celery must be the container's main process, not wrapped in a shell that would swallow
    SIGTERM, so docker stop triggers its warm shutdown."""
    command = worker_service()["command"]
    assert command.split()[0] == "celery"
