"""Milestone 9: tests for the REAL FinBridge /api/diagnostics/item Flask route.

Install as: ~/quickstart/tests/test_finbridge_diagnostic_endpoint.py
Run: python/.venv312/bin/python -m pytest -v tests/test_finbridge_diagnostic_endpoint.py

ISOLATION AND SAFETY:
- Copies repair_item.py into pytest's temporary directory before importing it.
  Its import-time initialize_database() therefore creates a TEMPORARY finbridge.db.
- Patches dotenv_values and getpass before import: no real .env or tokens are read.
- Blocks urllib.request.urlopen by default and supplies mocked Plaid replies.
- Redirects the case-management and reliability Blueprints to the SAME temporary DB.
- No production DB writes, Sandbox API requests, or Flask server start.

Assumes the repair_item.py structure shared in the FinBridge conversation,
including the diagnostic route, after_request logger, and registered Blueprints.
"""

import importlib.util
import io
import json
import shutil
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import finbridge_case_management as case_module
import finbridge_reliability_routes as reliability_module


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_APP = PROJECT_ROOT / "repair_item.py"
LOGIN_REQUIRED = {"error_type": "ITEM_ERROR", "error_code": "ITEM_LOGIN_REQUIRED"}
INSTITUTION = "First Platypus Bank"


def plaid_reply(*, issue=None, request_id="mock-req-1", institution=INSTITUTION):
    return {
        "item": {"institution_name": institution, "error": issue},
        "request_id": request_id,
    }


def fake_plaid(monkeypatch, reply):
    """Make urlopen return a local JSON buffer; never call the network."""
    def urlopen(request, *args, **kwargs):
        assert isinstance(request, urllib.request.Request)
        assert request.full_url == "https://sandbox.plaid.com/item/get"
        assert request.get_method() == "POST"
        assert kwargs.get("timeout") == 15
        assert json.loads(request.data)["access_token"] == "access-sandbox-test-only"
        return io.BytesIO(json.dumps(reply).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)


def fake_http_error(monkeypatch, *, code=400, body=None):
    """Return a Plaid-shaped urllib HTTPError with a JSON or malformed body."""
    if body is None:
        body = {
            "error_type": "INVALID_REQUEST",
            "error_code": "INVALID_ACCESS_TOKEN",
            "request_id": "mock-http-error",
        }
    raw = json.dumps(body).encode() if isinstance(body, dict) else body

    def urlopen(request, *args, **kwargs):
        raise urllib.error.HTTPError(
            request.full_url, code, "Mocked Plaid API error", {}, io.BytesIO(raw)
        )

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)


