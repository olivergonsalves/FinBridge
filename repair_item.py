import json
import getpass
import urllib.request
import urllib.error
import sqlite3
from pathlib import Path
from contextlib import closing
from flask import Flask, jsonify,render_template, render_template_string, request
from dotenv import dotenv_values
from datetime import datetime, timezone


config = dotenv_values(".env")
client_id = config.get("PLAID_CLIENT_ID")
secret = config.get("PLAID_SECRET")

if not client_id or not secret:
    raise SystemExit("Missing Sandbox credentials")

access_token = getpass.getpass(
    "Paste the affected Item's Sandbox access token: "
).strip()

if not access_token.startswith("access-sandbox-"):
    raise SystemExit("Expected a Sandbox access token")

app = Flask(__name__)

@app.get("/")
def home():
    return render_template_string("""
    <h2>Plaid Connection Repair Lab</h2>
    <p>Restore the existing Sandbox connection.</p>
    <button id="repair">Repair Connection</button>
    <pre id="status"></pre>

    <script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
    <script>
      document.getElementById("repair").onclick = async () => {
        const status = document.getElementById("status");
        status.textContent = "Preparing update mode...";

        try {
          const response = await fetch("/link-token", {
            method: "POST"
          });

          const data = await response.json();

          if (!response.ok) {
            status.textContent = JSON.stringify(data, null, 2);
            return;
          }

          const handler = Plaid.create({
            token: data.link_token,

            onSuccess: () => {
              status.textContent =
                "Update completed. Verify the Item in API Explorer.";
            },

            onExit: (error) => {
              status.textContent = error
                ? "Link error: " + error.error_code
                : "Link closed before completion.";
            }
          });

          handler.open();
        } catch (error) {
          status.textContent = "Error: " + error.message;
        }
      };
    </script>
    """)


@app.post("/link-token")
def create_update_token():
    payload = {
        "client_id": client_id,
        "secret": secret,
        "client_name": "Plaid Diagnostics Lab",
        "user": {
            "client_user_id": "local-diagnostics-user"
        },
        "country_codes": ["US"],
        "language": "en",
        "access_token": access_token
    }

    request = urllib.request.Request(
        "https://sandbox.plaid.com/link/token/create",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(request) as response:
            result = json.load(response)
            return jsonify({"link_token": result["link_token"]})
    except urllib.error.HTTPError as error:
        return jsonify(json.load(error)), error.code



@app.get("/api/diagnostics/item")
def diagnose_item():
    payload = {
        "client_id": client_id,
        "secret": secret,
        "access_token": access_token
    }

    request = urllib.request.Request(
        "https://sandbox.plaid.com/item/get",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(
            request, timeout=15
        ) as response:
            data = json.load(response)

        item = data.get("item", {})
        error = item.get("error") or {}
        error_code = error.get("error_code")

        if error_code == "ITEM_LOGIN_REQUIRED":
            connection_status = "action_required"
            recommended_action = (
                "Reauthenticate using Plaid Link update mode"
            )
        elif error_code:
            connection_status = "error"
            recommended_action = (
                "Investigate the error code and Plaid documentation"
            )
        else:
            connection_status = "healthy"
            recommended_action = "No action required"

        return jsonify({
            "institution": item.get("institution_name"),
            "connection_status": connection_status,
            "error_type": error.get("error_type"),
            "error_code": error_code,
            "recommended_action": recommended_action,
            "request_id": data.get("request_id"),
            "last_checked": datetime.now(
                timezone.utc
            ).isoformat()
        })

    except urllib.error.HTTPError as error:
        try:
            details = json.load(error)
        except (ValueError, OSError):
            details = {}

        return jsonify({
            "connection_status": "api_request_failed",
            "upstream_http_status": error.code,
            "error_type": details.get("error_type"),
            "error_code": details.get("error_code"),
            "request_id": details.get("request_id"),
            "recommended_action": (
                "Investigate the API error before retrying"
            )
        }), 502

    except (urllib.error.URLError, TimeoutError):
        return jsonify({
            "connection_status": "unavailable",
            "recommended_action": (
                "Check network connectivity and retry later"
            )
        }), 502



# FinBridge — Diagnostic Event Logging

DB_PATH = Path(__file__).resolve().parent / "finbridge.db"


def initialize_database():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS diagnostic_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                checked_at TEXT NOT NULL,
                institution TEXT,
                connection_status TEXT NOT NULL,
                error_type TEXT,
                error_code TEXT,
                request_id TEXT,
                http_status INTEGER
            )
        """)
        conn.commit()


initialize_database()


@app.after_request
def log_diagnostic_event(response):
    if request.path != "/api/diagnostics/item":
        return response

    data = response.get_json(silent=True)

    if not isinstance(data, dict):
        return response

    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            conn.execute("""
                INSERT INTO diagnostic_events (
                    checked_at,
                    institution,
                    connection_status,
                    error_type,
                    error_code,
                    request_id,
                    http_status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now(timezone.utc).isoformat(),
                data.get("institution"),
                data.get("connection_status", "unknown"),
                data.get("error_type"),
                data.get("error_code"),
                data.get("request_id"),
                response.status_code
            ))
            conn.commit()

    except sqlite3.Error:
        app.logger.exception("Failed to save diagnostic event")

    return response


