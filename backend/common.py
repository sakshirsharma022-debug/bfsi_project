"""Config + small helpers shared by all ingestion scripts."""
import os
import time
from concurrent.futures import ThreadPoolExecutor

from google.api_core.exceptions import NotFound
from google.cloud import bigquery, storage
import vertexai
from vertexai.language_models import TextEmbeddingModel, TextEmbeddingInput

# Defaults are your current values; set the environment variables to override them.
PROJECT = os.getenv("TENQ_PROJECT", "quiet-terminal-422106")
LOCATION = os.getenv("TENQ_LOCATION", "us-central1")
# >>> Put your NEW us-central1 bucket name here (or set the TENQ_BUCKET environment variable).
BUCKET = os.getenv("TENQ_BUCKET", "bfsi_raw_data")
RAW_PREFIX = "raw"          # raw/{TICKER}/{name}.pdf   and   raw/{TICKER}/html/{name}.htm
DATASET = "tenq"
EMBED_MODEL = "text-embedding-005"


if BUCKET.startswith("CHANGE-ME"):
    raise RuntimeError(
        "Set your new bucket name in common.py (BUCKET = ...) "
        "or in the TENQ_BUCKET environment variable."
    )

vertexai.init(project=PROJECT, location=LOCATION)
_embedder = TextEmbeddingModel.from_pretrained(EMBED_MODEL)
# All BigQuery jobs run in us-central1 (same region as the dataset, bucket and Vertex AI).
bq = bigquery.Client(project=PROJECT, location=LOCATION)
gcs = storage.Client(project=PROJECT)


def _is_rate_limit(exc):
    return "429" in str(exc) or "Resource exhausted" in str(exc)


# Vertex text-embedding-005 accepts at most 20,000 tokens per request.
# Texts are grouped by estimated size instead of a fixed count of 150.
MAX_TOKENS_PER_REQUEST = 16000      # headroom under the 20,000 limit
MAX_TEXTS_PER_REQUEST = 250         # Vertex also caps texts per request
MAX_CHARS_PER_TEXT = 8000
EMBED_PARALLEL_REQUESTS = 4         # batches sent to Vertex at the same time


def _estimate_tokens(text):
    # ~3 characters per token for filing text. If a batch still turns out too
    # large, _embed_batch splits it in half automatically.
    return len(text) // 3 + 1


def _token_batches(texts):
    """Split texts into batches that stay under the token limit (order kept)."""
    batch, tokens = [], 0
    for text in texts:
        text = text[:MAX_CHARS_PER_TEXT]
        n = _estimate_tokens(text)
        if batch and (tokens + n > MAX_TOKENS_PER_REQUEST or len(batch) >= MAX_TEXTS_PER_REQUEST):
            yield batch
            batch, tokens = [], 0
        batch.append(text)
        tokens += n
    if batch:
        yield batch


def _embed_batch(texts):
    """Embed one batch. Retries on 429; splits the batch in half if it is still too large."""
    inputs = [TextEmbeddingInput(t, "RETRIEVAL_DOCUMENT") for t in texts]
    for attempt in range(6):
        try:
            return [e.values for e in _embedder.get_embeddings(inputs)]
        except Exception as exc:
            if "token count" in str(exc).lower() and len(texts) > 1:
                half = len(texts) // 2
                return _embed_batch(texts[:half]) + _embed_batch(texts[half:])
            if not _is_rate_limit(exc):
                raise
            wait = 20 * (attempt + 1)
            print(f"  rate limited, waiting {wait}s...")
            time.sleep(wait)
    raise RuntimeError("Still rate limited after 6 retries")


def embed(texts, batch=None):
    """List of strings -> list of vectors, in the same order.

    `batch` is ignored (kept so existing callers still work): batches are sized
    by tokens so no request goes over Vertex's 20,000-token limit, and up to
    EMBED_PARALLEL_REQUESTS batches are sent at the same time. There is no
    fixed pause between batches; waits only happen if Vertex says 429.
    """
    batches = list(_token_batches(texts))
    if not batches:
        return []
    if len(batches) == 1:
        return _embed_batch(batches[0])

    with ThreadPoolExecutor(max_workers=EMBED_PARALLEL_REQUESTS) as pool:
        results = list(pool.map(_embed_batch, batches))     # map keeps the order

    return [vector for batch_vectors in results for vector in batch_vectors]


def embed_query(text):
    """Embed a user question (queries use a different task type than documents).
    Retries a few times on 429 so a busy moment does not fail the user's question."""
    inputs = [TextEmbeddingInput(text, "RETRIEVAL_QUERY")]
    for attempt in range(4):
        try:
            return _embedder.get_embeddings(inputs)[0].values
        except Exception as exc:
            if not _is_rate_limit(exc):
                raise
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("Embedding service is rate limited. Try again in a minute.")


def save_rows(table, filing_id, rows):
    """Replace all rows of one filing in a BigQuery table (safe to re-run)."""
    # Create the dataset in us-central1 (without a location BigQuery uses the US multi-region).
    dataset = bigquery.Dataset(f"{PROJECT}.{DATASET}")
    dataset.location = LOCATION                           # "us-central1"
    bq.create_dataset(dataset, exists_ok=True)
    full_name = f"{PROJECT}.{DATASET}.{table}"
    try:
        bq.query(
            f"DELETE FROM `{full_name}` WHERE filing_id = @fid",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("fid", "STRING", filing_id)]
            ),
        ).result()
    except NotFound:
        pass                                              # table does not exist yet
    except Exception as exc:
        if "not found" not in str(exc).lower():           # anything else is a real failure
            raise
    if rows:
        job = bq.load_table_from_json(rows, full_name, job_config=bigquery.LoadJobConfig(
            write_disposition="WRITE_APPEND", autodetect=True))
        job.result()


def list_files():
    """Yield (ticker, filing_id, pdf_blob, html_blob or None) for every PDF under raw/."""
    blobs = {b.name: b for b in gcs.list_blobs(BUCKET, prefix=RAW_PREFIX + "/")}
    for name, pdf in blobs.items():
        parts = name.split("/")
        if name.lower().endswith(".pdf") and len(parts) == 3:        # raw/<company>/<file>.pdf
            ticker, stem = parts[1], parts[2].rsplit(".", 1)[0]
            base = f"{RAW_PREFIX}/{ticker}/{stem}"
            html = blobs.get(base + ".html") or blobs.get(base + ".htm")
            yield ticker, f"{ticker}_{stem}", pdf, html


def load_filings():
    """filing_id -> filing metadata (from the `filings` table)."""
    rows = bq.query(f"SELECT * FROM `{PROJECT}.{DATASET}.filings`").result()
    return {r["filing_id"]: dict(r.items()) for r in rows}


def make_citation(filing, page, section, table_no=None, table_title=None):
    """One readable string the UI can show next to every answer."""
    parts = [f"{filing['ticker']} {filing['form']} {filing['fiscal_period']}",
             f"p.{page}" if page else "page n/a"]
    if section:
        parts.append(section)
    if table_no:
        parts.append(f"Table {table_no}: {table_title}")
    return " · ".join(parts)