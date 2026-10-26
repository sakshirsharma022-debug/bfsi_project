"""Tools the agent can call. Every result that comes from a filing includes its `citation`."""
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from google.cloud import bigquery
import os
import sys

# Use the single shared backend/common.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import bq, PROJECT, DATASET, embed_query
logger = logging.getLogger(__name__)


def _t(name):
    return f"`{PROJECT}.{DATASET}.{name}`"


TICKER_ALIASES = {
    "googl": "alphabet",
    "goog": "alphabet",
    "google": "alphabet",
    "amzn": "amazon",
    "fb": "meta",
    "nvda": "nvidia",
}


def _ticker_variants(ticker):
    """All spellings that may be stored in the `ticker` column for one company.

    'GOOGL' -> ['alphabet', 'googl', 'goog', 'google'] so filings ingested from the UI
    (stored under the symbol the user typed) are found just like the original folders.
    """
    t = (ticker or "").strip().lower()
    if not t:
        return []
    canonical = TICKER_ALIASES.get(t, t)
    variants = {canonical, t}
    variants.update(alias for alias, name in TICKER_ALIASES.items() if name == canonical)
    return sorted(variants)


def _clean_period(text):
    """'Q3 FY2025', 'fy2025 q3', 'Q3 2025' -> 'FY2025 Q3'."""
    year = re.search(r"(20\d\d)", text or "")
    quarter = re.search(r"Q\s*([1-4])", text or "", re.IGNORECASE)
    if year and quarter:
        return f"FY{year.group(1)} Q{quarter.group(1)}"
    return f"Q{quarter.group(1)}" if quarter else ""


FILTER = f"""(ARRAY_LENGTH(@tickers) = 0 OR LOWER(ticker) IN UNNEST(@tickers))
  AND (@period = '' OR filing_id IN (
        SELECT filing_id FROM {_t('filings')}
        WHERE UPPER(fiscal_period) LIKE CONCAT('%', UPPER(@period), '%')))"""


def _run(sql, params):
    job = bq.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params))
    return [dict(row.items()) for row in job.result()]


def _vector_search(table, columns, question, ticker, fiscal_period, top_k, embedding=None):
    """Nearest rows with an optional precomputed embedding."""
    cols = ", ".join(f"base.{c}" for c in columns)
    sql = f"""
        SELECT {cols}
        FROM VECTOR_SEARCH(
            (SELECT * FROM {_t(table)} WHERE {FILTER}),
            'embedding', (SELECT @q AS embedding),
            top_k => {top_k}, distance_type => 'COSINE')
        ORDER BY distance"""
    if embedding is None:
        embedding = embed_query(question)
    params = [
        bigquery.ArrayQueryParameter("q", "FLOAT64", embedding),
        bigquery.ArrayQueryParameter("tickers", "STRING", _ticker_variants(ticker)),
        bigquery.ScalarQueryParameter("period", "STRING", _clean_period(fiscal_period)),
    ]
    return _run(sql, params)


def _with_hint(rows, ticker, fiscal_period):
    result = {"results": rows}
    if not rows and (ticker or fiscal_period):
        result["note"] = (
            f"No rows matched ticker='{ticker}', fiscal_period='{fiscal_period}'. "
            "Call list_filings and retry with its exact ticker and fiscal_period values."
        )
    return result


# ------------------------------------------------------------------
# Internal helpers (not exposed to the agent)
# ------------------------------------------------------------------

