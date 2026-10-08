import logging
import os
import threading
import time

logger = logging.getLogger("orders.resources")

MEMORY_WARN_RATIO = float(os.getenv("MEMORY_WARN_RATIO", "0.8"))
CHECK_INTERVAL_SECONDS = 10


def _read_int(path: str) -> int | None:
    try:
        raw = open(path, encoding="utf-8").read().strip()
        value = int(raw)
    except (OSError, ValueError):
        return None

    # cgroup v1 reports a huge number when unlimited.
    return value if value < (1 << 50) else None


def memory_usage_and_limit() -> tuple[int | None, int | None]:
    usage = _read_int("/sys/fs/cgroup/memory.current") or _read_int(
        "/sys/fs/cgroup/memory/memory.usage_in_bytes"
    )
    limit = _read_int("/sys/fs/cgroup/memory.max") or _read_int(
        "/sys/fs/cgroup/memory/memory.limit_in_bytes"
    )
    return usage, limit


def _monitor() -> None:
    while True:
        usage, limit = memory_usage_and_limit()

        if usage and limit and usage / limit >= MEMORY_WARN_RATIO:
            logger.error(
                "RESOURCE_ERROR memory usage %dMi is %.0f%% of the %dMi container limit",
                usage // (1024 * 1024),
                100 * usage / limit,
                limit // (1024 * 1024),
            )

        time.sleep(CHECK_INTERVAL_SECONDS)


def start_resource_monitor() -> None:
    threading.Thread(target=_monitor, daemon=True).start()