@app.get("/api/diagnostics/history")
def diagnostic_history():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row

        rows = conn.execute("""
            SELECT *
            FROM diagnostic_events
            ORDER BY id DESC
            LIMIT 20
        """).fetchall()

        events = [dict(row) for row in rows]

    return jsonify({
        "total_returned": len(events),
        "events": events
    })







# FinBridge — Incident Lifecycle Detection

@app.get("/api/incidents")
def get_incidents():
    # Read events in chronological order
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT id, checked_at, institution,
                   connection_status, error_code, request_id
            FROM diagnostic_events
            ORDER BY id ASC
        """).fetchall()

    incidents = []
    active_incident = None

    for row in rows:
        event = dict(row)
        status = event["connection_status"]

        # An error means an incident should be open
        if status in ("action_required", "error"):

            if active_incident is None:
                active_incident = {
                    "incident_id": f"INC-{event['id']:04d}",
                    "institution": event["institution"],
                    "error_code": event["error_code"],
                    "status": "open",
                    "first_detected_at": event["checked_at"],
                    "last_detected_at": event["checked_at"],
                    "failure_checks": 1,
                    "first_request_id": event["request_id"],
                    "resolved_at": None,
                    "resolution_event_id": None
                }

                incidents.append(active_incident)

            else:
                # Same ongoing incident, another failed check
                active_incident["failure_checks"] += 1
                active_incident["last_detected_at"] = (
                    event["checked_at"]
                )

        # A healthy result closes an existing incident
        elif status == "healthy" and active_incident:
            active_incident["status"] = "resolved"
            active_incident["resolved_at"] = event["checked_at"]
            active_incident["resolution_event_id"] = event["id"]
            active_incident = None

    return jsonify({
        "total_incidents": len(incidents),
        "open_incidents": sum(
            1 for i in incidents if i["status"] == "open"
        ),
        "incidents": list(reversed(incidents))
    })


@app.get("/api/incidents/<incident_id>")
def incident_details(incident_id):

    # Reuse our existing incident detection logic
    summary = get_incidents().get_json()

    incident = next(
        (
            item for item in summary["incidents"]
            if item["incident_id"] == incident_id
        ),
        None
    )

    if incident is None:
        return jsonify({
            "error": "Incident not found"
        }), 404

    # Incident ID contains the first failure event ID
    first_event_id = int(incident_id.split("-")[1])
    resolution_event_id = incident["resolution_event_id"]

    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row

        if resolution_event_id is not None:
            rows = conn.execute("""
                SELECT *
                FROM diagnostic_events
                WHERE id >= ? AND id <= ?
                ORDER BY id ASC
            """, (
                first_event_id,
                resolution_event_id
            )).fetchall()

        else:
            rows = conn.execute("""
                SELECT *
                FROM diagnostic_events
                WHERE id >= ?
                ORDER BY id ASC
            """, (first_event_id,)).fetchall()

    return jsonify({
        "incident": incident,
        "timeline": [dict(row) for row in rows]
    })


@app.get("/console")
def support_console():
    return render_template("support_console.html")

from finbridge_case_management import case_bp
app.register_blueprint(case_bp)

from finbridge_reliability_routes import reliability_bp
app.register_blueprint(reliability_bp)

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8001, debug=False)