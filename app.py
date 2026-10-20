
import os
import re
import sqlite3
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_file
from google import genai

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DATABASE = BASE_DIR / "pcards.db"
app = Flask(__name__)

COLUMNS = [
    "Year", "Month", "FullName", "ID",
    "AgencyNumber", "AgencyName",
    "CardholderLastName", "CardholderFirstInitial",
    "Description", "Amount", "Vendor",
    "TransactionDate", "PostedDate", "MCC"
]


def database_connection():
    if not DATABASE.exists():
        raise FileNotFoundError("pcards.db was not found")

    # Read-only database connection
    connection = sqlite3.connect(
        DATABASE.as_uri() + "?mode=ro",
        uri=True,
        timeout=5
    )
    connection.row_factory = sqlite3.Row
    return connection


def fetch_results(sql, parameters=()):
    connection = database_connection()

    try:
        # Only allow reading data and using SQL functions
        def authorize(action, arg1, arg2, db, source):
            allowed = {
                sqlite3.SQLITE_SELECT,
                sqlite3.SQLITE_READ,
                sqlite3.SQLITE_FUNCTION
            }
            return (
                sqlite3.SQLITE_OK
                if action in allowed
                else sqlite3.SQLITE_DENY
            )

        connection.set_authorizer(authorize)

        # Stop queries that take too long
        import time
        deadline = time.monotonic() + 5

        def check_timeout():
            return 1 if time.monotonic() > deadline else 0

        connection.set_progress_handler(check_timeout, 1000)

        cursor = connection.execute(sql, parameters)
        rows = cursor.fetchmany(101)
        limited = len(rows) > 100

        return {
            "columns": [item[0] for item in cursor.description],
            "rows": [dict(row) for row in rows[:100]],
            "limited": limited
        }
    finally:
        connection.close()


@app.route("/")
def home():
    return send_file(BASE_DIR / "index.html")


@app.route("/api/tables")
def get_tables():
    return jsonify({"tables": ["pcards"]})


@app.route("/api/columns")
def get_columns():
    connection = database_connection()
    try:
        rows = connection.execute(
            "PRAGMA table_info(pcards)"
        ).fetchall()
        return jsonify({
            "columns": [
                {"name": row[1], "type": row[2]}
                for row in rows
            ]
        })
    finally:
        connection.close()


@app.route("/api/years")
def get_years():
    result = fetch_results(
        'SELECT DISTINCT "Year" FROM pcards '
        'WHERE "Year" IS NOT NULL ORDER BY "Year"'
    )
    return jsonify({
        "years": [row["Year"] for row in result["rows"]]
    })


@app.route("/api/search")
def search_transactions():
    try:
        year = int(request.args.get("year", "2014"))
        field = request.args.get("field", "Description")
        keyword = request.args.get("keyword", "").strip()

        if field not in ("Description", "Vendor"):
            return jsonify({"error": "Invalid search field"}), 400

        if not keyword or len(keyword) > 100:
            return jsonify({"error": "Enter a valid keyword"}), 400

        sql = f'''
            SELECT
                "Year",
                "TransactionDate",
                "FullName",
                "AgencyName",
                "Vendor",
                "Description",
                "Amount",
                "MCC"
            FROM pcards
            WHERE "Year" = ?
              AND "{field}" LIKE ? ESCAPE '\\'
            ORDER BY ABS("Amount") DESC
            LIMIT 101
        '''

        # Treat keyword characters as literal text
        safe_keyword = (
            keyword.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )

        result = fetch_results(
            sql, (year, "%" + safe_keyword + "%")
        )
        return jsonify(result)

    except (ValueError, sqlite3.Error) as error:
        return jsonify({"error": str(error)}), 400


@app.route("/api/ask", methods=["POST"])
def ask_database():
    data = request.get_json(silent=True) or {}
    question = str(data.get("question", "")).strip()

    if not question or len(question) > 1000:
        return jsonify({"error": "Enter a question"}), 400

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return jsonify({
            "error": "GEMINI_API_KEY is missing from .env"
        }), 500

    model = os.getenv(
        "GEMINI_MODEL", "gemini-2.5-flash-lite"
    )

    prompt = f"""
You are an SQL assistant for an auditor.

Convert the user's question into exactly ONE
read-only SQLite SELECT query.

Database table: pcards

Available columns:
{", ".join(COLUMNS)}

Rules:
- Use only the pcards table.
- Use only the listed columns.
- Generate SQLite-compatible SQL.
- Never generate INSERT, UPDATE, DELETE, DROP,
  ALTER, CREATE, PRAGMA, or ATTACH.
- Never include multiple statements.
- Do not invent database values or results.
- Use LIMIT 100 for transaction-level results.
- Return SQL only, without explanation or markdown.

User question:
{question}
"""

    try:
        client = genai.Client(api_key=api_key)

        response = client.models.generate_content(
            model=model,
            contents=prompt
        )

        sql = (response.text or "").strip()
        sql = re.sub(
            r"^```(?:sql)?\s*|\s*```$",
            "",
            sql,
            flags=re.IGNORECASE
        ).strip()

        if sql.endswith(";"):
            sql = sql[:-1].strip()

        if not re.match(
            r"^(SELECT|WITH)\b", sql, re.IGNORECASE
        ):
            return jsonify({
                "error": "Gemini did not return a SELECT query"
            }), 400

        if ";" in sql:
            return jsonify({
                "error": "Multiple SQL statements are not allowed"
            }), 400

        result = fetch_results(sql)

        return jsonify({
            "question": question,
            "sql": sql,
            **result
        })

    except Exception as error:
        # Do not return secret-bearing error details
        app.logger.error(
            "AI query failed: %s",
            type(error).__name__
        )
        return jsonify({
            "error": "The AI request or database query failed. "
                     "Check the model name, API quota and SQL."
        }), 500


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False
    )