def _search_tables(question, ticker="", fiscal_period="", embedding=None):
    rows = _vector_search(
        "table_rows",
        ["row_id", "filing_id", "table_id", "table_title", "row_label", "scale", "page", "citation"],
        question,
        ticker,
        fiscal_period,
        top_k=5,
        embedding=embedding,
    )
    ids = [r["row_id"] for r in rows]
    if not ids:
        return _with_hint(rows, ticker, fiscal_period)

    table_ids = list({r["table_id"] for r in rows})

    # The cell values and the "% of revenue" check are independent:
    # run both BigQuery queries at the same time instead of one after another.
    def fetch_cells():
        return _run(
            f"SELECT row_id, column_label, display, value FROM {_t('table_cells')} "
            "WHERE row_id IN UNNEST(@ids)",
            [bigquery.ArrayQueryParameter("ids", "STRING", ids)],
        )

    def fetch_percent_tables():
        return _run(
            f"SELECT DISTINCT table_id FROM {_t('table_cells')} "
            "WHERE table_id IN UNNEST(@t) "
            "AND LOWER(row_label) IN ('revenue','revenues','net sales','total revenue','total revenues') "
            "AND value = 100",
            [bigquery.ArrayQueryParameter("t", "STRING", table_ids)],
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        cells_job = pool.submit(fetch_cells)
        pct_job = pool.submit(fetch_percent_tables)
        cells = cells_job.result()
        pct = pct_job.result()

    for r in rows:
        r["values"] = [
            {"column": c["column_label"], "shown_as": c["display"], "value": c["value"]}
            for c in cells if c["row_id"] == r["row_id"]
        ]

    percent_tables = {p["table_id"] for p in pct}
    for r in rows:
        if r["table_id"] in percent_tables:
            r["scale"] = "% of revenue"
            r["note"] = (
                "These values are percentages of revenue (not dollars, and not rates "
                "such as the effective tax rate)."
            )
    return {"results": rows}


def _search_text(question, ticker="", fiscal_period="", embedding=None):
    rows = _vector_search(
        "text_chunks",
        ["chunk_id", "filing_id", "page_start", "section", "text", "citation"],
        question,
        ticker,
        fiscal_period,
        top_k=3,
        embedding=embedding,
    )
    return _with_hint(rows, ticker, fiscal_period)


# ------------------------------------------------------------------
# Tools exposed to the agent
# ------------------------------------------------------------------

def list_filings() -> dict:
    """List available reports (company, ticker, form, fiscal period)."""
    rows = _run(
        f"SELECT ticker, company, form, fiscal_period, period_end "
        f"FROM {_t('filings')} ORDER BY ticker, period_end",
        [],
    )
    return {"filings": rows}


def search_tables(question: str, ticker: str = "", fiscal_period: str = "") -> dict:
    """Find table line items for ONE company and return numbers, units, and citations.
    For comparisons across several companies use search_tables_many instead.

    Args:
        question: what to look for, e.g. "total net sales" or "research and development expense".
        ticker: one company id such as "meta" (optional).
        fiscal_period: optional period such as "FY2025 Q3".
    """
    return _search_tables(question, ticker, fiscal_period)


def get_table(table_id: str) -> dict:
    """Get one full table when surrounding context is needed."""
    rows = _run(
        f"SELECT table_id, filing_id, page, table_title, scale, markdown, citation FROM {_t('tables')} "
        "WHERE table_id = @id",
        [bigquery.ScalarQueryParameter("id", "STRING", table_id)],
    )
    return rows[0] if rows else {"error": f"table {table_id} not found"}


def search_text(question: str, ticker: str = "", fiscal_period: str = "") -> dict:
    """Search written filing text for explanations and reasons."""
    return _search_text(question, ticker, fiscal_period)


def calculate(operation: str, a: float, b: float) -> dict:
    """Perform arithmetic exactly. Always use this for derived numbers instead of computing them yourself.

    Args:
        operation: one of "percent_change" (from a to b), "difference" (b - a),
            "ratio" (a / b), "percent_of" (a as a percent of b), "sum" (a + b).
        a: the first number (for percent_change, the older / base value).
        b: the second number (for percent_change, the newer value).
    """
    logger.info("calculate operation=%r a=%s b=%s", operation, a, b)
    try:
        if operation == "percent_change":
            return {
                "result": round((b - a) / abs(a) * 100, 2),
                "unit": "%",
                "formula": f"({b} - {a}) / |{a}| * 100",
            }
        if operation == "difference":
            return {"result": round(b - a, 4), "formula": f"{b} - {a}"}
        if operation == "ratio":
            return {"result": round(a / b, 4), "formula": f"{a} / {b}"}
        if operation == "percent_of":
            return {
                "result": round(a / b * 100, 2),
                "unit": "%",
                "formula": f"{a} / {b} * 100",
            }
        if operation == "sum":
            return {"result": round(a + b, 4), "formula": f"{a} + {b}"}
    except ZeroDivisionError:
        return {"error": "division by zero"}
    return {"error": f"unknown operation '{operation}'"}


def search_tables_many(question: str, tickers: str, fiscal_period: str = "") -> dict:
    """Search table line items for SEVERAL companies in one call. Use this for comparisons.
    Returns the best matching rows for each company separately, with numbers, units and citations.

    Args:
        question: what to look for, e.g. "revenue" or "research and development expense".
        tickers: company ids separated by commas, e.g. "meta,alphabet".
        fiscal_period: optional period such as "FY2025 Q3".
    """
    names = [t.strip() for t in tickers.split(",") if t.strip()]
    if not names:
        return {}

    embedding = embed_query(question)          # embed the question once for all companies

    def one(ticker):
        try:
            return _search_tables(question, ticker, fiscal_period, embedding)
        except Exception as exc:               # one company failing should not lose the others
            logger.warning("search_tables_many failed for %s: %s", ticker, exc)
            return {"results": [], "error": str(exc)[:200]}

    with ThreadPoolExecutor(max_workers=min(len(names), 8)) as pool:
        futures = {t: pool.submit(one, t) for t in names}
        return {t: f.result() for t, f in futures.items()}


def search_all(question: str, ticker: str = "", fiscal_period: str = "") -> dict:
    """Search BOTH tables and text when the question genuinely needs both."""
    embedding = embed_query(question)
    return {
        "tables": _search_tables(question, ticker, fiscal_period, embedding),
        "passages": _search_text(question, ticker, fiscal_period, embedding),
    }