"""ChurnSignal — Coral SQL queries and Groq analysis."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CORAL_EXE = PROJECT_ROOT / "coral.exe"

GROQ_MODEL_DEFAULT = "llama-3.3-70b-versatile"
GROQ_MODEL_FALLBACK = "llama-3.1-8b-instant"
GROQ_MAX_TOKENS = 2000
ANALYSIS_SYSTEM_PROMPT = (
    "You are a Customer Success analyst specializing in churn prevention."
)

ALLOWED_CATEGORIES = (
    "Performance & Reliability",
    "Onboarding & Support",
    "Pricing & Billing",
    "Product Features",
    "Security & Compliance",
    "Integration Issues",
)

ANALYSIS_JSON_SCHEMA = """
{
  "top_themes": [
    {"theme": "string", "count": 0, "severity": "critical|high|medium",
     "description": "string", "example": "string"}
  ],
  "churn_risk_summary": {
    "total_open_tickets": 0,
    "high_risk_count": 0,
    "canceled_subscriptions": 0,
    "past_due_subscriptions": 0
  },
  "triage_queue": [
    {"customer": "string", "email": "string", "priority": "P1|P2|P3",
     "issue_summary": "string", "recommended_action": "string"}
  ],
  "categories": [
    {"name": "string", "count": 0}
  ],
  "executive_summary": "2-3 sentences for leadership"
}
""".strip()


def _load_local_env() -> None:
    """Load secrets: shell env (highest) > .env > local.settings.json Values."""
    try:
        from dotenv import load_dotenv

        load_dotenv(PROJECT_ROOT / ".env", override=False)
    except ImportError:
        env_file = PROJECT_ROOT / ".env"
        if env_file.is_file():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    os.environ.setdefault(key, value)

    settings_file = PROJECT_ROOT / "local.settings.json"
    if not settings_file.is_file():
        return

    try:
        data = json.loads(settings_file.read_text(encoding="utf-8"))
        for key, value in data.get("Values", {}).items():
            os.environ.setdefault(key, str(value))
    except json.JSONDecodeError:
        pass


_load_local_env()

DATA_SOURCE = os.environ.get("DATA_SOURCE", "local")
DATA_DIR = PROJECT_ROOT / "data"

SQL_OPEN_CONVERSATIONS = """
SELECT
    id,
    state,
    created_at,
    source_author_email,
    source_author_name,
    source_subject,
    priority,
    title
FROM intercom.conversations
WHERE state IN ('open', 'snoozed')
ORDER BY created_at DESC
LIMIT 50
"""

SQL_CONTACTS = """
SELECT id, email, name, role, last_replied_at, updated_at
FROM intercom.contacts
ORDER BY updated_at DESC
LIMIT 50
"""

SQL_STRIPE = """
SELECT
    c.id AS customer_id,
    c.name AS customer_name,
    c.email,
    c.delinquent,
    s.id AS subscription_id,
    s.status AS subscription_status
FROM stripe.customers c
LEFT JOIN stripe.subscriptions s ON s.customer = c.id
ORDER BY c.email
LIMIT 100
"""

SQL_CROSS_JOIN = """
SELECT
    conv.id AS conversation_id,
    conv.state AS ticket_state,
    conv.created_at AS opened_at,
    ic.email AS contact_email,
    conv.source_author_name AS contact_name,
    conv.source_subject,
    conv.priority,
    ic.name AS intercom_contact_name,
    sc.name AS customer_name,
    sc.email AS stripe_email,
    sub.status AS subscription_status,
    sub.id AS subscription_id
