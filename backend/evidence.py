"""Turn the tool calls ADK made during one question into structured data for the frontend.

The frontend needs citations as data (page, table, filing), not as text the model wrote.
So we read them from the tool results instead of parsing the model's answer.

    events = [event async for event in runner.run_async(...)]
    evidence = collect_evidence(events, answer_text)
"""
import json
import re

SEARCH_TOOLS = {"search_tables", "search_tables_many", "search_text", "search_all", "get_table"}

# Wording the agent uses when it gives up ("I could not find ... in the provided filings").
NOT_FOUND = re.compile(
    r"\b(could not|couldn't|can't|cannot|unable to|did not|didn't) (find|locate)\b"
    r"|\bnot (found|mentioned|available|stated) in the (provided |indexed )?filings?\b", re.I)


def value_in_text(value, text):
    """True when a number such as '15,144' appears in the text as a whole number.

    Empty values and one-digit values ('5', '—') never count: they would match almost anything.
    """
    core = str(value if value is not None else "").strip().strip("()$% ").strip()
    if len(re.sub(r"\D", "", core)) < 2:
        return False
    return re.search(r"(?<![\d.,])" + re.escape(core) + r"(?!\d|,\d|\.\d)", text or "") is not None


# ------------------------------------------------------------------
# Number grounding: every financial number in the answer should come
# from something the tools actually returned.
# ------------------------------------------------------------------

# 15,144 | 1,234.5 | 3.75 | 12%   (plain integers like "3 months" or "Q3" are ignored)
FIN_NUM = re.compile(r"\$?\d{1,3}(?:,\d{3})+(?:\.\d+)?|\$?\d+\.\d+|\d+(?:\.\d+)?%")
LOOSE_NUM = re.compile(r"\d[\d,]*\.?\d*")

# An answer is marked "unverified" when at least this share of its numbers is not backed by any tool result.
# Raise it (e.g. 0.75) to be more lenient, lower it to be stricter.
UNVERIFIED_RATIO = 0.5


def _to_float(value):
    if value is None:
        return None
    s = str(value).replace(",", "").replace("$", "").replace("%", "").strip().strip("()").strip()
    try:
        return float(s)
    except ValueError:
        return None


def check_numbers(answer_text, figures, raw_numbers, evidence_texts, calculations):
    """Return (unsupported_numbers, total_numbers_checked).

    A number is supported when it matches (within rounding) a figure, a raw table value,
    a number inside retrieved passages/tables, or a calculate() result. Millions/billions
    rounding ("$143.3 billion" vs 143,313) is allowed.
    """
    known = set()

    def add(v):
        if v is not None:
            v = abs(v)
            known.update({v, v / 1e3, v / 1e6, v * 1e3})

    for f in figures:
        add(_to_float(f.get("value")))
    for raw in raw_numbers:
        add(_to_float(raw))
    for text in evidence_texts:
        for m in LOOSE_NUM.findall(text or ""):
            add(_to_float(m))
    for c in calculations:                      # only the result: the formula holds constants like "* 100"
        if isinstance(c, dict):
            add(_to_float(c.get("result")))

    bad, total = [], 0
    for m in FIN_NUM.findall(answer_text or ""):
        v = _to_float(m)
        if v is None:
            continue
        v = abs(v)
        total += 1
        if not any(abs(v - k) <= max(0.06, k * 0.005) for k in known):
            bad.append(m)
    return list(dict.fromkeys(bad)), total


# ------------------------------------------------------------------
# Main entry point
# ------------------------------------------------------------------

