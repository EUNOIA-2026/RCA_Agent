import logging
import os
import sys

import psycopg2
import redis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("orders.worker")

QUEUE_KEY = "orders:queue"


def required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        logger.error("CONFIG_ERROR Missing required environment variable: %s", name)
        sys.exit(1)
    return value


DATABASE_URL = required("DATABASE_URL")
REDIS_URL = required("REDIS_URL")


def process(order_id: int) -> None:
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE orders SET status = 'processed' WHERE id = %s",
                (order_id,),
            )
        conn.commit()
    finally:
        conn.close()

    logger.info("processed order %s", order_id)


def main() -> None:
    client = redis.Redis.from_url(REDIS_URL)
    logger.info("worker started, waiting for orders")

    while True:
        try:
            item = client.blpop(QUEUE_KEY, timeout=5)
        except redis.RedisError:
            logger.exception("DEPENDENCY_ERROR lost connection to redis")
            raise

        if item is None:
            continue

        try:
            process(int(item[1]))
        except Exception:
            logger.exception("BACKEND_ERROR failed to process queue item %r", item[1])


if __name__ == "__main__":
    main()