FROM intercom.conversations conv
LEFT JOIN intercom.contacts ic ON ic.id = conv.source_author_id
LEFT JOIN stripe.customers sc ON sc.email = ic.email
LEFT JOIN stripe.subscriptions sub ON sub.customer = sc.id
WHERE conv.state IN ('open', 'snoozed')
ORDER BY conv.created_at DESC
LIMIT 50
"""


def _coral_executable() -> Path:
    """Resolve coral.exe path (subprocess per call — thread-safe)."""
    override = os.environ.get("CORAL_PATH", "").strip()
    if override:
        path = Path(override)
        if not path.is_file():
            raise FileNotFoundError(f"CORAL_PATH not found: {path}")
        return path
    if not CORAL_EXE.is_file():
        raise FileNotFoundError(f"coral.exe not found at {CORAL_EXE}")
    return CORAL_EXE


def run_coral_query(sql: str) -> list[dict[str, Any]]:
    """Run coral.exe with JSON output and return rows as dicts (fresh subprocess each call)."""
    coral_path = _coral_executable()

    proc = subprocess.run(
        [str(coral_path), "sql", sql.strip(), "--format", "json"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "Coral query failed").strip()
        raise RuntimeError(err)

    raw = proc.stdout.strip()
    if not raw:
        return []
    data = json.loads(raw)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    return []


def get_intercom_conversations() -> list[dict[str, Any]]:
    return run_coral_query(SQL_OPEN_CONVERSATIONS)


def get_stripe_data() -> list[dict[str, Any]]:
    return run_coral_query(SQL_STRIPE)


def _load_json_data(filename: str) -> list[dict[str, Any]]:
    path = DATA_DIR / filename
    if not path.is_file():
        raise FileNotFoundError(f"Local data file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    return []


def _get_data_from_local() -> list[dict[str, Any]]:
    """Load Intercom + Stripe JSON fixtures and JOIN in Python."""
    conversations = _load_json_data("intercom_conversations.json")
    contacts = _load_json_data("intercom_contacts.json")
    stripe_customers = _load_json_data("stripe_customers.json")
    subscriptions = _load_json_data("stripe_subscriptions.json")

    contacts_by_id = {c["id"]: c for c in contacts if c.get("id")}
    customers_by_email = {
        c["email"]: c for c in stripe_customers if c.get("email")
    }
    subs_by_customer = {
        s["customer"]: s for s in subscriptions if s.get("customer")
    }

    merged: list[dict[str, Any]] = []
    for conv in conversations:
        if conv.get("state") not in ("open", "snoozed"):
            continue

        contact = contacts_by_id.get(conv.get("source_author_id"), {})
        email = contact.get("email") or conv.get("source_author_email")
        customer = customers_by_email.get(email, {}) if email else {}
        sub = subs_by_customer.get(customer.get("id"), {}) if customer else {}

        merged.append(
            {
                "conversation_id": conv.get("id"),
                "ticket_state": conv.get("state"),
                "opened_at": conv.get("created_at"),
                "contact_email": email,
                "contact_name": conv.get("source_author_name") or contact.get("name"),
                "source_subject": conv.get("source_subject"),
                "priority": conv.get("priority"),
                "intercom_contact_name": contact.get("name"),
                "customer_name": customer.get("name"),
                "stripe_email": customer.get("email"),
                "subscription_status": sub.get("status"),
                "subscription_id": sub.get("id"),
                "merge_source": "local",
            }
        )

    return merged


def _get_data_from_coral() -> list[dict[str, Any]]:
    """Cross-source Coral JOIN; fall back to Python merge on error or empty result."""
    try:
        rows = run_coral_query(SQL_CROSS_JOIN)
        if rows:
            return rows
    except RuntimeError:
        pass

    return _merge_in_python_coral()


def _merge_in_python_coral() -> list[dict[str, Any]]:
    """Fetch Intercom + Stripe via Coral separately and merge on email."""
    conversations = get_intercom_conversations()
    contacts = run_coral_query(SQL_CONTACTS)
    stripe_rows = get_stripe_data()

    stripe_by_email: dict[str, dict[str, Any]] = {}
    for row in stripe_rows:
        email = (row.get("email") or "").strip().lower()
        if email and email not in stripe_by_email:
            stripe_by_email[email] = row

    contact_by_email: dict[str, dict[str, Any]] = {}
    for row in contacts:
        email = (row.get("email") or "").strip().lower()
        if email:
            contact_by_email[email] = row

    merged: list[dict[str, Any]] = []
    emails_in_rows: set[str] = set()

    for conv in conversations:
        email = (conv.get("source_author_email") or "").strip()
        email_key = email.lower()
        contact = contact_by_email.get(email_key, {})
        stripe = stripe_by_email.get(email_key, {})

        if email_key:
            emails_in_rows.add(email_key)

        merged.append(
            {
                "conversation_id": conv.get("id"),
                "ticket_state": conv.get("state"),
                "opened_at": conv.get("created_at"),
                "contact_email": email or None,
                "contact_name": conv.get("source_author_name")
                or contact.get("name"),
                "source_subject": conv.get("source_subject"),
                "priority": conv.get("priority"),
                "customer_name": stripe.get("customer_name"),
                "subscription_status": stripe.get("subscription_status"),
                "subscription_id": stripe.get("subscription_id"),
                "merge_source": "coral_conversation",
            }
        )

    for email_key, contact in contact_by_email.items():
        if email_key in emails_in_rows:
            continue
        stripe = stripe_by_email.get(email_key, {})
        merged.append(
            {
                "conversation_id": None,
                "ticket_state": None,
                "opened_at": contact.get("last_replied_at"),
                "contact_email": contact.get("email"),
                "contact_name": contact.get("name"),
                "source_subject": None,
                "priority": None,
                "customer_name": stripe.get("customer_name"),
                "subscription_status": stripe.get("subscription_status"),
                "subscription_id": stripe.get("subscription_id"),
                "merge_source": "coral_contact",
            }
        )

    for email_key, stripe in stripe_by_email.items():
        if email_key in emails_in_rows or any(
            (r.get("contact_email") or "").strip().lower() == email_key for r in merged
        ):
            continue
        merged.append(
            {
                "conversation_id": None,
                "ticket_state": None,
                "opened_at": None,
                "contact_email": stripe.get("email"),
                "contact_name": stripe.get("customer_name"),
                "customer_name": stripe.get("customer_name"),
                "subscription_status": stripe.get("subscription_status"),
                "subscription_id": stripe.get("subscription_id"),
                "merge_source": "coral_stripe",
            }
        )

    return merged


def get_combined_data() -> list[dict[str, Any]]:
    if DATA_SOURCE == "live":
        return _get_data_from_coral()
    return _get_data_from_local()


def _strip_json_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_json_object(text: str) -> str:
    """Pull the outermost JSON object from model output."""
    text = _strip_json_fences(text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text


def _validate_analysis(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    required = (
        "top_themes",
        "churn_risk_summary",
        "triage_queue",
        "categories",
        "executive_summary",
    )
    if not all(key in result for key in required):
        return False
    summary = result["churn_risk_summary"]
    if not isinstance(summary, dict):
        return False
    for key in (
        "total_open_tickets",
        "high_risk_count",
        "canceled_subscriptions",
        "past_due_subscriptions",
    ):
        if key not in summary:
            return False
    if not isinstance(result["top_themes"], list):
        return False
    if not isinstance(result["triage_queue"], list):
        return False
    if not isinstance(result["categories"], list):
        return False
    if not isinstance(result["executive_summary"], str):
        return False
    return True


def _normalize_analysis(result: dict[str, Any]) -> dict[str, Any]:
    """Coerce Groq output into a consistent shape for the API/UI."""
    summary = result.get("churn_risk_summary") or {}
    normalized: dict[str, Any] = {
        "top_themes": [],
        "churn_risk_summary": {
            "total_open_tickets": int(summary.get("total_open_tickets") or 0),
            "high_risk_count": int(summary.get("high_risk_count") or 0),
            "canceled_subscriptions": int(summary.get("canceled_subscriptions") or 0),
            "past_due_subscriptions": int(summary.get("past_due_subscriptions") or 0),
        },
        "triage_queue": [],
        "categories": [],
        "executive_summary": str(result.get("executive_summary") or "").strip(),
    }

    for theme in result.get("top_themes") or []:
        if not isinstance(theme, dict):
            continue
        severity = str(theme.get("severity") or "medium").lower()
        if severity not in ("critical", "high", "medium"):
            severity = "medium"
        normalized["top_themes"].append(
            {
                "theme": str(theme.get("theme") or "Unknown"),
                "count": int(theme.get("count") or 0),
                "severity": severity,
                "description": str(theme.get("description") or ""),
                "example": str(theme.get("example") or ""),
            }
        )

    for item in result.get("triage_queue") or []:
        if not isinstance(item, dict):
            continue
        priority = str(item.get("priority") or "P3").upper()
        if priority not in ("P1", "P2", "P3"):
            priority = "P3"
        normalized["triage_queue"].append(
            {
                "customer": str(item.get("customer") or "Unknown"),
                "email": str(item.get("email") or ""),
                "priority": priority,
                "issue_summary": str(item.get("issue_summary") or ""),
                "recommended_action": str(item.get("recommended_action") or ""),
            }
        )

    for cat in result.get("categories") or []:
        if not isinstance(cat, dict):
            continue
        name = str(cat.get("name") or "")
        if name in ALLOWED_CATEGORIES or name:
            normalized["categories"].append(
                {"name": name or "Other", "count": int(cat.get("count") or 0)}
            )

    return normalized


def compute_churn_summary(data: list[dict[str, Any]]) -> dict[str, int]:
    """Deterministic metrics from combined rows (used for dashboard cards)."""
    open_tickets = sum(1 for r in data if r.get("ticket_state") in ("open", "snoozed"))
    canceled = sum(1 for r in data if r.get("subscription_status") == "canceled")
    past_due = sum(1 for r in data if r.get("subscription_status") == "past_due")
    high_risk = sum(
        1
        for r in data
        if r.get("ticket_state") in ("open", "snoozed")
        and r.get("subscription_status") in ("canceled", "past_due", "unpaid")
    )
    return {
        "total_open_tickets": open_tickets,
        "high_risk_count": high_risk,
        "canceled_subscriptions": canceled,
        "past_due_subscriptions": past_due,
    }


def _analysis_fallback(data: list[dict[str, Any]], reason: str = "") -> dict[str, Any]:
    summary = compute_churn_summary(data)
    open_tickets = summary["total_open_tickets"]
    canceled = summary["canceled_subscriptions"]
    past_due = summary["past_due_subscriptions"]
    high_risk = summary["high_risk_count"]

    triage: list[dict[str, str]] = []
    for row in data[:10]:
        status = (row.get("subscription_status") or "").lower()
        has_ticket = row.get("ticket_state") in ("open", "snoozed") or row.get(
            "conversation_id"
        )
        if has_ticket and status in ("canceled", "past_due", "unpaid"):
            priority = "P1"
        elif has_ticket and status == "active":
            priority = "P2"
        else:
            priority = "P3"

        triage.append(
            {
                "customer": row.get("contact_name")
                or row.get("customer_name")
                or "Unknown",
                "email": row.get("contact_email") or row.get("stripe_email") or "",
                "priority": priority,
                "issue_summary": row.get("source_subject")
                or f"Open ticket ({row.get('ticket_state') or 'contact'})",
                "recommended_action": "Review conversation and subscription status",
            }
        )

    summary_note = f" ({reason})" if reason else ""
    return {
        "top_themes": [
            {
                "theme": "Open support volume",
                "count": open_tickets,
                "severity": "medium",
                "description": "Active Intercom conversations require triage.",
                "example": "Multiple open tickets without linked Stripe billing data.",
            }
        ],
        "churn_risk_summary": summary,
        "triage_queue": triage,
        "categories": [
            {"name": "Onboarding & Support", "count": open_tickets},
            {"name": "Pricing & Billing", "count": canceled + past_due},
        ],
        "executive_summary": (
            f"ChurnSignal analyzed {len(data)} combined Intercom and Stripe records{summary_note}. "
            f"{open_tickets} open tickets; {canceled} canceled and {past_due} past-due subscriptions."
        ),
    }


def _build_analysis_user_prompt(data: list[dict[str, Any]]) -> str:
    payload = json.dumps(data[:80], default=str)
    categories = ", ".join(ALLOWED_CATEGORIES)
    return f"""Analyze this customer success dataset (Intercom tickets + Stripe subscriptions).