def db_events(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in conn.execute("SELECT * FROM diagnostic_events ORDER BY id")
        ]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Import an isolated copy of the actual app, with all side effects contained."""
    assert SOURCE_APP.is_file(), f"Expected application file: {SOURCE_APP}"
    isolated_dir = tmp_path / "isolated_app"
    isolated_dir.mkdir()
    copied_app = isolated_dir / "repair_item.py"
    shutil.copy2(SOURCE_APP, copied_app)
    db_path = isolated_dir / "finbridge.db"

    # Patch before import: repair_item.py reads credentials, prompts, and opens
    # SQLite during import. Never read the user's actual .env or access token.
    monkeypatch.setattr(
        "dotenv.dotenv_values",
        lambda *args, **kwargs: {
            "PLAID_CLIENT_ID": "test-client-only",
            "PLAID_SECRET": "test-secret-only",
        },
    )
    monkeypatch.setattr(
        "getpass.getpass", lambda *args, **kwargs: "access-sandbox-test-only"
    )

    def network_blocked(*args, **kwargs):
        raise AssertionError("Unexpected network access: test must mock Plaid")

    monkeypatch.setattr(urllib.request, "urlopen", network_blocked)

    # The Blueprints live in the original project, but must also use the temp DB.
    monkeypatch.setattr(case_module, "DB_PATH", db_path)
    monkeypatch.setattr(reliability_module, "DB_PATH", db_path)

    module_name = f"finbridge_isolated_test_{tmp_path.name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(module_name, copied_app)
    assert spec is not None and spec.loader is not None
    app_module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, app_module)
    spec.loader.exec_module(app_module)

    assert app_module.DB_PATH == db_path, "Database must be isolated"
    app_module.app.config["TESTING"] = True
    assert db_path.exists(), "Import-time database initialization missing"
    with app_module.app.test_client() as client:
        yield client, db_path, monkeypatch


def test_healthy_response_classified(env):
    client, db, mp = env
    fake_plaid(mp, plaid_reply())
    response = client.get("/api/diagnostics/item")
    body = response.get_json()
    assert response.status_code == 200
    assert body["connection_status"] == "healthy"
    assert body["error_code"] is None
    assert body["recommended_action"] == "No action required"
    assert body["institution"] == INSTITUTION
    assert body["request_id"] == "mock-req-1"
    assert body["last_checked"]


def test_healthy_response_logged_with_http_200(env):
    client, db, mp = env
    fake_plaid(mp, plaid_reply(request_id="healthy-log"))
    client.get("/api/diagnostics/item")
    events = db_events(db)
    assert len(events) == 1
    assert events[0]["connection_status"] == "healthy"
    assert events[0]["http_status"] == 200
    assert events[0]["request_id"] == "healthy-log"
    assert events[0]["institution"] == INSTITUTION
    assert events[0]["checked_at"]


def test_item_login_required_classified_and_logged(env):
    client, db, mp = env
    fake_plaid(mp, plaid_reply(issue=LOGIN_REQUIRED, request_id="login-fail"))
    response = client.get("/api/diagnostics/item")
    body = response.get_json()
    assert response.status_code == 200
    assert body["connection_status"] == "action_required"
    assert body["error_code"] == "ITEM_LOGIN_REQUIRED"
    assert "Reauthenticate" in body["recommended_action"]
    event = db_events(db)[0]
    assert event["error_type"] == "ITEM_ERROR"
    assert event["error_code"] == "ITEM_LOGIN_REQUIRED"
    assert event["http_status"] == 200


def test_other_item_error_classified_and_logged(env):
    client, db, mp = env
    issue = {"error_type": "ITEM_ERROR", "error_code": "ITEM_NOT_SUPPORTED"}
    fake_plaid(mp, plaid_reply(issue=issue))
    response = client.get("/api/diagnostics/item")
    assert response.status_code == 200
    assert response.get_json()["connection_status"] == "error"
    assert db_events(db)[0]["error_code"] == "ITEM_NOT_SUPPORTED"


def test_first_failure_opens_reliability_incident(env):
    client, db, mp = env
    fake_plaid(mp, plaid_reply(issue=LOGIN_REQUIRED))
    client.get("/api/diagnostics/item")
    result = client.get("/api/reliability/incidents")
    assert result.status_code == 200
    data = result.get_json()
    assert data["total_incidents"] == 1
    assert data["open_incidents"] == 1
    assert data["incidents"][0]["incident_id"] == "INC-0001"


def test_repeated_failures_remain_single_incident(env):
    client, db, mp = env
    fake_plaid(mp, plaid_reply(issue=LOGIN_REQUIRED))
    client.get("/api/diagnostics/item")
    client.get("/api/diagnostics/item")
    summary = client.get("/api/reliability/incidents").get_json()
    assert summary["total_incidents"] == 1
    assert summary["open_incidents"] == 1
    assert summary["incidents"][0]["failure_checks"] == 2
    assert len(db_events(db)) == 2


def test_healthy_recovery_resolves_incident(env):
    client, db, mp = env
    fake_plaid(mp, plaid_reply(issue=LOGIN_REQUIRED, request_id="failed-1"))
    client.get("/api/diagnostics/item")
    fake_plaid(mp, plaid_reply(issue=LOGIN_REQUIRED, request_id="failed-2"))
    client.get("/api/diagnostics/item")
    fake_plaid(mp, plaid_reply(request_id="recovered"))
    client.get("/api/diagnostics/item")
    response = client.get("/api/reliability/incidents/INC-0001")
    assert response.status_code == 200
    body = response.get_json()
    assert body["incident"]["status"] == "resolved"
    assert body["incident"]["resolution_event_id"] == 3
    assert body["incident"]["failure_checks"] == 2
    assert [e["id"] for e in body["timeline"]] == [1, 2, 3]
    assert [e["request_id"] for e in body["timeline"]] == [
        "failed-1", "failed-2", "recovered"
    ]


def test_initial_healthy_check_creates_no_incident(env):
    client, db, mp = env
    fake_plaid(mp, plaid_reply())
    client.get("/api/diagnostics/item")
    body = client.get("/api/reliability/incidents").get_json()
    assert body["total_incidents"] == 0
    assert body["incidents"] == []
    assert len(db_events(db)) == 1


def test_upstream_http_error_returns_502_and_is_logged(env):
    client, db, mp = env
    fake_http_error(mp, code=400)
    response = client.get("/api/diagnostics/item")
    assert response.status_code == 502
    body = response.get_json()
    assert body["connection_status"] == "api_request_failed"
    assert body["upstream_http_status"] == 400
    assert body["error_code"] == "INVALID_ACCESS_TOKEN"
    event = db_events(db)[0]
    assert event["connection_status"] == "api_request_failed"
    assert event["http_status"] == 502
    assert event["request_id"] == "mock-http-error"


def test_malformed_upstream_error_body_still_returns_502(env):
    client, db, mp = env
    fake_http_error(mp, code=503, body=b"not JSON")
    response = client.get("/api/diagnostics/item")
    assert response.status_code == 502
    assert response.get_json()["connection_status"] == "api_request_failed"
    assert response.get_json()["error_code"] is None
    assert db_events(db)[0]["http_status"] == 502


def test_network_error_returns_502_and_is_logged(env):
    client, db, mp = env
    def raise_network_error(*args, **kwargs):
        raise urllib.error.URLError("mock disconnected network")
    mp.setattr(urllib.request, "urlopen", raise_network_error)
    response = client.get("/api/diagnostics/item")
    assert response.status_code == 502
    assert response.get_json()["connection_status"] == "unavailable"
    assert db_events(db)[0]["connection_status"] == "unavailable"


def test_timeout_returns_502_and_is_logged(env):
    client, db, mp = env
    def raise_timeout(*args, **kwargs):
        raise TimeoutError("mock timeout")
    mp.setattr(urllib.request, "urlopen", raise_timeout)
    response = client.get("/api/diagnostics/item")
    assert response.status_code == 502
    assert response.get_json()["connection_status"] == "unavailable"
    assert db_events(db)[0]["http_status"] == 502


def test_case_management_reads_and_saves_real_diagnostic_incident(env):
    client, db, mp = env
    fake_plaid(mp, plaid_reply(issue=LOGIN_REQUIRED))
    client.get("/api/diagnostics/item")
    response = client.patch("/api/incidents/INC-0001/case", json={
        "severity": "P2",
        "owner": "FinBridge Support",
        "investigation_notes": "Simulated login reset; manual reauthentication required",
        "resolution_summary": "Plaid Link update mode is recommended",
    })
    assert response.status_code == 200
    case = client.get("/api/incidents/INC-0001/case")
    assert case.status_code == 200
    assert case.get_json()["owner"] == "FinBridge Support"
    assert case.get_json()["severity"] == "P2"
    assert len(db_events(db)) == 1  # Reading/saving case does not record a new check.
