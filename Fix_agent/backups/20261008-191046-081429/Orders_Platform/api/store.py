import json
import logging
import time

import redis
from psycopg2 import pool as pg_pool

logger = logging.getLogger("orders.store")

QUEUE_KEY = "orders:queue"


class Faults:
    """Runtime fault switches, toggled by env (FAULTS) or the chaos endpoints."""

    def __init__(self, initial: set[str]) -> None:
        self.redis_down = "redis_down" in initial
        self.db_delay_seconds = 5.0 if "db_slow" in initial else 0.0


class Store:
    def __init__(self, settings, faults: Faults) -> None:
        self.settings = settings
        self.faults = faults
        self.local_cache: dict[str, object] = {}
        self._pool = None
        self._redis = None

    # -- connections -------------------------------------------------------

    def pool(self):
        if self._pool is None:
            self._pool = pg_pool.ThreadedConnectionPool(
                1,
                self.settings.db_pool_size,
                self.settings.database_url,
            )
            self._init_schema()
        return self._pool

    def _init_schema(self) -> None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS orders ("
                    "id SERIAL PRIMARY KEY, "
                    "customer TEXT, "
                    "amount NUMERIC NOT NULL DEFAULT 0, "
                    "status TEXT NOT NULL DEFAULT 'pending', "
                    "created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                )
            conn.commit()
        finally:
            self._pool.putconn(conn)

    def redis_client(self):
        if self.faults.redis_down:
            raise redis.ConnectionError("Connection refused (fault: redis_down)")

        if self._redis is None:
            self._redis = redis.Redis.from_url(self.settings.redis_url)

        return self._redis

    # -- database ----------------------------------------------------------

    def _query(self, sql: str, params=(), fetch: str | None = None):
        started = time.monotonic()

        if self.faults.db_delay_seconds:
            time.sleep(self.faults.db_delay_seconds)

        conn = self.pool().getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                result = None

                if fetch == "all":
                    result = cur.fetchall()
                elif fetch == "one":
                    result = cur.fetchone()

            conn.commit()
            return result
        finally:
            self.pool().putconn(conn)

            elapsed = time.monotonic() - started
            if elapsed > self.settings.slow_query_seconds:
                logger.error(
                    "DEPENDENCY_ERROR database query took %.1fs (threshold %ss)",
                    elapsed,
                    self.settings.slow_query_seconds,
                )

    def ping_db(self) -> None:
        self._query("SELECT 1", fetch="one")

    def list_orders(self) -> list[dict]:
        rows = self._query(
            "SELECT id, customer, amount, status FROM orders ORDER BY id",
            fetch="all",
        )
        return [_order_dict(row) for row in rows]

    def get_order(self, order_id: int) -> dict | None:
        row = self._query(
            "SELECT id, customer, amount, status FROM orders WHERE id = %s",
            (order_id,),
            fetch="one",
        )
        return _order_dict(row) if row else None

    def create_order(self, customer: str | None, amount: float) -> int:
        row = self._query(
            "INSERT INTO orders (customer, amount) VALUES (%s, %s) RETURNING id",
            (customer, amount),
            fetch="one",
        )
        return row[0]

    def clear_orders(self) -> None:
        self._query("TRUNCATE orders RESTART IDENTITY")

    def export_orders(self) -> str:
        conn = self.pool().getconn()
        cur = conn.cursor()
        cur.execute("SELECT id, customer, amount FROM orders ORDER BY id")

        lines = []
        for order_id, customer, amount in cur.fetchall():
            lines.append(f"{order_id},{customer.strip()},{amount}")

        self.pool().putconn(conn)
        return "\n".join(lines)

    # -- redis -------------------------------------------------------------

    def cache_get(self, order_id: int) -> dict | None:
        raw = self.redis_client().get(f"order:{order_id}")
        return json.loads(raw) if raw else None

    def cache_set(self, order_id: int, payload: dict) -> None:
        self.redis_client().set(f"order:{order_id}", json.dumps(payload))
        self.local_cache[f"order:{order_id}"] = payload

    def enqueue(self, order_id: int) -> None:
        self.redis_client().rpush(QUEUE_KEY, order_id)


def _order_dict(row) -> dict:
    return {
        "id": row[0],
        "customer": row[1],
        "amount": float(row[2]),
        "status": row[3],
    }
