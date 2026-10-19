"""Turns an EDGAR 10-Q HTML file into clean tables. No GCP code here, so it is easy to test.

    tables = extract_tables(html_bytes)

Each table is a dict:
    table_no, title, statement_type, section, scale, columns, rows
    rows = [{row_label, cells: [{column_label, display, value, concept}]}]
"""
import re
from bs4 import BeautifulSoup

NUMBER = re.compile(r"^\(?-?\$?\s?[\d,]+(\.\d+)?\)?%?$")
DASHES = {"—", "–", "-", "$—"}
YEAR = re.compile(r"^(19|20)\d\d$")
SKIP_TITLE = re.compile(r"^\(|in (thousands|millions|billions)|table of contents|form 10-q|^\d+$", re.I)
TOTAL_ROW = re.compile(r"^(total|net cash|cash (generated|used|provided))\b", re.I)   # ends a group like "Current assets:"

STATEMENT_TYPES = [  # keyword in title -> statement type
    ("balance sheet", "balance_sheet"), ("financial condition", "balance_sheet"),
    ("operations", "income_statement"), ("income", "income_statement"), ("earnings", "income_statement"),
    ("cash flow", "cash_flow"), ("equity", "equity"), ("stockholders", "equity"),
]


def clean(text):
    text = (text or "").replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\s+([%)])", r"\1", text)      # "10 %" -> "10%", "5 )" -> "5)"


def is_number(text):
    return bool(NUMBER.match(text)) or text in DASHES


def to_float(text):
    t = text.replace("$", "").replace(",", "").replace("%", "").strip()
    if t in DASHES or not t:
        return 0.0
    negative = t.startswith("(") or t.startswith("-")
    value = float(t.strip("()-"))
    return -value if negative else value


# ---------- reading one <tr> ----------
def read_cells(tr):
    """Return non-empty cells as {start, end, text, concept}.
    start/end = grid columns (colspan aware). A lone '$' is dropped; ')' and '%' are glued
    onto the previous number, because EDGAR puts them in separate <td>s."""
    cells, col, open_paren = [], 0, ""
    for td in tr.find_all(["td", "th"]):
        span = int(td.get("colspan") or 1)
        text = clean(td.get_text(" "))
        if text == "(":
            open_paren = "("
            col += span
            continue
        if text and open_paren:
            text, open_paren = "(" + text, ""
            col -= 1                                         # number starts where the "(" cell was
        if text in {")", "%", ")%", "%)"} and cells:
            cells[-1]["text"] += text
            cells[-1]["end"] = col + span - 1
        elif text and text != "$":
            tag = td.find("ix:nonfraction")                    # inline-XBRL tag, if present
            cells.append({"start": col, "end": col + span - 1, "text": text,
                          "concept": tag.get("name", "") if tag else ""})
        col += span
    return cells


def parse_rows(table):
    """Split a <table> into header rows and data rows."""
    header_rows, data_rows, group, started = [], [], "", False
    for tr in table.find_all("tr"):
        cells = read_cells(tr)
        if not cells:
            continue
        numbers = [c for c in cells if is_number(c["text"])]
        labels = [c for c in cells if not is_number(c["text"])]
        all_years = numbers and all(YEAR.match(c["text"]) for c in numbers)

        if all_years and not labels and not started:         # header row like "2025   2024"
            header_rows.append(cells)
        elif numbers and (started or not all_years):
            started = True
            label = labels[0]["text"].rstrip(":") if labels else "Total"
            if TOTAL_ROW.match(label):
                group = ""                                   # a total closes the group
            elif group:
                label = f"{group}: {label}"
            data_rows.append({"label": label, "cells": numbers})
        elif not started:
            header_rows.append(cells)                        # text header, e.g. "Three Months Ended"
        elif len(cells) == 1:                                # group row, e.g. "Operating expenses:"
            group = cells[0]["text"].rstrip(":")
    return header_rows, data_rows


def column_label(header_rows, col):
    """Combine every header cell whose span covers this grid column."""
    parts = []
    for row in header_rows:
        hit = [c["text"] for c in row if c["start"] <= col <= c["end"]]
        if not hit:                                          # fall back to nearest header cell
            nearest = min(row, key=lambda c: abs((c["start"] + c["end"]) / 2 - col))
            hit = [nearest["text"]]
        parts += [h for h in hit if h not in parts]
    return " / ".join(parts) or "Value"


# ---------- context around a table ----------
DATE_ONLY = re.compile(r"^(january|february|march|april|may|june|july|august|september|october|november|december)\b[\d,\s]*$", re.I)


def looks_like_heading(text):
    """A table title is short, starts with a capital, is not a sentence and not just a date."""
    return (8 <= len(text) <= 120 and not SKIP_TITLE.search(text) and not is_number(text)
            and not text.endswith(".") and (text[0].isupper() or text[0].isdigit())
            and not DATE_ONLY.match(text))


def find_title(table):
    for s in table.find_all_previous(string=True, limit=60):
        if s.find_parent(["td", "th"]):                      # reached the previous table: no heading in between
            break
        text = clean(re.sub(r"\(unaudited\)", "", clean(s), flags=re.I))
        if looks_like_heading(text):
            return text
    return ""                                                # caller falls back to the first row label


def find_scale(table):
    nearby = " ".join(clean(s) for s in table.find_all_previous(string=True, limit=15))
    m = re.search(r"in (thousands|millions|billions)", nearby, re.I)
    return m.group(1).lower() if m else "units"


PART_RE = re.compile(r"^\s*PART\s+[IV]+\b", re.I)
ITEM_RE = re.compile(r"^\s*Item\s+\d+[A]?\.", re.I)


def find_section(table):
    parts = []
    for pattern in (PART_RE, ITEM_RE):
        node = table.find_previous(string=pattern)
        if node:
            text = clean(node)
            if len(text) < 20:                               # "Item 2." + next text node = its title
                nxt = node.find_next(string=re.compile(r"\S"))
                if nxt is not None and nxt is not node:
                    text = clean(f"{text} {clean(nxt)}")
            parts.append(text[:110])
    return " - ".join(parts)


def statement_type(title):
    low = title.lower()
    return next((t for key, t in STATEMENT_TYPES if key in low), "other")


# ---------- main entry ----------
def extract_tables(html_bytes):
    soup = BeautifulSoup(html_bytes, "lxml")
    tables = []
    for table in soup.find_all("table"):
        if table.find("table"):                              # nested layout table
            continue
        header_rows, data_rows = parse_rows(table)
        if len(data_rows) < 2:                               # not a financial table
            continue
        title = find_title(table)
        rows = []
        for r in data_rows:
            cells = [{"column_label": column_label(header_rows, c["start"]), "display": c["text"],
                      "value": to_float(c["text"]), "concept": c["concept"]} for c in r["cells"]]
            rows.append({"row_label": r["label"], "cells": cells})
        if not title:
            title = f"Untitled table (first row: {rows[0]['row_label']})"
        columns = list(dict.fromkeys(c["column_label"] for r in rows for c in r["cells"]))
        tables.append({"table_no": len(tables) + 1, "title": title, "statement_type": statement_type(title),
                       "section": find_section(table), "scale": find_scale(table),
                       "columns": columns, "rows": rows})
    return tables


def to_markdown(table):
    cols = table["columns"]
    lines = ["| Line item | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
    for r in table["rows"]:
        by_col = {c["column_label"]: c["display"] for c in r["cells"]}
        lines.append(f"| {r['row_label']} | " + " | ".join(by_col.get(c, "") for c in cols) + " |")
    return "\n".join(lines)