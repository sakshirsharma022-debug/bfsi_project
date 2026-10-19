"""Tools the agent can call. Every result that comes from a filing includes its `citation`."""
from google.cloud import bigquery
from .common import bq, PROJECT, DATASET, embed_query
import re

def _t(name):
    return f"`{PROJECT}.{DATASET}.{name}`"

TICKER_ALIASES = {"googl": "alphabet", "goog": "alphabet", "google": "alphabet",
                  "amzn": "amazon", "fb": "meta", "nvda": "nvidia"}


def _clean_ticker(ticker):
    t = ticker.strip().lower()
    return TICKER_ALIASES.get(t, t)


def _clean_period(text):
    """'Q3 FY2025', 'fy2025 q3', 'Q3 2025' -> 'FY2025 Q3'. Only 'Q3' -> 'Q3'. '' -> ''."""
    year = re.search(r"(20\d\d)", text or "")
    quarter = re.search(r"Q\s*([1-4])", text or "", re.IGNORECASE)
    if year and quarter:
        return f"FY{year.group(1)} Q{quarter.group(1)}"
    return f"Q{quarter.group(1)}" if quarter else ""

# optional filters: empty string = no filter
FILTER = f"""(@ticker = '' OR LOWER(ticker) = @ticker)
  AND (@period = '' OR filing_id IN (
        SELECT filing_id FROM {_t('filings')}
        WHERE UPPER(fiscal_period) LIKE CONCAT('%', UPPER(@period), '%')))"""

def _run(sql, params):
    job = bq.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params))
    return [dict(row.items()) for row in job.result()]


def _vector_search(table, columns, question, ticker, fiscal_period, top_k):
    """Nearest rows of `table` to the question, optionally limited to one company / period."""
    cols = ", ".join(f"base.{c}" for c in columns)
    sql = f"""
        SELECT {cols}
        FROM VECTOR_SEARCH(
            (SELECT * FROM {_t(table)} WHERE {FILTER}),
            'embedding', (SELECT @q AS embedding),
            top_k => {top_k}, distance_type => 'COSINE')
        ORDER BY distance"""
    params = [bigquery.ArrayQueryParameter("q", "FLOAT64", embed_query(question)),
              bigquery.ScalarQueryParameter("ticker", "STRING", _clean_ticker(ticker)),
              bigquery.ScalarQueryParameter("period", "STRING", _clean_period(fiscal_period))]
    return _run(sql, params)

def _with_hint(rows, ticker, fiscal_period):
    """Wrap results; if empty, tell the agent how to recover instead of leaving it stuck."""
    result = {"results": rows}
    if not rows and (ticker or fiscal_period):
        result["note"] = (f"No rows matched ticker='{ticker}', fiscal_period='{fiscal_period}'. "
                          "Call list_filings and retry with its exact ticker and fiscal_period values.")
    return result

def list_filings() -> dict:
    """List the reports available (company, ticker, form, fiscal period). Call this first
    if you do not know which companies or periods exist."""
    rows = _run(f"SELECT ticker, company, form, fiscal_period, period_end FROM {_t('filings')} "
                "ORDER BY ticker, period_end", [])
    return {"filings": rows}


def search_tables(question: str, ticker: str = "", fiscal_period: str = "") -> dict:
    """Find table line items (revenue, R&D expense, total assets, ...) that match the question.
    Returns each matching row with ALL its numbers (one per column), the units, and a citation.

    Args:
        question: what to look for, e.g. "research and development expense".
        ticker: optional company ticker such as "AAPL".
        fiscal_period: optional period such as "Q3 FY2025" (see list_filings).
    """
    rows = _vector_search("table_rows",
                          ["row_id", "filing_id", "table_id", "table_title", "row_label",
                           "scale", "page", "citation"],
                          question, ticker, fiscal_period, top_k=8)
    ids = [r["row_id"] for r in rows]
    cells = _run(f"SELECT row_id, column_label, display, value FROM {_t('table_cells')} "
                 "WHERE row_id IN UNNEST(@ids)", [bigquery.ArrayQueryParameter("ids", "STRING", ids)])
    for r in rows:
        r["values"] = [{"column": c["column_label"], "shown_as": c["display"], "value": c["value"]}
                       for c in cells if c["row_id"] == r["row_id"]]
    return {"results": rows}


def get_table(table_id: str) -> dict:
    """Get one full table (as markdown) with its units and citation. Use the table_id from
    search_tables, e.g. when you need to see the other rows around a value."""
    rows = _run(f"SELECT table_id, table_title, scale, markdown, citation FROM {_t('tables')} "
                "WHERE table_id = @id", [bigquery.ScalarQueryParameter("id", "STRING", table_id)])
    return rows[0] if rows else {"error": f"table {table_id} not found"}


def search_text(question: str, ticker: str = "", fiscal_period: str = "") -> dict:
    """Search the written parts of the reports (MD&A, risk factors, notes). Returns passages
    with citations. Use this for explanations and reasons, not for numbers in tables.

    Args:
        question: what to look for.
        ticker: optional company ticker such as "AAPL".
        fiscal_period: optional period such as "Q3 FY2025".
    """
    rows = _vector_search("text_chunks", ["chunk_id", "text", "citation"],
                          question, ticker, fiscal_period, top_k=5)
    return {"results": rows}


def calculate(operation: str, a: float, b: float) -> dict:
    """Do arithmetic exactly. Never calculate in your head; always use this.

    Args:
        operation: one of
            "percent_change" - change from a (old value) to b (new value), in percent
            "difference"     - b minus a
            "ratio"          - a divided by b
            "percent_of"     - a as a percentage of b
        a: first number.
        b: second number.
    """
    try:
        if operation == "percent_change":
            return {"result": round((b - a) / abs(a) * 100, 2), "unit": "%",
                    "formula": f"({b} - {a}) / |{a}| * 100"}
        if operation == "difference":
            return {"result": round(b - a, 4), "formula": f"{b} - {a}"}
        if operation == "ratio":
            return {"result": round(a / b, 4), "formula": f"{a} / {b}"}
        if operation == "percent_of":
            return {"result": round(a / b * 100, 2), "unit": "%", "formula": f"{a} / {b} * 100"}
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
    results = {}
    for ticker in tickers.split(","):
        ticker = ticker.strip()
        if ticker:
            results[ticker] = search_tables(question, ticker, fiscal_period)
    return results