Return ONLY valid JSON matching this schema (no markdown, no extra text):

{ANALYSIS_JSON_SCHEMA}

Priority rules:
- P1 = canceled or past_due subscription with an open ticket
- P2 = open ticket with active subscription
- P3 = general inquiry or no subscription risk

Use categories from: {categories}.

Data:
{payload}
"""


def _groq_models_to_try() -> list[str]:
    primary = os.environ.get("GROQ_MODEL", GROQ_MODEL_DEFAULT)
    models = [primary]
    if primary != GROQ_MODEL_FALLBACK:
        models.append(GROQ_MODEL_FALLBACK)
    return models


def analyze_with_groq(data: list[dict[str, Any]]) -> dict[str, Any]:
    """Send combined records to Groq; return structured churn analysis JSON."""
    _load_local_env()
    if not os.environ.get("GROQ_API_KEY"):
        return _analysis_fallback(data, reason="GROQ_API_KEY not set")

    try:
        from groq import Groq
    except ImportError:
        return _analysis_fallback(data, reason="groq package not installed")

    user_prompt = _build_analysis_user_prompt(data)
    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    last_error = ""

    for model in _groq_models_to_try():
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=GROQ_MAX_TOKENS,
            )
            result_text = resp.choices[0].message.content or ""
            parsed = json.loads(_extract_json_object(result_text))
            if _validate_analysis(parsed):
                return _normalize_analysis(parsed)
            last_error = f"invalid JSON schema from {model}"
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError):
            last_error = f"parse error from {model}"
        except Exception as exc:
            err_name = type(exc).__name__
            last_error = f"{err_name} ({model})"
            if "RateLimit" in err_name or "429" in str(exc):
                continue

    return _analysis_fallback(
        data, reason=last_error or "Groq parse or API error"
    )


CHAT_SQL_SYSTEM_PROMPT = """You are a SQL expert for a customer churn tool.
Available tables via Coral SQL:
- intercom.conversations (id, state, title, source_subject, source_author_id,
  source_author_email, priority, created_at)
