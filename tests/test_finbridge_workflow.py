"""FinBridge workflow integration tests: mocked Plaid -> SQLite -> Flask APIs.

Place in ~/quickstart/tests/test_finbridge_workflow.py and run from ~/quickstart:
    python/.venv312/bin/python -m pytest -v tests/test_finbridge_workflow.py

Safety: Does not import repair_item.py; does not connect to Plaid; only touches
SQLite in pytest's tmp_path. Uses the SAME production reliability and case
blueprints registered in the running FinBridge app.

Limit: Does not exercise the LIVE /api/diagnostics/item handler or its
@after_request logging hook. Those require a separate isolated app-level test
once the handler's source and dependencies are reviewed.
"""

import sqlite3
from unittest.mock import Mock

import pytest
from flask import Flask

import finbridge_case_management as cases
import finbridge_reliability_routes as reliability_routes
from finbridge_reliability import diagnose_item


INSTITUTION = "First Platypus Bank"
LOGIN_REQUIRED = {
    "error_type": "ITEM_ERROR",
    "error_code": "ITEM_LOGIN_REQUIRED",
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Test the production Blueprints against one temporary database."""
    test_db = tmp_path / "isolated_workflow.db"
    # These two modules resolve DB_PATH at request time.
    monkeypatch.setattr(cases, "DB_PATH", test_db)
    monkeypatch.setattr(reliability_routes, "DB_PATH", test_db)

    with sqlite3.connect(test_db) as conn:
        conn.execute("""
            CREATE TABLE diagnostic_events (
                id INTEGER PRIMARY KEY,
                checked_at TEXT NOT NULL,
                institution TEXT NOT NULL,
                connection_status TEXT NOT NULL,
                error_type TEXT,
                error_code TEXT,
                request_id TEXT
            )
        """)

    app = Flask("finbridge_workflow_integration_test")
    app.config["TESTING"] = True
    app.register_blueprint(cases.case_bp)
    app.register_blueprint(reliability_routes.reliability_bp)

    with app.test_client() as client:
        yield client, test_db, app


def record_fake_check(db, event_id, *, issue=None, request_id=None):
    """Classify a mocked /item/get reply, then persist a realistic event row.

    This emulates the recording boundary rather than calling the live
    /api/diagnostics/item endpoint; no HTTP requests leave the test.
    """
    request_id = request_id or f"mock-req-{event_id}"
    fetch_item = Mock(return_value={
        "item": {"error": issue},
        "request_id": request_id,
    })
    diagnostic = diagnose_item(fetch_item)
    fetch_item.assert_called_once_with()
    with sqlite3.connect(db) as conn:
        conn.execute("""
            INSERT INTO diagnostic_events
                (id, checked_at, institution, connection_status,
                 error_type, error_code, request_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            event_id,
            f"2026-10-23T06:{event_id:02d}:00+00:00",
            INSTITUTION,
            diagnostic["connection_status"],
            diagnostic["error_type"],
            diagnostic["error_code"],
            diagnostic["request_id"],
        ))
    return diagnostic


def record_resolved_sequence(db):
    record_fake_check(db, 5, request_id="healthy-before")
    record_fake_check(db, 6, issue=LOGIN_REQUIRED, request_id="failed-1")
    record_fake_check(db, 7, issue=LOGIN_REQUIRED, request_id="failed-2")
    record_fake_check(db, 8, request_id="recovered")


def test_healthy_diagnostic_produces_no_incident(env):
    client, db, _ = env
    result = record_fake_check(db, 5)
    assert result["connection_status"] == "healthy"
    summary = client.get("/api/reliability/incidents")
    assert summary.status_code == 200
    assert summary.get_json()["total_incidents"] == 0
    assert summary.get_json()["incidents"] == []


def test_first_auth_failure_creates_one_open_incident(env):
    client, db, _ = env
    record_fake_check(db, 5)
    diagnostic = record_fake_check(db, 6, issue=LOGIN_REQUIRED)
    assert diagnostic["connection_status"] == "action_required"
    summary = client.get("/api/reliability/incidents").get_json()
    assert summary["total_incidents"] == 1
    assert summary["open_incidents"] == 1
    assert summary["incidents"][0]["incident_id"] == "INC-0006"
    assert summary["incidents"][0]["status"] == "open"


def test_repeated_failures_remain_one_incident(env):
    client, db, _ = env
    record_fake_check(db, 6, issue=LOGIN_REQUIRED)
    record_fake_check(db, 7, issue=LOGIN_REQUIRED)
    incident = client.get("/api/reliability/incidents/INC-0006").get_json()
    assert incident["incident"]["failure_checks"] == 2
    assert incident["incident"]["status"] == "open"
    assert [e["id"] for e in incident["timeline"]] == [6, 7]
    assert client.get("/api/reliability/incidents").get_json()["total_incidents"] == 1


