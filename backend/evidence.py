"""Turn the tool calls ADK made during one question into structured data for the frontend.

The frontend needs citations as data (page, table, filing), not as text the model wrote.
So we read them from the tool results instead of parsing the model's answer.

    events = [event async for event in runner.run_async(...)]
    evidence = collect_evidence(events, answer_text)
"""
import re

SEARCH_TOOLS = {"search_tables", "search_tables_many", "search_text", "search_all", "get_table"}

# Wording the agent uses when it gives up ("I could not find ... in the provided filings").
NOT_FOUND = re.compile(
    r"\b(could not|couldn't|can't|cannot|unable to|did not|didn't) (find|locate)\b"
    r"|\bnot (found|mentioned|available|stated) in the (provided |indexed )?filings?\b", re.I)


def value_in_text(value, text):
    """True when a number such as '15,144' appears in the text as a whole number."""
    return re.search(r"(?<![\d.,])" + re.escape(value) + r"(?![\d,]|\.\d)", text) is not None


def collect_evidence(events, answer_text=""):
    citations, figures, calculations, tools_used = {}, [], [], []

    def add_table_rows(rows):
        for row in (rows or {}).get("results", []):
            citations[row["citation"]] = {"citation": row["citation"], "filing_id": row.get("filing_id"),
                                          "table_id": row.get("table_id"), "page": row.get("page")}
            for value in row.get("values", []):
                figures.append({"item": row["row_label"], "column": value["column"], "value": value["shown_as"],
                                "units": row.get("scale"), "citation": row["citation"]})

    def add_text_rows(rows):
        for row in (rows or {}).get("results", []):
            citations[row["citation"]] = {"citation": row["citation"], "filing_id": row.get("filing_id"),
                                          "page": row.get("page_start")}

    for event in events:
        for call in event.get_function_calls():
            tools_used.append(call.name)

        for response in event.get_function_responses():
            data = response.response or {}

            if response.name == "search_tables":
                add_table_rows(data)
            elif response.name == "search_tables_many":          # {ticker: {"results": [...]}}
                for group in data.values():
                    add_table_rows(group)
            elif response.name == "search_text":
                add_text_rows(data)
            elif response.name == "search_all":                  # {"tables": {...}, "passages": {...}}
                add_table_rows(data.get("tables"))
                add_text_rows(data.get("passages"))
            elif response.name == "get_table" and data.get("citation"):
                citations.setdefault(data["citation"], {"citation": data["citation"], "table_id": data.get("table_id")})
            elif response.name == "calculate":
                calculations.append(data)

    cited = [c for text, c in citations.items() if text in answer_text]
    used_figures = pick_used_figures(figures, {c["citation"] for c in (cited or citations.values())}, answer_text)

    # A refusal ("I could not find ...") must not show sources: nothing it retrieved supports an answer.
    refused = bool(NOT_FOUND.search(answer_text)) and not calculations \
        and not any(value_in_text(f["value"], answer_text) for f in used_figures)
    if refused:
        shown, used_figures, calculations = [], [], []
    else:
        shown = cited or list(citations.values())      # if the answer forgot to list sources, show what was used

    return {
        "citations": shown,
        "figures": used_figures,
        "calculations": calculations,
        "tools_used": tools_used,
        "searched": any(t in SEARCH_TOOLS for t in tools_used),   # did the agent look anything up?
        "grounded": bool(citations),                              # did any lookup return a source?
        "refused": refused,                                       # the answer says it could not find it
    }


def pick_used_figures(figures, cited_texts, answer_text):
    """A search returns many rows. Keep the figures that belong to a cited source and whose value
    appears in the answer; if none match (the answer rounded the numbers), keep rows whose label
    appears in the answer; if still none, keep the first few from the cited sources."""
    seen, unique = set(), []
    for f in figures:
        key = (f["citation"], f["item"], f["column"], f["value"])
        if f["citation"] in cited_texts and key not in seen:
            seen.add(key)
            unique.append(f)

    by_value = [f for f in unique if value_in_text(f["value"], answer_text)]
    if by_value:
        return by_value
    by_label = [f for f in unique if f["item"].split(": ")[-1].lower() in answer_text.lower()]
    return by_label or unique[:8]