def collect_evidence(events, answer_text=""):
    citations, figures, calculations, tools_used = {}, [], [], []
    snippets = {}          # citation -> passage text (used only for ranking, never sent to the UI)
    raw_numbers = []       # numeric cell values returned by table searches
    evidence_texts = []    # passage text / table markdown returned by tools

    def add_table_rows(rows):
        if not isinstance(rows, dict):
            return
        for row in rows.get("results") or []:
            if not isinstance(row, dict) or not row.get("citation"):
                continue
            citation = row["citation"]
            citations[citation] = {"citation": citation, "filing_id": row.get("filing_id"),
                                   "table_id": row.get("table_id"), "page": row.get("page")}
            for value in row.get("values") or []:
                if not isinstance(value, dict):
                    continue
                figures.append({"item": row.get("row_label") or "", "column": value.get("column") or "",
                                "value": value.get("shown_as") or "", "units": row.get("scale"),
                                "citation": citation})
                raw_numbers.append(value.get("value"))

    def add_text_rows(rows):
        if not isinstance(rows, dict):
            return
        for row in rows.get("results") or []:
            if not isinstance(row, dict) or not row.get("citation"):
                continue
            citation = row["citation"]
            citations[citation] = {"citation": citation, "filing_id": row.get("filing_id"),
                                   "page": row.get("page_start")}
            text = row.get("text") or ""
            if text:
                snippets[citation] = text
                evidence_texts.append(text)

    for event in events:
        for call in event.get_function_calls():
            tools_used.append(call.name)

        for response in event.get_function_responses():
            data = response.response
            if not isinstance(data, dict):
                continue

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
                citations.setdefault(data["citation"], {"citation": data["citation"],
                                                        "filing_id": data.get("filing_id"),
                                                        "table_id": data.get("table_id"),
                                                        "page": data.get("page")})
                evidence_texts.append(data.get("markdown") or "")
            elif response.name == "calculate":
                calculations.append(data)

    # Is every number in the answer backed by something the tools returned?
    unsupported, numbers_total = check_numbers(
        answer_text, figures, raw_numbers, evidence_texts, calculations)

    # Citations come from tool results, not from whether the LLM repeated the citation text.
    # Retrieval may return many candidates, so rank them before showing them in the UI.
    shown = select_relevant_citations(citations, figures, answer_text, snippets)
    used_figures = pick_used_figures(figures, {c["citation"] for c in shown}, answer_text)

    # A refusal ("I could not find ...") must not show sources: nothing it retrieved supports an answer.
    refused = bool(NOT_FOUND.search(answer_text)) and not calculations \
        and not any(value_in_text(f["value"], answer_text) for f in used_figures)
    if refused:
        shown, used_figures, calculations = [], [], []

    # What to highlight in the PDF when the user opens each citation.
    for source in shown:
        source["highlights"] = highlight_terms(
            source, used_figures, snippets.get(source["citation"], ""), answer_text)

    return {
        "citations": shown,
        "figures": used_figures,
        "calculations": calculations,
        "tools_used": tools_used,
        "searched": any(t in SEARCH_TOOLS for t in tools_used),   # did the agent look anything up?
        "grounded": bool(citations),                              # did any lookup return a source?
        "refused": refused,                                       # the answer says it could not find it
        "unsupported_numbers": unsupported,                       # numbers in the answer no tool returned
        "mostly_unsupported": bool(numbers_total) and len(unsupported) / numbers_total >= UNVERIFIED_RATIO,
    }


# ------------------------------------------------------------------
# What to highlight inside the cited PDF page
# ------------------------------------------------------------------

MAX_HIGHLIGHTS = 8
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def best_sentences(passage, answer_text, limit=2):
    """The sentences of a passage that the answer drew on most (by shared words)."""
    answer_terms = _answer_terms(answer_text)
    scored = []
    for sentence in SENTENCE_SPLIT.split(passage or ""):
        sentence = " ".join(sentence.split())
        if not 40 <= len(sentence) <= 400:
            continue
        overlap = len(_answer_terms(sentence) & answer_terms)
        if overlap >= 3:
            scored.append((overlap, sentence))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [sentence for _, sentence in scored[:limit]]


def highlight_terms(source, used_figures, passage, answer_text):
    """Text to highlight on the cited page.

    Table sources: the numbers the answer used and their row labels
    (e.g. "Revenue", "51,242"). Text sources: the 1-2 sentences the answer
    relied on most.
    """
    terms = []
    for f in used_figures:
        if f.get("citation") != source.get("citation"):
            continue
        value = str(f.get("value") or "").strip().strip("()$% ")
        if len(re.sub(r"\D", "", value)) >= 2:
            terms.append(value)
        label = str(f.get("item") or "").split(": ")[-1].strip()
        if len(label) >= 4:
            terms.append(label)
    if passage:
        terms += best_sentences(passage, answer_text)
    return list(dict.fromkeys(terms))[:MAX_HIGHLIGHTS]


# ------------------------------------------------------------------
# Ranking which sources to show
# ------------------------------------------------------------------

MAX_DISPLAY_CITATIONS = 3
MAX_CITATIONS_HARD = 6        # comparisons across many companies may show one source per filing

STOP_WORDS = {
    "the", "and", "for", "from", "with", "that", "this", "were", "was",
    "are", "how", "what", "did", "does", "into", "over", "year", "latest",
    "quarter", "quarterly", "change", "changed", "total", "than", "their",
    "they", "its", "our", "your", "has", "have", "had", "million", "billion",
    "dollars", "percent", "percentage", "increase", "increased", "decrease",
    "decreased", "compared", "ended", "three", "months", "nine", "months",
}


def _answer_terms(text):
    """Return meaningful words from the answer for lightweight source ranking."""
    return {
        word.lower()
        for word in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", text or "")
        if word.lower() not in STOP_WORDS
    }


