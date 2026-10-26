
import pymupdf
from common import list_files, load_filings, embed, save_rows, make_citation
from table_parser import extract_tables, to_markdown

# Keep embedding requests small to reduce token-limit errors.
EMBED_BATCH_SIZE = 150


def embed_in_batches(texts, batch_size=EMBED_BATCH_SIZE):
    """Generate embeddings in batches while preserving input order."""
    vectors = []

    for start in range(0, len(texts), batch_size):
        batch = texts[start:start + batch_size]
        batch_vectors = embed(batch)

        if len(batch_vectors) != len(batch):
            raise ValueError(
                f"Expected {len(batch)} embeddings, got {len(batch_vectors)}"
            )

        vectors.extend(batch_vectors)

    return vectors


def find_page(pdf_pages, table):
    """PDF page whose text contains the most of this table's numbers AND row labels (0 if none).
    Row labels matter: an MD&A table can share its numbers with the income statement page."""
    numbers = {c["display"].strip("()$%") for r in table["rows"] for c in r["cells"]}
    numbers = [n for n in numbers if len(n) >= 4]                 # skip tiny, ambiguous numbers
    labels = {r["row_label"].split(": ")[-1].lower() for r in table["rows"]}   # drop group prefix
    labels = [l for l in labels if len(l) >= 8]
 
    best_page, best_score = 0, 0
    for page_no, text in enumerate(pdf_pages, start=1):           # pages are lower-case, one line each
        score = sum(n in text for n in numbers) + sum(l in text for l in labels)
        if score > best_score:
            best_page, best_score = page_no, score
    return best_page if best_score >= 2 else 0


def build_rows(filing, tables, pdf_pages):
    fid, ticker = filing["filing_id"], filing["ticker"]
    table_rows, row_rows, cell_rows = [], [], []

    for t in tables:
        table_id = f"{fid}:T{t['table_no']:03d}"
        page = find_page(pdf_pages, t)
        citation = make_citation(
            filing, page, t["section"], t["table_no"], t["title"]
        )

        base = {
            "filing_id": fid,
            "ticker": ticker,
            "table_id": table_id,
            "table_no": t["table_no"],
            "table_title": t["title"],
            "page": page,
            "section": t["section"],
            "citation": citation,
        }

        # One record for the complete financial table.
        table_rows.append({
            **base,
            "statement_type": t["statement_type"],
            "scale": t["scale"],
            "markdown": to_markdown(t),
            "text": (
                f"{t['title']} ({t['statement_type']}, in {t['scale']}), "
                f"{filing['company']} {filing['fiscal_period']}. "
                f"Columns: {'; '.join(t['columns'])}. "
                f"Rows: {', '.join(r['row_label'] for r in t['rows'][:40])}"
            ),
        })

        # One record per financial line item, plus individual cell records.
        for row_no, r in enumerate(t["rows"]):
            facts = "; ".join(
                f"{c['column_label']}: {c['display']}" for c in r["cells"]
            )
            row_id = f"{table_id}:R{row_no:03d}"

            row_rows.append({
                **base,
                "row_id": row_id,
                "row_label": r["row_label"],
                "scale": t["scale"],
                "text": (
                    f"{filing['company']} {filing['form']} "
                    f"{filing['fiscal_period']} | "
                    f"{t['title']} (in {t['scale']}) | "
                    f"{r['row_label']} -> {facts}"
                ),
            })

            for c in r["cells"]:
                cell_rows.append({
                    **base,
                    "row_id": row_id,
                    "row_label": r["row_label"],
                    "column_label": c["column_label"],
                    "display": c["display"],
                    "value": c["value"],
                    "scale": t["scale"],
                    "xbrl_concept": c["concept"],
                })

    return table_rows, row_rows, cell_rows


def main():
    filings = load_filings()

    for ticker, filing_id, pdf, html in list_files():
        if html is None:
            print(f"SKIP {filing_id}: no HTML file found")
            continue

        try:
            # Extract financial tables from SEC HTML.
            tables = extract_tables(html.download_as_bytes())

            # Read PDF page text only for citation page matching.
            pdf_pages = []
            if pdf is not None:
                with pymupdf.open(
                    stream=pdf.download_as_bytes(),
                    filetype="pdf",
                ) as document:
                    pdf_pages = [" ".join(page.get_text().split()).lower() for page in document]

            table_rows, row_rows, cell_rows = build_rows(
                filings[filing_id], tables, pdf_pages
            )

            # Generate table-level embeddings in batches.
            if table_rows:
                vectors = embed_in_batches([r["text"] for r in table_rows])
                for row, vec in zip(table_rows, vectors):
                    row["embedding"] = vec

            # Generate row-level embeddings in batches.
            if row_rows:
                vectors = embed_in_batches([r["text"] for r in row_rows])
                for row, vec in zip(row_rows, vectors):
                    row["embedding"] = vec

            # Save to the same BigQuery tables as the original script.
            if table_rows:
                save_rows("tables", filing_id, table_rows)
            if row_rows:
                save_rows("table_rows", filing_id, row_rows)
            if cell_rows:
                save_rows("table_cells", filing_id, cell_rows)

            no_page = sum(1 for t in table_rows if t["page"] == 0)
            print(
                f"{filing_id}: {len(table_rows)} tables "
                f"({no_page} without page), {len(row_rows)} rows, "
                f"{len(cell_rows)} cells"
            )

        except Exception as error:
            # Report the problem without stopping the remaining filings.
            print(f"FAILED {filing_id}: {error}")


if __name__ == "__main__":
    main()
