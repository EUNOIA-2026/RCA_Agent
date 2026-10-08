import os


class ConfigError(RuntimeError):
    pass


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None


class Settings:
    def __init__(self) -> None:
        self.database_url = _required("DATABASE_URL")
        self.redis_url = _required("REDIS_URL")
        self.db_pool_size = _int("DB_POOL_SIZE", 5)
        self.slow_query_seconds = _int("SLOW_QUERY_SECONDS", 3)
        self.chaos_enabled = os.getenv("CHAOS_ENABLED", "false").lower() == "true"
        self.initial_faults = {
            item.strip()
            for item in os.getenv("FAULTS", "").split(",")
            if item.strip()
        }
