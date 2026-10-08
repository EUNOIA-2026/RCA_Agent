import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

os.environ.setdefault("DATABASE_URL", "postgresql://unused")
os.environ.setdefault("REDIS_URL", "redis://unused")

from app import create_app  # noqa: E402
from config import ConfigError, Settings  # noqa: E402


class FakeStore:
    def __init__(self, settings=None, faults=None):
        self.orders = []

    def list_orders(self):
        return self.orders

    def get_order(self, order_id):
        return next((o for o in self.orders if o["id"] == order_id), None)

    def cache_get(self, order_id):
        return None

    def cache_set(self, order_id, payload):
        pass

    def create_order(self, customer, amount):
        self.orders.append(
            {"id": len(self.orders) + 1, "customer": customer, "amount": amount, "status": "pending"}
        )
        return len(self.orders)

    def enqueue(self, order_id):
        pass


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("app.Store", FakeStore)
    return create_app().test_client()


def test_health(client):
    assert client.get("/health").status_code == 200


def test_create_and_get_order(client):
    created = client.post("/orders", json={"customer": "acme", "amount": 10})
    assert created.status_code == 201

    fetched = client.get("/orders/1")
    assert fetched.status_code == 200
    assert fetched.get_json()["customer"] == "acme"


def test_summary_with_orders(client):
    client.post("/orders", json={"customer": "acme", "amount": 10})
    assert client.get("/orders/summary").get_json()["average"] == 10


def test_summary_with_no_orders_returns_200(client):
    assert client.get("/orders/summary").status_code == 200


def test_unknown_order_is_not_a_server_error(client):
    assert client.get("/orders/999").status_code == 404


def test_missing_config_raises(monkeypatch):
    monkeypatch.delenv("REDIS_URL")
    with pytest.raises(ConfigError):
        Settings()
