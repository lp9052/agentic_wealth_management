"""Tests for app/main.py — FastAPI endpoints."""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app


@pytest.fixture
def client(monkeypatch):
    """A FastAPI test client with the engine graph mocked out."""
    fake_graph = MagicMock()
    fake_graph.invoke = MagicMock(return_value={
        "proposal": "buy SPY",
        "status": "CERTIFIED_COMPLIANT",
        "critique": "ok",
        "revision_count": 1,
        "fired_rules": [],
    })
    monkeypatch.setattr(main_module, "graph", fake_graph)
    monkeypatch.setattr(main_module, "detect_signals", lambda text: ["HIGH_RISK"])
    return TestClient(app)


def test_health_endpoint(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_propose_trade_supervised(client):
    r = client.post("/propose-trade", json={
        "client_id": "C1",
        "client_data": {"age": 40},
        "prompt": "buy SPY",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["final_proposal"] == "buy SPY"
    assert body["status"] == "CERTIFIED_COMPLIANT"


def test_propose_trade_unsupervised_default_status(client, monkeypatch):
    """When supervisor_enabled=False and the graph returns no `status`,
    the endpoint defaults to CERTIFIED_COMPLIANT."""
    fake_graph = MagicMock()
    fake_graph.invoke = MagicMock(return_value={"proposal": "ok"})
    monkeypatch.setattr(main_module, "graph", fake_graph)

    r = client.post("/propose-trade", json={
        "client_id": "C1",
        "client_data": {},
        "prompt": "buy SPY",
        "supervisor_enabled": False,
    })
    assert r.status_code == 200
    assert r.json()["status"] == "CERTIFIED_COMPLIANT"


def test_propose_trade_supervised_unknown_status_falls_back(client, monkeypatch):
    """When the graph returns no `status` in supervised mode → UNKNOWN."""
    fake_graph = MagicMock()
    fake_graph.invoke = MagicMock(return_value={"proposal": "ok"})
    monkeypatch.setattr(main_module, "graph", fake_graph)

    r = client.post("/propose-trade", json={
        "client_id": "C1",
        "client_data": {},
        "prompt": "buy SPY",
    })
    assert r.status_code == 200
    assert r.json()["status"] == "UNKNOWN"


def test_audit_signal_endpoint(client):
    r = client.post("/audit-signal", json={"text": "buy leveraged SPY"})
    assert r.status_code == 200
    body = r.json()
    assert body["text"] == "buy leveraged SPY"
    assert body["detected_signals"] == ["HIGH_RISK"]
