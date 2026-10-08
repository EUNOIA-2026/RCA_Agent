"""
Chaos endpoints for reproducing operational incidents. Enabled only when
CHAOS_ENABLED=true.

  POST /chaos/clear_orders          empty the orders table      -> GET /orders/summary fails
  GET  /orders/999999               unknown order id            -> unhandled exception
  POST /chaos/redis_down|redis_up   simulate cache/queue outage -> dependency failures
  POST /chaos/db_slow?seconds=N     add latency to every query  -> slow-dependency errors
  POST /chaos/db_normal             remove injected latency
  POST /chaos/memory_leak?mb=N      fill the in-process cache   -> memory pressure / OOMKilled
  POST /chaos/bad_order_row         insert an order with no customer
                                    -> GET /orders/export fails and leaks a DB connection;
                                       repeat until the pool is exhausted
"""

import time

from flask import Blueprint, jsonify, request


def create_chaos_blueprint(settings, store, faults) -> Blueprint:
    bp = Blueprint("chaos", __name__, url_prefix="/chaos")

    @bp.before_request
    def _guard():
        if not settings.chaos_enabled:
            return jsonify({"error": "chaos endpoints are disabled"}), 404

    @bp.get("")
    def state():
        return jsonify(
            {
                "redis_down": faults.redis_down,
                "db_delay_seconds": faults.db_delay_seconds,
                "local_cache_entries": len(store.local_cache),
            }
        )

    @bp.post("/clear_orders")
    def clear_orders():
        store.clear_orders()
        return jsonify({"status": "orders cleared"})

    @bp.post("/redis_down")
    def redis_down():
        faults.redis_down = True
        return jsonify({"status": "redis marked unavailable"})

    @bp.post("/redis_up")
    def redis_up():
        faults.redis_down = False
        return jsonify({"status": "redis available"})

    @bp.post("/db_slow")
    def db_slow():
        faults.db_delay_seconds = float(request.args.get("seconds", "5"))
        return jsonify({"status": f"db delay {faults.db_delay_seconds}s"})

    @bp.post("/db_normal")
    def db_normal():
        faults.db_delay_seconds = 0.0
        return jsonify({"status": "db delay removed"})

    @bp.post("/memory_leak")
    def memory_leak():
        megabytes = int(request.args.get("mb", "50"))
        store.local_cache[f"leak-{time.time_ns()}"] = b"x" * (megabytes * 1024 * 1024)
        return jsonify({"status": f"cached {megabytes}MB", "entries": len(store.local_cache)})

    @bp.post("/bad_order_row")
    def bad_order_row():
        order_id = store.create_order(None, 1)
        return jsonify({"status": "inserted order without customer", "id": order_id})

    return bp
