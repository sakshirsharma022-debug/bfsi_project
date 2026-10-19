"""Config + small helpers shared by all ingestion scripts."""
from google.cloud import bigquery, storage
import vertexai
from vertexai.language_models import TextEmbeddingModel, TextEmbeddingInput
import time 

PROJECT = "rare-bastion-503107-q3"
LOCATION = "us-central1"
BUCKET = "bfsi_project_data"
RAW_PREFIX = "raw"          # raw/{TICKER}/{name}.pdf   and   raw/{TICKER}/html/{name}.htm
DATASET = "tenq"
EMBED_MODEL = "text-embedding-005"  


vertexai.init(project=PROJECT, location=LOCATION)
_embedder = TextEmbeddingModel.from_pretrained(EMBED_MODEL)
bq = bigquery.Client(project=PROJECT)
gcs = storage.Client(project=PROJECT)


def embed(texts, batch=8):
    """List of strings -> list of vectors. Waits and retries when Vertex says 429."""
    vectors = []
    for i in range(0, len(texts), batch):
        inputs = [TextEmbeddingInput(t[:8000], "RETRIEVAL_DOCUMENT") for t in texts[i:i + batch]]
        for attempt in range(6):
            try:
                vectors += [e.values for e in _embedder.get_embeddings(inputs)]
                break
            except Exception as exc:
                if "429" not in str(exc) and "Resource exhausted" not in str(exc):
                    raise
                wait = 20 * (attempt + 1)
                print(f"  rate limited, waiting {wait}s...")
                time.sleep(wait)
        else:
            raise RuntimeError("Still rate limited after 6 retries")
        time.sleep(1)                      # small pause between batches
    return vectors

def embed_query(text):
    """Embed a user question (queries use a different task type than documents)."""
    return _embedder.get_embeddings([TextEmbeddingInput(text, "RETRIEVAL_QUERY")])[0].values


def save_rows(table, filing_id, rows):
    """Replace all rows of one filing in a BigQuery table (safe to re-run)."""
    bq.create_dataset(DATASET, exists_ok=True)
    full_name = f"{PROJECT}.{DATASET}.{table}"
    try:
        bq.query(f"DELETE FROM `{full_name}` WHERE filing_id = '{filing_id}'").result()
    except Exception:
        pass                                              # table does not exist yet
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