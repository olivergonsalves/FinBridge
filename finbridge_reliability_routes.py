"""Read-only Flask endpoints backed by FinBridge's tested incident state machine.

Register this Blueprint alongside your existing routes to compare results safely.
It never calls Plaid, writes to SQLite, or replaces the working /api/incidents API.
"""

import sqlite3
from contextlib import closing
from pathlib import Path

from flask import Blueprint, jsonify

from finbridge_reliability import build_incident_history

reliability_bp = Blueprint("finbridge_reliability", __name__)
DB_PATH = Path(__file__).resolve().parent / "finbridge.db"


def _incident_history():
    # SQLite URI mode=ro prevents accidental database creation or writes.
    database_uri = DB_PATH.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(database_uri, uri=True, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        events = [
            dict(row)
            for row in conn.execute("""
                SELECT id, checked_at, institution, connection_status,
                       error_type, error_code, request_id
                  FROM diagnostic_events
              ORDER BY id ASC
            """)
        ]
    return build_incident_history(events)


@reliability_bp.get("/api/reliability/incidents")
def reliability_incidents():
    incidents = _incident_history()
    summaries = [
        {key: value for key, value in incident.items() if key != "timeline"}
        for incident in incidents
    ]
    return jsonify({
        "total_incidents": len(summaries),
        "open_incidents": sum(incident["status"] == "open" for incident in summaries),
        "incidents": summaries,
    })


@reliability_bp.get("/api/reliability/incidents/<incident_id>")
def reliability_incident_detail(incident_id):
    for incident in _incident_history():
        if incident["incident_id"] == incident_id:
            return jsonify({
                "incident": {key: value for key, value in incident.items() if key != "timeline"},
                "timeline": incident["timeline"],
            })
    return jsonify({"error": "Incident not found"}), 404

