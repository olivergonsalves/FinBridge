"""Pure, testable FinBridge diagnostic and incident logic.

This module intentionally makes no network requests and opens no databases.
The current Flask endpoints are unchanged until you explicitly wire this in.
The incident grouping assumes ONE Plaid Item/connection, matching the
current FinBridge prototype; a multi-Item version must group by item_id.
"""


def diagnose_item(fetch_item):
    """Obtain one mocked (or injected) /item/get-style response and classify it.

    The caller supplies a zero-argument callable. In unit tests this is a Mock,
    so nothing contacts Plaid. Do NOT pass or log access tokens here.
    """
    response = fetch_item()
    if not isinstance(response, dict):
        raise ValueError("Item response must be a JSON object")

    item = response.get("item")
    if not isinstance(item, dict) or "error" not in item:
        raise ValueError("Expected an item object with an error field")

    issue = item["error"]
    if issue is None:
        return {
            "connection_status": "healthy",
            "error_type": None,
            "error_code": None,
            "request_id": response.get("request_id"),
            "recommended_action": "No action required.",
        }

    if not isinstance(issue, dict):
        raise ValueError("item.error must be null or an object")

    error_code = issue.get("error_code")
    error_type = issue.get("error_type")
    needs_reauth = error_code == "ITEM_LOGIN_REQUIRED"

    return {
        "connection_status": "action_required" if needs_reauth else "error",
        "error_type": error_type,
        "error_code": error_code,
        "request_id": response.get("request_id"),
        "recommended_action": (
            "Reauthenticate this Item using Plaid Link update mode."
            if needs_reauth else "Investigate the Plaid error and request ID."
        ),
    }


def build_incident_history(events):
    """Reconstruct incidents from recorded diagnostic events for ONE connection.

    Args:
        events: iterable of dicts with id, checked_at, connection_status,
                institution, error_code, error_type, request_id.

    Returns:
        List of incidents in first-detected order. Each has a ``timeline``
        containing its failed checks and (when present) its recovery check.
        Healthy checks before an incident or after recovery are not incidents.
    """
    ordered = sorted(events, key=lambda e: int(e["id"]))
    incidents = []
    active = None

    for event in ordered:
        status = event.get("connection_status")
        if status in ("action_required", "error"):
            if active is None:
                active = {
                    "incident_id": f"INC-{int(event['id']):04d}",
                    "institution": event.get("institution"),
                    "error_code": event.get("error_code"),
                    "first_detected_at": event.get("checked_at"),
                    "last_detected_at": event.get("checked_at"),
                    "failure_checks": 0,
                    "status": "open",
                    "resolution_event_id": None,
                    "resolved_at": None,
                    "timeline": [],
                }
                incidents.append(active)

            active["failure_checks"] += 1
            active["last_detected_at"] = event.get("checked_at")
            active["timeline"].append(dict(event))

        elif status == "healthy" and active is not None:
            active["status"] = "resolved"
            active["resolution_event_id"] = int(event["id"])
            active["resolved_at"] = event.get("checked_at")
            active["timeline"].append(dict(event))
            active = None

    return incidents
