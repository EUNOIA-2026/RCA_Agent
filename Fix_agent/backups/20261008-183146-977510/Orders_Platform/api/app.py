import logging

import psycopg2
import redis
from flask import Flask, jsonify, request
from psycopg2 import pool as pg_pool
from werkzeug.exceptions import HTTPException

from chaos import create_chaos_blueprint
from config import Settings
from resources import start_resource_monitor
from store import Faults, Store

logger = logging.getLogger("orders.api")


def create_app(start_monitors: bool = False) -> Flask:
    settings = Settings()
    faults = Faults(settings.initial_faults)
    store = Store(settings, faults)

    app = Flask(__name__)
    app.config["store"] = store

    if start_monitors:
        start_resource_monitor()

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.get("/ready")
    def ready():
        try:
            store.ping_db()
            store.redis_client().ping()
        except Exception as exc:
            logger.warning("readiness check failed: %s", exc)
            return jsonify({"status": "unavailable"}), 503

        return jsonify({"status": "ready"})

    @app.get("/orders")
    def list_orders():
        return jsonify(store.list_orders())

    @app.post("/orders")
    def create_order():
        body = request.get_json(silent=True) or {}
        customer = body.get("customer")
        amount = body.get("amount")

        if not customer or not isinstance(amount, (int, float)):
            return jsonify({"error": "customer and numeric amount are required"}), 400

        order_id = store.create_order(customer, amount)
        store.enqueue(order_id)

        return jsonify({"id": order_id, "status": "pending"}), 201

    @app.get("/orders/summary")
    def summary():
        orders = store.list_orders()
        total = sum(order["amount"] for order in orders)
        average = total / len(orders)

        return jsonify(
            {
                "count": len(orders),
                "total": round(total, 2),
                "average": round(average, 2),
            }
        )

    @app.get("/orders/export")
    def export_orders():
        return store.export_orders(), 200, {"Content-Type": "text/csv"}

    @app.get("/orders/<int:order_id>")
    def get_order(order_id: int):
        cached = store.cache_get(order_id)
        if cached:
            return jsonify(cached)

        order = store.get_order(order_id)
        payload = {
            "id": order["id"],
            "customer": order["customer"],
            "amount": order["amount"],
            "status": order["status"],
        }
        store.cache_set(order_id, payload)

        return jsonify(payload)

    app.register_blueprint(create_chaos_blueprint(settings, store, faults))

    @app.errorhandler(Exception)
    def handle_exception(exc: Exception):
        if isinstance(exc, HTTPException):
            return exc

        if isinstance(exc, pg_pool.PoolError):
            tag = "RESOURCE_ERROR"
        elif isinstance(exc, (redis.RedisError, psycopg2.OperationalError)):
            tag = "DEPENDENCY_ERROR"
        else:
            tag = "BACKEND_ERROR"

        logger.exception("%s %s %s failed", tag, request.method, request.path)
        return jsonify({"error": "internal server error"}), 500

    return app