def test_recovery_closes_incident_and_preserves_request_ids(env):
    client, db, _ = env
    record_resolved_sequence(db)
    result = client.get("/api/reliability/incidents/INC-0006")
    assert result.status_code == 200
    payload = result.get_json()
    assert payload["incident"]["status"] == "resolved"
    assert payload["incident"]["resolution_event_id"] == 8
    assert payload["incident"]["failure_checks"] == 2
    assert [e["id"] for e in payload["timeline"]] == [6, 7, 8]
    assert [e["request_id"] for e in payload["timeline"]] == [
        "failed-1", "failed-2", "recovered"
    ]
    assert client.get("/api/reliability/incidents").get_json()["open_incidents"] == 0


def test_case_can_be_saved_for_resolved_incident(env):
    client, db, _ = env
    record_resolved_sequence(db)
    fields = {
        "severity": "P3",
        "owner": "Support Engineering",
        "investigation_notes": "Sandbox ITEM_LOGIN_REQUIRED reproduced twice",
        "root_cause": "Simulated credential reset",
        "resolution_summary": "Reauthenticated via update mode",
    }
    response = client.patch("/api/incidents/INC-0006/case", json=fields)
    assert response.status_code == 200
    assert response.get_json()["updated_at"]
    loaded = client.get("/api/incidents/INC-0006/case")
    assert loaded.status_code == 200
    assert all(loaded.get_json()[key] == value for key, value in fields.items())


def test_case_data_survives_new_flask_client(env):
    client, db, app = env
    record_resolved_sequence(db)
    assert client.patch("/api/incidents/INC-0006/case", json={
        "owner": "FinBridge Support"
    }).status_code == 200
    with app.test_client() as another_client:
        response = another_client.get("/api/incidents/INC-0006/case")
    assert response.status_code == 200
    assert response.get_json()["owner"] == "FinBridge Support"


def test_partial_case_update_does_not_overwrite_notes(env):
    client, db, _ = env
    record_resolved_sequence(db)
    client.patch("/api/incidents/INC-0006/case", json={
        "owner": "Engineer A", "investigation_notes": "Evidence retained"
    })
    response = client.patch("/api/incidents/INC-0006/case", json={
        "severity": "P2"
    })
    assert response.status_code == 200
    assert response.get_json()["severity"] == "P2"
    assert response.get_json()["owner"] == "Engineer A"
    assert response.get_json()["investigation_notes"] == "Evidence retained"


def test_second_failure_after_recovery_is_separate_case(env):
    client, db, _ = env
    record_resolved_sequence(db)
    record_fake_check(db, 9, issue=LOGIN_REQUIRED)
    summaries = client.get("/api/reliability/incidents").get_json()
    assert summaries["total_incidents"] == 2
    assert summaries["open_incidents"] == 1
    assert [x["incident_id"] for x in summaries["incidents"]] == [
        "INC-0006", "INC-0009"
    ]
    assert client.get("/api/incidents/INC-0009/case").status_code == 200
    assert client.get("/api/incidents/INC-0006/case").status_code == 200


def test_unexpected_plaid_item_error_still_creates_incident(env):
    client, db, _ = env
    issue = {"error_type": "ITEM_ERROR", "error_code": "ITEM_NOT_SUPPORTED"}
    diagnostic = record_fake_check(db, 6, issue=issue)
    assert diagnostic["connection_status"] == "error"
    incident = client.get("/api/reliability/incidents/INC-0006").get_json()
    assert incident["incident"]["error_code"] == "ITEM_NOT_SUPPORTED"
    assert incident["incident"]["status"] == "open"


def test_malformed_plaid_reply_is_rejected_without_db_write(env):
    client, db, _ = env
    mocked_plaid = Mock(return_value={"item": {}})
    with pytest.raises(ValueError):
        diagnose_item(mocked_plaid)
    with sqlite3.connect(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM diagnostic_events").fetchone()[0]
    assert count == 0
    assert client.get("/api/reliability/incidents").get_json()["total_incidents"] == 0


def test_incident_reads_do_not_modify_diagnostics(env):
    client, db, _ = env
    record_resolved_sequence(db)
    with sqlite3.connect(db) as conn:
        before = conn.execute("SELECT COUNT(*) FROM diagnostic_events").fetchone()[0]
    client.get("/api/reliability/incidents")
    client.get("/api/reliability/incidents/INC-0006")
    with sqlite3.connect(db) as conn:
        after = conn.execute("SELECT COUNT(*) FROM diagnostic_events").fetchone()[0]
    assert before == after == 4