def _citation_score(source, source_figures, answer_text, snippet=""):
    """
    Rank a source by direct evidence first, then by semantic clues in its
    citation/row labels/passage text. This does not change retrieval; it only
    decides which retrieved sources are worth showing to the user.
    """
    citation = str(source.get("citation", ""))
    citation_lower = citation.lower()
    answer_lower = (answer_text or "").lower()
    answer_terms = _answer_terms(answer_text)

    score = 0

    # Strongest signal: a number from this source is explicitly present
    # in the final answer.
    matching_values = 0
    for figure in source_figures:
        value = str(figure.get("value", ""))
        if value and value_in_text(value, answer_text):
            matching_values += 1
            score += 100

        label = str(figure.get("item", ""))
        label_terms = _answer_terms(label)
        score += min(20, 5 * len(label_terms & answer_terms))

    # Prefer the main financial statements when they directly support
    # the same answer. This helps avoid secondary notes/MD&A tables.
    main_statement_terms = (
        "consolidated statements of income",
        "condensed consolidated statements of income",
        "consolidated statements of operations",
        "statements of income",
        "statements of operations",
    )
    if any(term in citation_lower for term in main_statement_terms):
        score += 30

    # Direct overlap between meaningful answer terms and the citation.
    citation_terms = _answer_terms(citation)
    score += min(30, 3 * len(citation_terms & answer_terms))

    # Narrative passages have no figures, so rank them by how much of
    # their text the answer actually reflects.
    if snippet:
        score += min(30, 2 * len(_answer_terms(snippet) & answer_terms))

    # If the answer itself contains a source-specific page/table clue,
    # give it a small boost.
    if citation_lower and any(term in answer_lower for term in citation_terms if len(term) > 4):
        score += 2

    return score, matching_values


def select_relevant_citations(citations, figures, answer_text, snippets=None):
    """
    Convert potentially many retrieved citations into a concise set of
    verified sources for the UI.

    Rules:
      1. Prefer sources containing numbers actually used in the answer.
      2. Prefer the primary financial statement when it contains those numbers.
      3. Deduplicate by citation text and by filing/page.
      4. Show at most three sources, or one per filing when a comparison
         across several filings is supported (up to MAX_CITATIONS_HARD).
      5. Keep at least one source when retrieval produced evidence.
    """
    if not citations:
        return []

    snippets = snippets or {}

    figures_by_citation = {}
    for figure in figures:
        citation = figure.get("citation")
        if citation:
            figures_by_citation.setdefault(citation, []).append(figure)

    ranked = []
    for citation, source in citations.items():
        score, matching_values = _citation_score(
            source,
            figures_by_citation.get(citation, []),
            answer_text,
            snippets.get(citation, ""),
        )
        ranked.append((score, matching_values, citation, source))

    # Direct numerical support first, then overall evidence score.
    ranked.sort(key=lambda item: (item[1], item[0]), reverse=True)

    supported_filings = {
        s.get("filing_id") for _, mv, _, s in ranked if mv > 0 and s.get("filing_id")
    }
    limit = min(MAX_CITATIONS_HARD, max(MAX_DISPLAY_CITATIONS, len(supported_filings)))
    best_matching_values = ranked[0][1]

    selected, chosen, seen_pages = [], set(), set()

    def try_add(citation, source):
        if citation in chosen:
            return False
        # Avoid showing multiple citations for the exact same filing/page.
        page_key = (source.get("filing_id"), source.get("page"))
        if page_key != (None, None) and page_key in seen_pages:
            return False
        selected.append(source)
        chosen.add(citation)
        if page_key != (None, None):
            seen_pages.add(page_key)
        return True

    # Pass 1: the best supported source from every filing (so comparisons show every company).
    covered = set()
    for score, matching_values, citation, source in ranked:
        filing_id = source.get("filing_id")
        if matching_values > 0 and filing_id and filing_id not in covered and len(selected) < limit:
            if try_add(citation, source):
                covered.add(filing_id)

    # Pass 2: fill the remaining slots by rank.
    for score, matching_values, citation, source in ranked:
        if len(selected) >= limit:
            break
        # Skip weak candidates once we already have direct evidence.
        if selected and matching_values == 0 and best_matching_values > 0 and score < 20:
            continue
        try_add(citation, source)

    # Safety fallback: retrieval produced sources but all were scored weakly.
    if not selected:
        selected = [ranked[0][3]]

    return selected


def pick_used_figures(figures, cited_texts, answer_text):
    """A search returns many rows. Keep the figures that belong to a cited source and whose value
    appears in the answer; if none match (the answer rounded the numbers), keep rows whose label
    appears in the answer. If still none, show no figure chips rather than unrelated numbers."""
    seen, unique = set(), []
    for f in figures:
        key = (f["citation"], f["item"], f["column"], f["value"])
        if f["citation"] in cited_texts and key not in seen:
            seen.add(key)
            unique.append(f)

    by_value = [f for f in unique if value_in_text(f["value"], answer_text)]
    if by_value:
        return by_value

    answer_lower = (answer_text or "").lower()
    by_label = []
    for f in unique:
        label = f["item"].split(": ")[-1].strip().lower()
        if label and label in answer_lower:
            by_label.append(f)
    return by_label