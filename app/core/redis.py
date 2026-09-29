from redis import Redis

from app.core.config import get_settings

# Lazy: nothing connects until the first command.
redis_client = Redis.from_url(
    get_settings().redis_url,
    socket_connect_timeout=2,
    socket_timeout=2,
)


def get_redis() -> Redis:
    return redis_client
