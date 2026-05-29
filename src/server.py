"""ChurnSignal Flask API."""

from __future__ import annotations

import sys
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

_SRC = Path(__file__).resolve().parent
_ROOT = _SRC.parent
_UI = _ROOT / "ui"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from app import (
    DATA_SOURCE,
    MOCK_ANALYSIS,
    MOCK_COMBINED_DATA,
    SQL_CONTACTS,
    SQL_CROSS_JOIN,
    SQL_OPEN_CONVERSATIONS,
    SQL_STRIPE,
    analyze_with_groq,
    ask_question,
    compute_churn_summary,
    get_combined_data,
    run_coral_query,
)

app = Flask(__name__)
app.config["JSONIFY_PRETTYPRINT_REGULAR"] = False
CORS(app)


@app.get("/")
def dashboard():
    return send_from_directory(_UI, "index.html")


@app.get("/architecture")
@app.get("/architecture.html")
def architecture():
    return send_from_directory(_UI, "architecture.html")


@app.get("/ui/<path:filename>")
def ui_static(filename: str):
    """Serve dashboard assets (e.g. /ui/architecture.html)."""
    return send_from_directory(_UI, filename)


@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "sources": ["intercom", "stripe"]})


@app.get("/api/analysis")
def analysis():
    using_mock = False
    coral_error: str | None = None

    try:
        combined = get_combined_data()
    except Exception as exc:
        combined = MOCK_COMBINED_DATA
        using_mock = True
        coral_error = str(exc)

    try:
        result = analyze_with_groq(combined)
    except Exception as exc:
        result = MOCK_ANALYSIS
        using_mock = True
        if not coral_error:
            coral_error = str(exc)

    computed = compute_churn_summary(combined)
    if isinstance(result, dict):
        result = {**result, "churn_risk_summary": computed}

    return jsonify(
        {
            "data": combined,
            "analysis": result,
            "meta": {
                "record_count": len(combined),
                "using_mock": using_mock,
                "coral_error": coral_error,
                "data_source": DATA_SOURCE,
                "sql_cross_join": SQL_CROSS_JOIN.strip(),
                "sql_open_conversations": SQL_OPEN_CONVERSATIONS.strip(),
                "sql_contacts": SQL_CONTACTS.strip(),
                "sql_stripe": SQL_STRIPE.strip(),
            },
        }
    )


@app.get("/api/data")
def data_only():
    """Combined Coral rows without Groq (faster debug)."""
    try:
        return jsonify({"data": get_combined_data(), "using_mock": False})
    except Exception as exc:
        return jsonify(
            {"data": MOCK_COMBINED_DATA, "using_mock": True, "error": str(exc)}
        )


@app.post("/api/chat")
def chat():
    body = request.get_json(silent=True) or {}
    question = str(body.get("question") or "").strip()
    return jsonify(ask_question(question))


@app.get("/api/coral-health")
def coral_health():
    try:
        rows = run_coral_query(
            "SELECT COUNT(*) AS n FROM intercom.conversations"
        )
        return jsonify({"status": "ok", "intercom_conversations": rows[0].get("n")})
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 503


if __name__ == "__main__":
    # use_reloader=False avoids a second process that can miss shell env on Windows
    print("ChurnSignal running:")
    print("  Dashboard:    http://127.0.0.1:5000/")
    print("  Architecture: http://127.0.0.1:5000/architecture")
    print("  API health: http://127.0.0.1:5000/api/health")
    print("  API data:   http://127.0.0.1:5000/api/analysis")
    print("  API chat:   POST http://127.0.0.1:5000/api/chat")
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True,
        threaded=True,
        use_reloader=False,
    )