- intercom.contacts (id, email, name, created_at, location_country)
- intercom.companies (id, name, plan, monthly_spend)
- stripe.customers (id, name, email, delinquent)
- stripe.subscriptions (id, customer, status, amount, plan_nickname,
  cancel_at_period_end, canceled_at)
- stripe.charges (id, customer, amount, status, failure_code)

Return ONLY a valid SQL SELECT query, no explanation, no markdown fences.
Always LIMIT 20. Use LEFT JOINs.
Join intercom.contacts on ic.id = conv.source_author_id
Join stripe via email: ic.email = sc.email"""

CHAT_ANSWER_SYSTEM_PROMPT = (
    "You are a customer success analyst. Answer concisely in 2-4 sentences."
)

_CORAL_TO_SQLITE_TABLES = {
    "intercom.conversations": "intercom_conversations",
    "intercom.contacts": "intercom_contacts",
    "intercom.companies": "intercom_companies",
    "stripe.customers": "stripe_customers",
    "stripe.subscriptions": "stripe_subscriptions",
    "stripe.charges": "stripe_charges",
}

_LOCAL_TABLE_FILES = {
    "intercom_conversations": "intercom_conversations.json",
    "intercom_contacts": "intercom_contacts.json",
    "intercom_companies": "intercom_companies.json",
    "stripe_customers": "stripe_customers.json",
    "stripe_subscriptions": "stripe_subscriptions.json",
    "stripe_charges": "stripe_charges.json",
}


def _groq_text_completion(system: str, user: str, max_tokens: int = 800) -> str:
    """Call Groq with model fallback; return assistant text."""
    _load_local_env()
    if not os.environ.get("GROQ_API_KEY"):
        raise RuntimeError("GROQ_API_KEY not set")

    from groq import Groq

    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    last_error = ""
    for model in _groq_models_to_try():
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=max_tokens,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as exc:
            last_error = str(exc)
            if "RateLimit" in type(exc).__name__ or "429" in str(exc):
                continue
    raise RuntimeError(last_error or "Groq request failed")


def _extract_sql_query(text: str) -> str:
    """Pull a SELECT statement from model output."""
    text = _strip_json_fences(text).strip()
    if text.lower().startswith("select"):
        sql = text
    else:
        match = re.search(r"(SELECT\b.+)", text, re.IGNORECASE | re.DOTALL)
        if not match:
            raise ValueError("No SELECT query found in model response")
        sql = match.group(1)
    sql = sql.strip().rstrip(";")
    upper = sql.upper()
    if not upper.startswith("SELECT"):
        raise ValueError("Only SELECT queries are allowed")
    for forbidden in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "ATTACH"):
        if re.search(rf"\b{forbidden}\b", upper):
            raise ValueError(f"Forbidden SQL keyword: {forbidden}")
    if ";" in sql:
        raise ValueError("Multiple SQL statements are not allowed")
    if not re.search(r"\bLIMIT\b", upper):
        sql = f"{sql} LIMIT 20"
    return sql


def _normalize_sql_for_local(sql: str) -> str:
    normalized = sql
    for coral_name, sqlite_name in _CORAL_TO_SQLITE_TABLES.items():
        normalized = re.sub(
            rf"\b{re.escape(coral_name)}\b",
            sqlite_name,
            normalized,
            flags=re.IGNORECASE,
        )
    return normalized


def _sqlite_load_table(conn: sqlite3.Connection, table: str, filename: str) -> None:
    rows = _load_json_data(filename)
    conn.execute(f"DROP TABLE IF EXISTS {table}")
    if not rows:
        conn.execute(f"CREATE TABLE {table} (id TEXT)")
        return
    columns = list(rows[0].keys())
    col_defs = ", ".join(f'"{c}"' for c in columns)
    conn.execute(f"CREATE TABLE {table} ({col_defs})")
    placeholders = ", ".join("?" for _ in columns)
    for row in rows:
        values = []
        for c in columns:
            val = row.get(c)
            if isinstance(val, (dict, list)):
                val = json.dumps(val)
            values.append(val)
        conn.execute(
            f'INSERT INTO {table} ({col_defs}) VALUES ({placeholders})',
            values,
        )


def run_local_query(sql: str) -> list[dict[str, Any]]:
    """Execute SQL on a fresh in-memory DB (thread-safe for Flask workers)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        for table, filename in _LOCAL_TABLE_FILES.items():
            _sqlite_load_table(conn, table, filename)
        local_sql = _normalize_sql_for_local(sql)
        result = conn.execute(local_sql)
        if result.description is None:
            return []
        columns = [col[0] for col in result.description]
        return [dict(zip(columns, row)) for row in result.fetchall()]
    except sqlite3.Error as exc:
        raise RuntimeError(str(exc)) from exc
    finally:
        conn.close()


