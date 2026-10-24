"""Isolated FinBridge diagnostics/incident state-machine tests.

Run from ~/quickstart:
    python/.venv312/bin/python -m pytest -v tests/test_finbridge_reliability.py

These tests do NOT import repair_item.py, contact Plaid, or touch finbridge.db.
They validate the new pure logic BEFORE integrating it with Flask.
"""

from unittest.mock import Mock

import pytest

from finbridge_reliability import build_incident_history, diagnose_item


def response(issue=None, request_id="mock-req-1"):
    return {"item": {"error": issue}, "request_id": request_id}


def event(id_, status, error_code=None):
    return {
        "id": id_,
        "checked_at": f"2026-10-23T06:{id_:02d}:00+00:00",
        "institution": "First Platypus Bank",
        "connection_status": status,
        "error_type": "ITEM_ERROR" if error_code else None,
        "error_code": error_code,
        "request_id": f"mock-request-{id_}",
    }


def login_error():
    return {"error_type": "ITEM_ERROR", "error_code": "ITEM_LOGIN_REQUIRED"}


def test_healthy_mocked_plaid_response():
    mocked_plaid = Mock(return_value=response(None, "request-healthy"))
    result = diagnose_item(mocked_plaid)
    mocked_plaid.assert_called_once_with()
    assert result["connection_status"] == "healthy"
    assert result["error_code"] is None
    assert result["request_id"] == "request-healthy"


def test_login_required_mocked_plaid_response():
    result = diagnose_item(Mock(return_value=response(login_error())))
    assert result["connection_status"] == "action_required"
    assert result["error_code"] == "ITEM_LOGIN_REQUIRED"
    assert "update mode" in result["recommended_action"]


def test_unexpected_error_requires_investigation():
    result = diagnose_item(Mock(return_value=response(
        {"error_type": "ITEM_ERROR", "error_code": "ITEM_NOT_SUPPORTED"}
    )))
    assert result["connection_status"] == "error"
    assert result["error_code"] == "ITEM_NOT_SUPPORTED"


@pytest.mark.parametrize("bad_response", [None, [], {}, {"item": {}}, {"item": {"error": "bad"}}])
def test_malformed_plaid_response_is_rejected(bad_response):
    with pytest.raises(ValueError):
        diagnose_item(Mock(return_value=bad_response))


def test_no_events_no_incidents():
    assert build_incident_history([]) == []


def test_healthy_checks_do_not_create_incidents():
    assert build_incident_history([event(1, "healthy"), event(2, "healthy")]) == []


def test_single_failure_creates_one_open_incident():
    incidents = build_incident_history([event(6, "action_required", "ITEM_LOGIN_REQUIRED")])
    assert len(incidents) == 1
    assert incidents[0]["incident_id"] == "INC-0006"
    assert incidents[0]["status"] == "open"
    assert incidents[0]["resolution_event_id"] is None
    assert incidents[0]["failure_checks"] == 1


def test_repeated_failures_are_grouped():
    incidents = build_incident_history([
        event(6, "action_required", "ITEM_LOGIN_REQUIRED"),
        event(7, "action_required", "ITEM_LOGIN_REQUIRED"),
    ])
    assert len(incidents) == 1
    assert incidents[0]["failure_checks"] == 2
    assert [x["id"] for x in incidents[0]["timeline"]] == [6, 7]


def test_recovery_resolves_incident_and_preserves_timeline():
    incidents = build_incident_history([
        event(5, "healthy"),
        event(6, "action_required", "ITEM_LOGIN_REQUIRED"),
        event(7, "action_required", "ITEM_LOGIN_REQUIRED"),
        event(8, "healthy"),
        event(9, "healthy"),
    ])
    assert len(incidents) == 1
    inc = incidents[0]
    assert inc["status"] == "resolved"
    assert inc["resolution_event_id"] == 8
    assert inc["failure_checks"] == 2
    assert [x["id"] for x in inc["timeline"]] == [6, 7, 8]


def test_separate_failures_after_recovery_create_new_incident():
    incidents = build_incident_history([
        event(6, "action_required", "ITEM_LOGIN_REQUIRED"),
        event(8, "healthy"),
        event(10, "action_required", "ITEM_LOGIN_REQUIRED"),
    ])
    assert [x["incident_id"] for x in incidents] == ["INC-0006", "INC-0010"]
    assert [x["status"] for x in incidents] == ["resolved", "open"]


def test_generic_error_creates_incident():
    inc = build_incident_history([event(3, "error", "ITEM_NOT_SUPPORTED")])[0]
    assert inc["status"] == "open"
    assert inc["error_code"] == "ITEM_NOT_SUPPORTED"


def test_out_of_order_events_are_sorted_by_id():
    incidents = build_incident_history([
        event(8, "healthy"),
        event(7, "action_required", "ITEM_LOGIN_REQUIRED"),
        event(6, "action_required", "ITEM_LOGIN_REQUIRED"),
    ])
    assert incidents[0]["incident_id"] == "INC-0006"
    assert [x["id"] for x in incidents[0]["timeline"]] == [6, 7, 8]


def test_request_ids_are_preserved_in_incident_timeline():
    inc = build_incident_history([
        event(6, "action_required", "ITEM_LOGIN_REQUIRED"),
        event(8, "healthy"),
    ])[0]
    assert [x["request_id"] for x in inc["timeline"]] == [
        "mock-request-6", "mock-request-8"
    ]


def test_end_to_end_simulated_failure_and_recovery():
    """Mock consecutive Plaid responses, then reconstruct the incident."""
    plaid_item_get = Mock(side_effect=[
        response(None, "mock-healthy-before"),
        response(login_error(), "mock-login-required-1"),
        response(login_error(), "mock-login-required-2"),
        response(None, "mock-recovered"),
    ])
    recorded = []
    for event_id in (5, 6, 7, 8):
        diagnostic = diagnose_item(plaid_item_get)
        recorded.append({
            **event(event_id, diagnostic["connection_status"]),
            **diagnostic,
        })

    incidents = build_incident_history(recorded)
    assert plaid_item_get.call_count == 4
    assert len(incidents) == 1
    incident = incidents[0]
    assert incident["incident_id"] == "INC-0006"
    assert incident["failure_checks"] == 2
    assert incident["status"] == "resolved"
    assert incident["resolution_event_id"] == 8
    assert [row["request_id"] for row in incident["timeline"]] == [
        "mock-login-required-1",
        "mock-login-required-2",
        "mock-recovered",
    ]
