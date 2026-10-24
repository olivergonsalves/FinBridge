"""FinBridge incident case metadata: persisted separately from diagnostic events.

Add near the bottom of repair_item.py, BEFORE app.run():
    from finbridge_case_management import case_bp
    app.register_blueprint(case_bp)
"""

import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, jsonify, request

case_bp = Blueprint("finbridge_cases", __name__)
DB_PATH = Path(__file__).resolve().parent / "finbridge.db"
FIELDS = ("severity", "owner", "investigation_notes", "root_cause", "resolution_summary")
DEFAULTS = {
    "severity": "P3",
    "owner": "Unassigned",
    "investigation_notes": "",
    "root_cause": "",
    "resolution_summary": "",
}
MAX_LENGTHS = {
    "owner": 100,
    "investigation_notes": 5000,
    "root_cause": 2000,
    "resolution_summary": 2000,
}


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _init_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS incident_cases (
            incident_id TEXT PRIMARY KEY,
            severity TEXT NOT NULL DEFAULT 'P3',
            owner TEXT NOT NULL DEFAULT 'Unassigned',
            investigation_notes TEXT NOT NULL DEFAULT '',
            root_cause TEXT NOT NULL DEFAULT '',
            resolution_summary TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()


def _valid_incident(conn, incident_id):
    """In this single-Item prototype, incident ID is the first failure event ID."""
    match = re.fullmatch(r"INC-(\d+)", incident_id)
    if not match:
        return False
    row = conn.execute(
        "SELECT connection_status FROM diagnostic_events WHERE id = ?",
        (int(match.group(1)),),
    ).fetchone()
    return row is not None and row["connection_status"] in ("action_required", "error")


def _get_case(conn, incident_id):
    row = conn.execute(
        "SELECT * FROM incident_cases WHERE incident_id = ?", (incident_id,)
    ).fetchone()
    if row:
        return dict(row)
    return {"incident_id": incident_id, **DEFAULTS, "updated_at": None}


@case_bp.route("/api/incidents/<incident_id>/case", methods=["GET", "PATCH"])
def incident_case(incident_id):
    with closing(_connect()) as conn:
        _init_table(conn)
        if not _valid_incident(conn, incident_id):
            return jsonify({"error": "Unknown incident ID"}), 404

        current = _get_case(conn, incident_id)
        if request.method == "GET":
            return jsonify(current)

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not any(k in payload for k in FIELDS):
            return jsonify({"error": "Send a JSON object with case fields"}), 400

        updated = current.copy()
        for field in FIELDS:
            if field not in payload:
                continue
            value = payload[field]
            if not isinstance(value, str):
                return jsonify({"error": f"{field} must be a string"}), 400
            value = value.strip()
            if field == "severity":
                value = value.upper()
                if value not in ("P1", "P2", "P3", "P4"):
                    return jsonify({"error": "severity must be P1, P2, P3, or P4"}), 400
            elif len(value) > MAX_LENGTHS[field]:
                return jsonify({"error": f"{field} is too long"}), 400
            updated[field] = value

        updated["updated_at"] = datetime.now(timezone.utc).isoformat()
        conn.execute("""
            INSERT INTO incident_cases
                (incident_id, severity, owner, investigation_notes, root_cause,
                 resolution_summary, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(incident_id) DO UPDATE SET
                severity = excluded.severity,
                owner = excluded.owner,
                investigation_notes = excluded.investigation_notes,
                root_cause = excluded.root_cause,
                resolution_summary = excluded.resolution_summary,
                updated_at = excluded.updated_at
        """, (
            incident_id, updated["severity"], updated["owner"],
            updated["investigation_notes"], updated["root_cause"],
            updated["resolution_summary"], updated["updated_at"],
        ))
        conn.commit()
        return jsonify(updated)