def _generate_chat_sql(question: str) -> str:
    raw = _groq_text_completion(
        CHAT_SQL_SYSTEM_PROMPT,
        question,
        max_tokens=600,
    )
    return _extract_sql_query(raw)


def _summarize_chat_answer(question: str, rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No matching records were found for that question."
    payload = json.dumps(rows[:10], default=str)
    return _groq_text_completion(
        CHAT_ANSWER_SYSTEM_PROMPT,
        f"Question: {question}\nData: {payload}",
        max_tokens=400,
    )


def ask_question(question: str) -> dict[str, Any]:
    """Natural-language question → SQL → data → Groq summary."""
    question = (question or "").strip()
    if not question:
        return {
            "answer": "Please enter a question.",
            "sql": "",
            "rows": [],
            "row_count": 0,
        }

    _load_local_env()
    if not os.environ.get("GROQ_API_KEY"):
        return {
            "answer": "GROQ_API_KEY is not configured. Add it to your .env file.",
            "sql": "",
            "rows": [],
            "row_count": 0,
        }

    sql = ""
    try:
        sql = _generate_chat_sql(question)
    except Exception as exc:
        return {
            "answer": f"I could not generate a SQL query for that question. ({exc})",
            "sql": "",
            "rows": [],
            "row_count": 0,
        }

    try:
        if DATA_SOURCE == "live":
            rows = run_coral_query(sql)
        else:
            rows = run_local_query(sql)
    except Exception as exc:
        return {
            "answer": (
                f"The query could not be run. Try rephrasing your question. ({exc})"
            ),
            "sql": sql,
            "rows": [],
            "row_count": 0,
        }

    try:
        answer = _summarize_chat_answer(question, rows)
    except Exception as exc:
        answer = (
            f"Found {len(rows)} matching rows, but summarization failed. ({exc})"
        )

    return {
        "answer": answer,
        "sql": sql,
        "rows": rows[:20],
        "row_count": len(rows),
    }


MOCK_COMBINED_DATA: list[dict[str, Any]] = [
    {
        "conversation_id": "demo-001",
        "ticket_state": "open",
        "opened_at": 1780000605,
        "contact_email": "email@projectmap.com",
        "contact_name": "Email",
        "subscription_status": "past_due",
        "customer_name": "Demo Customer",
        "merge_source": "mock",
    },
    {
        "conversation_id": "demo-002",
        "ticket_state": "open",
        "opened_at": 1780000604,
        "contact_email": "whatsapp@projectmap.com",
        "contact_name": "WhatsApp",
        "subscription_status": "active",
        "customer_name": "Demo Customer 2",
        "merge_source": "mock",
    },
]

MOCK_ANALYSIS: dict[str, Any] = {
    "top_themes": [
        {
            "theme": "Billing delays",
            "count": 1,
            "severity": "high",
            "description": "Customers report payment failures affecting renewal.",
            "example": "We cannot process our subscription renewal.",
        }
    ],
    "churn_risk_summary": {
        "total_open_tickets": 2,
        "high_risk_count": 1,
        "canceled_subscriptions": 0,
        "past_due_subscriptions": 1,
    },
    "triage_queue": [
        {
            "customer": "Demo Customer",
            "email": "email@projectmap.com",
            "priority": "P1",
            "issue_summary": "Past due subscription with open ticket",
            "recommended_action": "Contact finance and CS within 24h",
        }
    ],
    "categories": [
        {"name": "Pricing & Billing", "count": 1},
        {"name": "Onboarding & Support", "count": 1},
    ],
    "executive_summary": "Mock data: one high-risk past-due account needs immediate outreach.",
}
