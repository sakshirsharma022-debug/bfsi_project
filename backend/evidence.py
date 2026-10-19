"""Turn the tool calls ADK made during one question into structured data for the frontend.

The frontend needs citations as data (page, table, filing), not as text the model wrote.
So we read them from the tool results instead of parsing the model's answer.

    events = [event async for event in runner.run_async(...)]
    evidence = collect_evidence(events, answer_text)
"""


SEARCH_TOOLS = {"search_tables", "search_tables_many", "search_text", "get_table"}


def collect_evidence(events, answer_text=""):
    citations, figures, calculations, tools_used = {}, [], [], []

    for event in events:
        for call in event.get_function_calls():
            tools_used.append(call.name)

        for response in event.get_function_responses():
            data = response.response or {}

            if response.name in ("search_tables", "search_tables_many"):
                # search_tables_many returns {ticker: {"results": [...]}}, search_tables returns {"results": [...]}
                groups = list(data.values()) if response.name == "search_tables_many" else [data]
                for group in groups:
                    for row in (group or {}).get("results", []):
                        citations[row["citation"]] = {
                            "citation": row["citation"],
                            "filing_id": row.get("filing_id"),
                            "table_id": row.get("table_id"),
                            "page": row.get("page"),
                        }
                        for value in row.get("values", []):
                            figures.append({
                                "item": row["row_label"],
                                "column": value["column"],
                                "value": value["shown_as"],
                                "units": row.get("scale"),
                                "citation": row["citation"],
                            })

            elif response.name == "search_text":
                for row in data.get("results", []):
                    citations[row["citation"]] = {"citation": row["citation"], "page": None}

            elif response.name == "get_table" and data.get("citation"):
                citations.setdefault(data["citation"], {"citation": data["citation"],
                                                        "table_id": data.get("table_id")})

            elif response.name == "calculate":
                calculations.append(data)

    # Show only the sources the answer actually lists. Fall back to everything retrieved.
    cited = [c for text, c in citations.items() if text in answer_text]

    searched = any(t in SEARCH_TOOLS for t in tools_used)

    return {
        "citations": cited or list(citations.values()),
        "figures": figures,
        "calculations": calculations,
        "tools_used": tools_used,
        "searched": searched,               # did the agent look anything up?
        "grounded": bool(citations),        # did any lookup return a source?
    }