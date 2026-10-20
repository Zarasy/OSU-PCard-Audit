
import os
import re
import sqlite3
import time
from pathlib import Path
from html import escape

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from google import genai


# ==================================================
# CONFIGURATION
# ==================================================

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DATABASE = BASE_DIR / "pcards.db"

COLUMNS = [
    "Year",
    "Month",
    "FullName",
    "ID",
    "AgencyNumber",
    "AgencyName",
    "CardholderLastName",
    "CardholderFirstInitial",
    "Description",
    "Amount",
    "Vendor",
    "TransactionDate",
    "PostedDate",
    "MCC",
]

st.set_page_config(
    page_title="OSU P-Card Audit Platform",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# ==================================================
# STYLING
# ==================================================

st.markdown(
    """
    <style>
    .stApp {
        background-color: #f4f6fa;
    }

    .block-container {
        max-width: 1200px;
        padding-top: 2rem;
        padding-bottom: 3rem;
    }

    h1, h2, h3 {
        color: #172740;
    }

    .header-box {
        background: #172740;
        color: white;
        padding: 32px;
        border-radius: 15px;
        margin-bottom: 25px;
    }

    .header-title {
        color: white;
        font-size: 29px;
        font-weight: 750;
        margin-bottom: 9px;
    }

    .header-subtitle {
        color: #cbd5e1;
        font-size: 14px;
    }

    .section-note {
        color: #64748b;
        font-size: 14px;
        line-height: 1.7;
    }

    /* Make the navigation tabs prominent */
    .stTabs [data-baseweb="tab-list"] {
        display: flex;
        gap: 16px;
        background: transparent;
        border: none;
        padding: 5px 0 18px;
    }

    .stTabs [data-baseweb="tab"] {
        flex: 1;
        justify-content: center;
        min-height: 76px;
        padding: 18px 22px;
        border-radius: 12px;
        border: 2px solid #d5ddea;
        background-color: #e9eef5;
        color: #172740;
        font-size: 19px;
        font-weight: 700;
        white-space: normal;
        text-align: center;
        transition:
            background-color 0.2s ease,
            border-color 0.2s ease;
    }

    .stTabs [data-baseweb="tab"]:hover {
        background-color: #dbe7f7;
        border-color: #8da9d6;
        color: #172740;
    }

    .stTabs [data-baseweb="tab"][aria-selected="true"] {
        background-color: #172740;
        color: #ffffff;
        border-color: #172740;
        box-shadow: 0 5px 14px rgba(23, 39, 64, 0.16);
    }

    .stTabs [data-baseweb="tab"][aria-selected="true"] p {
        color: #ffffff;
    }

    .stTabs [data-baseweb="tab"] p {
        font-size: 19px;
        font-weight: 700;
        color: inherit;
    }

    .stTabs [data-baseweb="tab-highlight"],
    .stTabs [data-baseweb="tab-border"] {
        display: none;
    }

    div[data-testid="stMetric"] {
        background: white;
        border: 1px solid #e1e6ee;
        border-radius: 12px;
        padding: 22px;
    }

    div[data-testid="stMetricValue"] {
        font-size: 36px;
        color: #172740;
    }

    div[data-testid="stAlert"] {
        border-radius: 10px;
    }

    .result-value {
        font-size: 42px;
        font-weight: 750;
        color: #172740;
        overflow-wrap: anywhere;
        margin: 12px 0;
    }

    @media (max-width: 700px) {
        .stTabs [data-baseweb="tab"] {
            min-height: 70px;
            padding: 12px 8px;
            font-size: 14px;
        }

        .stTabs [data-baseweb="tab"] p {
            font-size: 14px;
        }

        .header-box {
            padding: 24px 20px;
        }

        .header-title {
            font-size: 24px;
        }

        .result-value {
            font-size: 32px;
        }
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ==================================================
# DATABASE FUNCTIONS
# ==================================================

def get_connection():
    if not DATABASE.exists():
        raise FileNotFoundError(
            "The P-card database is not available."
        )

    connection = sqlite3.connect(
        DATABASE.as_uri() + "?mode=ro",
        uri=True,
        timeout=5,
    )

    connection.row_factory = sqlite3.Row
    return connection


def execute_readonly(sql, parameters=()):
    connection = get_connection()

    try:
        def authorize(action, arg1, arg2, db, source):
            allowed = {
                sqlite3.SQLITE_SELECT,
                sqlite3.SQLITE_READ,
                sqlite3.SQLITE_FUNCTION,
            }

            return (
                sqlite3.SQLITE_OK
                if action in allowed
                else sqlite3.SQLITE_DENY
            )

        connection.set_authorizer(authorize)

        deadline = time.monotonic() + 5

        def check_timeout():
            return (
                1
                if time.monotonic() > deadline
                else 0
            )

        connection.set_progress_handler(
            check_timeout,
            1000,
        )

        cursor = connection.execute(
            sql,
            parameters,
        )

        rows = cursor.fetchmany(101)

        return {
            "columns": [
                item[0]
                for item in cursor.description
            ],
            "rows": [
                dict(row)
                for row in rows[:100]
            ],
            "limited": len(rows) > 100,
        }

    finally:
        connection.close()


@st.cache_data(ttl=300)
def get_years():
    result = execute_readonly(
        'SELECT DISTINCT "Year" '
        'FROM pcards '
        'WHERE "Year" IS NOT NULL '
        'ORDER BY "Year"'
    )

    return [
        row["Year"]
        for row in result["rows"]
    ]


def search_transactions(year, field, keyword):
    if field not in ("Description", "Vendor"):
        raise ValueError("Invalid search field.")

    keyword = keyword.strip()

    if not keyword or len(keyword) > 100:
        raise ValueError(
            "Enter a keyword of up to 100 characters."
        )

    safe_keyword = (
        keyword.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )

    sql = f"""
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
    """

    return execute_readonly(
        sql,
        (
            int(year),
            "%" + safe_keyword + "%",
        ),
    )


# ==================================================
# GEMINI FUNCTIONS
# ==================================================

def get_secret(name, default=None):
    try:
        value = st.secrets.get(name)

        if value:
            return value

    except Exception:
        pass

    return os.getenv(name, default)


def ask_database(question):
    api_key = get_secret("GEMINI_API_KEY")

    if not api_key:
        raise RuntimeError(
            "Gemini API key is not configured."
        )

    model = get_secret(
        "GEMINI_MODEL",
        "gemini-3.5-flash-lite",
    )

    prompt = f"""
You are an SQL assistant for a P-card auditor.

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

    client = genai.Client(api_key=api_key)

    response = client.models.generate_content(
        model=model,
        contents=prompt,
    )

    sql = (response.text or "").strip()

    sql = re.sub(
        r"^```(?:sql)?\s*|\s*```$",
        "",
        sql,
        flags=re.IGNORECASE,
    ).strip()

    if sql.endswith(";"):
        sql = sql[:-1].strip()

    if not re.match(
        r"^(SELECT|WITH)\b",
        sql,
        re.IGNORECASE,
    ):
        raise ValueError(
            "The AI did not generate a valid SELECT query."
        )

    if ";" in sql:
        raise ValueError(
            "Multiple SQL statements are not allowed."
        )

    result = execute_readonly(sql)

    return {
        "question": question,
        "sql": sql,
        **result,
    }


# ==================================================
# RESULTS
# ==================================================

def show_results(result):
    rows = result["rows"]
    columns = result["columns"]

    if not rows:
        st.info("No matching records were found.")
        return

    dataframe = pd.DataFrame(
        rows,
        columns=columns,
    )

    st.dataframe(
        dataframe,
        use_container_width=True,
        hide_index=True,
    )

    if result["limited"]:
        st.caption(
            "Only the first 100 results are displayed."
        )


def show_ai_answer(result):
    rows = result["rows"]
    columns = result["columns"]
    question = result["question"]

    st.success("Analysis complete")

    st.caption("YOUR QUESTION")
    st.write(question)

    if not rows:
        st.info(
            "No matching records were found "
            "for this question."
        )

    elif len(rows) == 1 and len(columns) == 1:
        value = rows[0][columns[0]]

        if isinstance(value, int):
            display_value = f"{value:,}"

        elif isinstance(value, float):
            display_value = f"{value:,.2f}"

        else:
            display_value = str(value)

        st.markdown(
            '<div class="result-value">'
            + escape(display_value)
            + "</div>",
            unsafe_allow_html=True,
        )

        question_lower = question.lower()

        if (
            "how many" in question_lower
            and "transaction" in question_lower
        ):
            st.write(
                "P-card transactions were recorded "
                "for the requested analysis."
            )

        elif (
            "total" in question_lower
            and (
                "spending" in question_lower
                or "spent" in question_lower
            )
        ):
            st.write(
                "Total transaction amount "
                "for the requested analysis."
            )

        else:
            st.write(
                "Result calculated from "
                "the P-card database."
            )

    else:
        st.write(
            f"The analysis returned "
            f"{len(rows)} result rows."
        )

        show_results(result)

    with st.expander(
        "Technical details · View SQL query"
    ):
        st.code(
            result["sql"],
            language="sql",
        )


# ==================================================
# HEADER
# ==================================================

st.markdown(
    """
    <div class="header-box">
        <div class="header-title">
            OSU P-Card Audit Platform
        </div>
        <div class="header-subtitle">
            Transaction Monitoring &amp;
            Internal Control Review
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)


# ==================================================
# NAVIGATION
# ==================================================

dashboard_tab, ai_tab = st.tabs(
    [
        "▦  Audit Dashboard",
        "✦  Ask the Database",
    ]
)


# ==================================================
# TAB 1: AUDIT DASHBOARD
# ==================================================

with dashboard_tab:
    st.header(
        "Prohibited Purchase Dashboard"
    )

    st.markdown(
        '<p class="section-note">'
        "Identify transactions that may require "
        "further investigation under P-card "
        "purchasing policies."
        "</p>",
        unsafe_allow_html=True,
    )

    with st.expander(
        "How to use this dashboard",
        expanded=True,
    ):
        st.markdown(
            """
            1. Select a transaction year.
            2. Search by description or vendor name.
            3. Review matching transactions.
            4. Download results for follow-up.

            **Important:** A keyword match does not
            establish that a policy violation occurred.
            """
        )

    try:
        years = get_years()

        if not years:
            st.warning(
                "No transaction years were found."
            )
            st.stop()

        default_year = (
            years.index(2014)
            if 2014 in years
            else len(years) - 1
        )

        selected_year = st.selectbox(
            "Transaction Year",
            years,
            index=default_year,
        )

        col1, col2 = st.columns(2)

        with col1:
            with st.container(border=True):
                st.subheader(
                    "Description Search"
                )

                st.caption(
                    "Search transaction descriptions "
                    "for purchases requiring review."
                )

                description_keyword = (
                    st.text_input(
                        "Description keyword",
                        placeholder=(
                            "e.g. alcohol, gift, gasoline"
                        ),
                    )
                )

                description_search = st.button(
                    "Search Descriptions",
                    use_container_width=True,
                    type="primary",
                )

        with col2:
            with st.container(border=True):
                st.subheader(
                    "Vendor Search"
                )

                st.caption(
                    "Search merchant names for "
                    "transactions requiring review."
                )

                vendor_keyword = st.text_input(
                    "Vendor keyword",
                    placeholder="e.g. Amazon, USPS",
                )

                vendor_search = st.button(
                    "Search Vendors",
                    use_container_width=True,
                )

        if description_search or vendor_search:
            field = (
                "Description"
                if description_search
                else "Vendor"
            )

            keyword = (
                description_keyword
                if description_search
                else vendor_keyword
            )

            try:
                with st.spinner(
                    "Searching transactions..."
                ):
                    result = search_transactions(
                        selected_year,
                        field,
                        keyword,
                    )

                    st.session_state[
                        "search_result"
                    ] = result

                    st.session_state[
                        "search_label"
                    ] = (
                        f"{field} search: "
                        f"{keyword} ({selected_year})"
                    )

            except Exception as error:
                st.error(str(error))

        st.divider()

        st.subheader(
            "Transaction Results"
        )

        if "search_result" in st.session_state:
            result = st.session_state[
                "search_result"
            ]

            st.caption(
                st.session_state.get(
                    "search_label",
                    "Search results",
                )
            )

            st.write(
                f"**{len(result['rows'])} "
                f"matching transactions displayed**"
            )

            show_results(result)

            if result["rows"]:
                dataframe = pd.DataFrame(
                    result["rows"],
                    columns=result["columns"],
                )

                st.download_button(
                    "Download Results as CSV",
                    data=dataframe.to_csv(
                        index=False
                    ).encode("utf-8-sig"),
                    file_name=(
                        "pcard_audit_results.csv"
                    ),
                    mime="text/csv",
                )

        else:
            st.info(
                "Enter a keyword and run a search "
                "to display transactions."
            )

    except Exception as error:
        st.error(
            "The database could not be loaded: "
            + str(error)
        )

    st.divider()

    with st.expander(
        "Prohibited Purchase Categories"
    ):
        st.markdown(
            """
            - Alcohol
            - Cash and ATM transactions
            - Decorations
            - Donations and sponsorships
            - Gasoline
            - Gifts and gift cards
            - Insurance
            - Late fees
            - Mail and postage
            - Moving expenses
            - Personal purchases
            - Individual memberships and dues
            - Salaries, wages and benefits
            - Service and incentive awards
            """
        )


# ==================================================
# TAB 2: ASK THE DATABASE
# ==================================================

with ai_tab:
    st.header("Ask the Database")

    st.markdown(
        '<p class="section-note">'
        "Ask a question in plain English and receive "
        "a database-based answer using Gemini AI."
        "</p>",
        unsafe_allow_html=True,
    )

    with st.expander(
        "How to use the AI assistant",
        expanded=True,
    ):
        st.markdown(
            """
            Enter a question and click
            **Analyze Question**.

            **Example questions:**

            - How many transactions occurred in 2014?
            - What was the total spending in 2013?
            - Which 10 vendors received the most money in 2014?
            - Which employees had the highest spending in 2014?

            The assistant generates a read-only
            database query. Results should
            be reviewed by an auditor.
            """
        )

    question = st.text_area(
        "Your Question",
        placeholder=(
            "Ask a question about "
            "P-card transactions..."
        ),
        height=110,
    )

    if st.button(
        "Analyze Question",
        type="primary",
        key="analyze_button",
    ):
        if not question.strip():
            st.warning(
                "Please enter a question."
            )

        elif len(question.strip()) > 1000:
            st.warning(
                "Please keep the question "
                "under 1,000 characters."
            )

        else:
            try:
                with st.spinner(
                    "Gemini is analyzing "
                    "your question..."
                ):
                    st.session_state[
                        "ai_result"
                    ] = ask_database(
                        question.strip()
                    )

            except Exception:
                st.session_state.pop(
                    "ai_result",
                    None,
                )

                st.error(
                    "The analysis could not "
                    "be completed. Check the "
                    "Gemini configuration "
                    "and database connection."
                )

    if "ai_result" in st.session_state:
        st.divider()

        st.subheader(
            "Analysis Result"
        )

        show_ai_answer(
            st.session_state[
                "ai_result"
            ]
        )


# ==================================================
# FOOTER
# ==================================================

st.divider()

st.caption(
    "OSU P-Card Audit Platform | "
    "Educational auditing project"
)
