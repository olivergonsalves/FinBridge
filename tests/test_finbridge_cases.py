"""Isolated tests for FinBridge's incident case-management API.

Run from the project root:
    python/.venv312/bin/python -m pytest -q tests/test_finbridge_cases.py

Tests use an independent temporary SQLite database. They never contact Plaid
or modify the application's real finbridge.db.
"""

import sqlite3

import pytest
from flask import Flask

import finbridge_case_management as case_module


@pytest.fixture
def client(tmp_path, monkeypatch):
    # The case-management module reads DB_PATH for every connection.
    test_db = tmp_path / "finbridge_test.db"
    monkeypatch.setattr(case_module, "DB_PATH", test_db)

    # Recreate only the minimal diagnostic table required to recognize incidents.
    with sqlite3.connect(test_db) as conn:
        conn.execute(
            "CREATE TABLE diagnostic_events "
            "(id INTEGER PRIMARY KEY, connection_status TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO diagnostic_events (id, connection_status) VALUES (?, ?)",
            [
                (1, "healthy"),
                (6, "action_required"),
                (7, "action_required"),
                (8, "healthy"),
            ],
        )

    # Register ONLY the case-management blueprint; do not import repair_item.py.
    app = Flask(__name__)
    app.config.update(TESTING=True)
    app.register_blueprint(case_module.case_bp)
    with app.test_client() as test_client:
        yield test_client


def test_new_case_has_defaults(client):
    response = client.get("/api/incidents/INC-0006/case")
    assert response.status_code == 200
    data = response.get_json()
    assert data["incident_id"] == "INC-0006"
    assert data["severity"] == "P3"
    assert data["owner"] == "Unassigned"
    assert data["investigation_notes"] == ""
    assert data["updated_at"] is None


def test_save_and_read_back_case(client):
    payload = {
        "severity": "P2",
        "owner": "Support Engineering",
        "investigation_notes": "Reproduced login failure in Sandbox",
        "root_cause": "Sandbox authentication was reset",
        "resolution_summary": "Restored access with Link update mode",
    }
    saved = client.patch("/api/incidents/INC-0006/case", json=payload)
    assert saved.status_code == 200
    assert saved.get_json()["updated_at"] is not None

    fetched = client.get("/api/incidents/INC-0006/case")
    assert fetched.status_code == 200
    for field, value in payload.items():
        assert fetched.get_json()[field] == value


def test_case_survives_new_app_client(client):
    client.patch("/api/incidents/INC-0006/case", json={"owner": "Engineer A"})
    # A new Flask app/client simulates the persistence aspect of a server restart.
    restarted_app = Flask("finbridge_restarted_test")
    restarted_app.config.update(TESTING=True)
    restarted_app.register_blueprint(case_module.case_bp)
    with restarted_app.test_client() as restarted_client:
        fetched = restarted_client.get("/api/incidents/INC-0006/case")
    assert fetched.status_code == 200
    assert fetched.get_json()["owner"] == "Engineer A"


def test_partial_update_preserves_other_fields(client):
    client.patch(
        "/api/incidents/INC-0006/case",
        json={"severity": "P1", "root_cause": "Test reason"},
    )
    updated = client.patch(
        "/api/incidents/INC-0006/case", json={"owner": "Engineer B"}
    )
    assert updated.status_code == 200
    data = updated.get_json()
    assert data["severity"] == "P1"
    assert data["root_cause"] == "Test reason"
    assert data["owner"] == "Engineer B"


@pytest.mark.parametrize("value", ["P0", "P5", "Critical", ""])
def test_rejects_invalid_severity(client, value):
    response = client.patch(
        "/api/incidents/INC-0006/case", json={"severity": value}
    )
    assert response.status_code == 400


def test_rejects_non_string_owner(client):
    response = client.patch(
        "/api/incidents/INC-0006/case", json={"owner": 123}
    )
    assert response.status_code == 400


def test_rejects_oversized_notes(client):
    response = client.patch(
        "/api/incidents/INC-0006/case",
        json={"investigation_notes": "x" * 5001},
    )
    assert response.status_code == 400


def test_rejects_missing_case_fields(client):
    response = client.patch(
        "/api/incidents/INC-0006/case", json={"not_a_field": "value"}
    )
    assert response.status_code == 400


@pytest.mark.parametrize("incident_id", ["INC-0001", "INC-9999", "INVALID"])
def test_rejects_unknown_incident(client, incident_id):
    response = client.get(f"/api/incidents/{incident_id}/case")
    assert response.status_code == 404
