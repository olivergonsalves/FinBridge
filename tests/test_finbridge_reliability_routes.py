"""Read-only Flask endpoint integration tests using an isolated SQLite database.

Run: python/.venv312/bin/python -m pytest -v tests/test_finbridge_reliability_routes.py
"""
import sqlite3

import pytest
from flask import Flask

import finbridge_reliability_routes as routes


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "isolated.db"
    with sqlite3.connect(db) as conn:
        conn.execute("""
            CREATE TABLE diagnostic_events (
                id INTEGER PRIMARY KEY,
                checked_at TEXT,
                institution TEXT,
                connection_status TEXT,
                error_type TEXT,
                error_code TEXT,
                request_id TEXT
            )
        """)
        sample = [
            (5, "2026-10-23T05:00:00+00:00", "First Platypus Bank", "healthy", None, None, "req-5"),
            (6, "2026-10-23T06:00:00+00:00", "First Platypus Bank", "action_required", "ITEM_ERROR", "ITEM_LOGIN_REQUIRED", "req-6"),
            (7, "2026-10-23T07:00:00+00:00", "First Platypus Bank", "action_required", "ITEM_ERROR", "ITEM_LOGIN_REQUIRED", "req-7"),
            (8, "2026-10-23T08:00:00+00:00", "First Platypus Bank", "healthy", None, None, "req-8"),
        ]
        conn.executemany("INSERT INTO diagnostic_events VALUES (?, ?, ?, ?, ?, ?, ?)", sample)
    monkeypatch.setattr(routes, "DB_PATH", db)
    app = Flask(__name__)
    app.register_blueprint(routes.reliability_bp)
    app.config["TESTING"] = True
    return app.test_client(), db


def test_incident_list_returns_expected_summary(client):
    browser, _ = client
    response = browser.get("/api/reliability/incidents")
    assert response.status_code == 200
    data = response.get_json()
    assert data["total_incidents"] == 1
    assert data["open_incidents"] == 0
    assert data["incidents"][0]["incident_id"] == "INC-0006"
    assert data["incidents"][0]["status"] == "resolved"
    assert data["incidents"][0]["failure_checks"] == 2
    assert "timeline" not in data["incidents"][0]


def test_incident_detail_returns_full_timeline(client):
    browser, _ = client
    response = browser.get("/api/reliability/incidents/INC-0006")
    assert response.status_code == 200
    body = response.get_json()
    assert body["incident"]["resolution_event_id"] == 8
    assert [event["id"] for event in body["timeline"]] == [6, 7, 8]
    assert [event["request_id"] for event in body["timeline"]] == ["req-6", "req-7", "req-8"]


def test_unknown_incident_returns_404(client):
    browser, _ = client
    result = browser.get("/api/reliability/incidents/INC-9999")
    assert result.status_code == 404
    assert result.get_json()["error"] == "Incident not found"


def test_read_only_calls_do_not_change_diagnostic_events(client):
    browser, db = client
    with sqlite3.connect(db) as conn:
        before = conn.execute("SELECT COUNT(*) FROM diagnostic_events").fetchone()[0]
    browser.get("/api/reliability/incidents")
    browser.get("/api/reliability/incidents/INC-0006")
    with sqlite3.connect(db) as conn:
        after = conn.execute("SELECT COUNT(*) FROM diagnostic_events").fetchone()[0]
    assert before == after == 4
