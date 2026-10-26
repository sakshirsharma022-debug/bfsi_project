
"""
STEP 2 - PDF text -> chunks -> embeddings -> BigQuery.text_chunks

- Extracts PDF text with page and section information.
- Skips blocks that appear to be mostly numeric tables.
- Splits text into overlapping chunks.
- Generates embeddings in small batches to avoid token limits.
- Saves chunks and embeddings to BigQuery.

Run: python ingest_text.py
"""

import re
import pymupdf

from common import (
    list_files,
    load_filings,
    embed,
    save_rows,
    make_citation,
)


CHUNK_WORDS = 400
OVERLAP_WORDS = 50

# Process only 8 chunks per embedding request.
EMBED_BATCH_SIZE = 10

HEADING = re.compile(
    r"^\s*(PART\s+[IV]+\b|Item\s+\d+[A]?\.)",
    re.IGNORECASE,
)


def looks_like_table(text):
    """Return True when a text block appears mostly numeric."""
    chars = [c for c in text if not c.isspace()]

    if not chars:
        return True

    numeric = sum(
        c.isdigit() or c in "$%(),.-—"
        for c in chars
    )

    return numeric / len(chars) > 0.55


def read_pdf(pdf_bytes):
    """
    Extract words from a PDF.

    Returns a list of:
        (word, page_number, section)
    """

    words = []
    section = "Front matter"

    with pymupdf.open(
        stream=pdf_bytes,
        filetype="pdf",
    ) as document:

        for page_no, page in enumerate(document, start=1):
            for block in page.get_text("blocks"):
                text = block[4].strip()

                if not text or looks_like_table(text):
                    continue

                first_line = text.split("\n")[0]

                if (
                    HEADING.match(first_line)
                    and len(first_line) < 120
                ):
                    section = re.sub(
                        r"\s+",
                        " ",
                        first_line,
                    ).strip()

                words.extend(
                    (word, page_no, section)
                    for word in text.split()
                )

    return words


def make_chunks(words):
    """Create overlapping text chunks with page and section metadata."""

    chunks = []
    start = 0
    step = CHUNK_WORDS - OVERLAP_WORDS

    while start < len(words):
        part = words[start:start + CHUNK_WORDS]

        if not part:
            break

        chunks.append({
            "text": " ".join(item[0] for item in part),
            "page_start": part[0][1],
            "page_end": part[-1][1],
            "section": part[0][2],
        })

        start += step

    return chunks


def embed_text_in_batches(texts, batch_size=EMBED_BATCH_SIZE):
    """
    Generate embeddings in smaller requests.

    Results remain in the same order as the input texts.
    """

    all_vectors = []

    for start in range(0, len(texts), batch_size):
        batch = texts[start:start + batch_size]

        print(
            f"Generating embeddings for chunks "
            f"{start + 1}-{start + len(batch)} "
            f"of {len(texts)}"
        )

        batch_vectors = embed(batch)

        if len(batch_vectors) != len(batch):
            raise RuntimeError(
                f"Expected {len(batch)} embeddings, "
                f"but received {len(batch_vectors)}."
            )

        all_vectors.extend(batch_vectors)

    return all_vectors


def main():
    filings = load_filings()

    successful = 0
    failed = 0

    for ticker, filing_id, pdf, _ in list_files():
        print(f"\nProcessing: {ticker} | {pdf.name}")

        try:
            if filing_id not in filings:
                raise KeyError(
                    f"Filing {filing_id!r} not found in "
                    "load_filings(). Run Step 1 first."
                )

            filing = filings[filing_id]

            # 1. Extract PDF text.
            words = read_pdf(pdf.download_as_bytes())

            if not words:
                raise ValueError(
                    "No usable text was extracted from the PDF. "
                    "The PDF may be scanned or mostly tables."
                )

            # 2. Create chunks.
            chunks = make_chunks(words)

            if not chunks:
                raise ValueError("No text chunks were created.")

            print(
                f"Extracted {len(words)} words; "
                f"created {len(chunks)} chunks."
            )

            # 3. Add company and filing context to each chunk.
            texts_to_embed = [
                (
                    f"{filing['company']} "
                    f"{filing['form']} "
                    f"{filing['fiscal_period']} | "
                    f"{chunk['section']}\n"
                    f"{chunk['text']}"
                )
                for chunk in chunks
            ]

            # 4. Generate embeddings in small batches.
            vectors = embed_text_in_batches(texts_to_embed)

            # 5. Prepare BigQuery rows.
            rows = []

            for i, (chunk, vector) in enumerate(
                zip(chunks, vectors)
            ):
                rows.append({
                    "chunk_id": f"{filing_id}:c{i:04d}",
                    "filing_id": filing_id,
                    "ticker": ticker,
                    **chunk,
                    "citation": make_citation(
                        filing,
                        chunk["page_start"],
                        chunk["section"],
                    ),
                    "embedding": vector,
                })

            # 6. Save the chunks and embeddings.
            save_rows("text_chunks", filing_id, rows)

            print(
                f"SUCCESS: {filing_id} "
                f"| {len(rows)} chunks saved."
            )

            successful += 1

        except Exception as exc:
            print(f"FAILED: {filing_id} | {exc}")
            failed += 1

    print("\n------------------------------")
    print("Text ingestion finished.")
    print(f"Successful: {successful}")
    print(f"Failed:     {failed}")
    print("------------------------------")


if __name__ == "__main__":
    main()
