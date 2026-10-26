"""
STEP 1 - one row per report in BigQuery.filings.

- Uses the HTML found beside each PDF in GCS.
- If the HTML is missing, downloads the matching 10-Q from SEC EDGAR
  and saves it beside the PDF.
- filing_id comes from list_files(), e.g. "meta_FY2025_Q1_10Q".

Before running (only needed when HTML must be downloaded), in PowerShell:
    $env:SEC_USER_AGENT = "Your Name your.email@example.com"

Run: python ingest_filings.py
"""

import gzip
import json
import os
import re
import time
import zlib
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from google.cloud import storage

from common import BUCKET, list_files, save_rows


# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------


# Safe production fallback if environment variable is not provided
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "FinancialResearchApp admin@financialresearch.internal")

if not os.getenv("SEC_USER_AGENT"):
    print("Warning: SEC_USER_AGENT environment variable not set. Using default fallback.")
REQUEST_DELAY_SECONDS = 0.15

SEC_TICKER_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_ARCHIVES_URL = (
    "https://www.sec.gov/Archives/edgar/data/"
    "{cik}/{accession}/{document}"
)

# Your GCS folder names are company names, not always SEC tickers.
COMPANY_TICKER_ALIASES = {
    "alphabet": "GOOGL",
    "google": "GOOGL",
    "amazon": "AMZN",
    "meta": "META",
    "nvidia": "NVDA",
}


# ---------------------------------------------------------
# SEC HTTP helper
# ---------------------------------------------------------

def sec_get(url):
    """
    Download an SEC response and decompress gzip/deflate data
    before decoding or parsing it.
    """

    if not SEC_USER_AGENT:
        raise RuntimeError(
            "SEC_USER_AGENT is missing. Set it in PowerShell first."
        )

    request = Request(
        url,
        headers={
            "User-Agent": SEC_USER_AGENT,
            "Accept": "application/json, text/html, */*",
            "Accept-Encoding": "gzip, deflate",
        },
    )

    try:
        with urlopen(request, timeout=30) as response:
            content = response.read()
            encoding = (
                response.headers.get("Content-Encoding", "")
                .lower()
                .strip()
            )

        # Some responses may omit Content-Encoding even when compressed.
        if encoding == "gzip" or content.startswith(b"\x1f\x8b"):
            content = gzip.decompress(content)

        elif encoding == "deflate":
            try:
                content = zlib.decompress(content)
            except zlib.error:
                content = zlib.decompress(content, -zlib.MAX_WBITS)

        time.sleep(REQUEST_DELAY_SECONDS)
        return content

    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"SEC request failed for {url}: {exc}") from exc

    except Exception as exc:
        raise RuntimeError(
            f"Could not process SEC response from {url}: {exc}"
        ) from exc


def sec_get_json(url):
    """Download and parse an SEC JSON response."""
    content = sec_get(url)

    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"SEC returned invalid JSON for {url}: {exc}"
        ) from exc


# ---------------------------------------------------------
# Filename and HTML metadata helpers
# ---------------------------------------------------------

def identify_period(filename):
    """
    Recognize filenames such as:
        FY2025_Q1_10Q.pdf
        FY2026_Q3_10Q.pdf

    Returns:
        ("quarter", "1", "2025")
    """

    name = os.path.basename(filename)

    match = re.search(r"FY(\d{4})_Q([1-4])(?:_10Q)?", name, re.IGNORECASE)

    if not match:
        return None

    year, quarter = match.groups()
    return ("quarter", quarter, year)


def clean_html_text(value):
    """Remove HTML tags and decode common HTML entities."""
    import html as html_module

    value = re.sub(r"<[^>]+>", " ", value)
    value = html_module.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def dei(html_text, name):
    """
    Extract an Inline XBRL DEI value, for example:
        dei:EntityRegistrantName
        dei:DocumentType
        dei:DocumentPeriodEndDate
        dei:DocumentFiscalPeriodFocus
        dei:DocumentFiscalYearFocus
    """

    pattern = (
        r"<ix:nonNumeric\b"
        r"(?=[^>]*\bname\s*=\s*['\"]dei:"
        + re.escape(name)
        + r"['\"])[^>]*>"
        r"(.*?)"
        r"</ix:nonNumeric\s*>"
    )

    match = re.search(pattern, html_text, flags=re.IGNORECASE | re.DOTALL)

    if not match:
        return None

    value = clean_html_text(match.group(1))
    return value or None


# ---------------------------------------------------------
# SEC company and filing lookup
# ---------------------------------------------------------

def normalize_sec_ticker(ticker):
    """Convert a GCS company-folder name to an SEC ticker."""
    ticker = ticker.strip()
    return COMPANY_TICKER_ALIASES.get(ticker.lower(), ticker.upper())


def get_company_cik(ticker):
    """Look up a company's CIK using the SEC ticker mapping."""

    requested_ticker = normalize_sec_ticker(ticker)
    companies = sec_get_json(SEC_TICKER_URL)

    for company in companies.values():
        sec_ticker = str(company.get("ticker", "")).upper()

        if sec_ticker == requested_ticker:
            return (
                str(company["cik_str"]).zfill(10),
                company.get("title", requested_ticker),
            )

    raise ValueError(
        f"Could not find SEC CIK for ticker "
        f"{requested_ticker!r} (input folder: {ticker!r})."
    )


def get_company_submissions(cik):
    """Fetch a company's SEC submissions history."""

    url = SEC_SUBMISSIONS_URL.format(cik=cik)
    data = sec_get_json(url)

    filings = data.get("filings", {})
    recent = filings.get("recent", {})

    records = []
    count = len(recent.get("accessionNumber", []))

    for i in range(count):
        records.append({
            "form": recent.get("form", [""] * count)[i],
            "filingDate": recent.get("filingDate", [""] * count)[i],
            "reportDate": recent.get("reportDate", [""] * count)[i],
            "accessionNumber": recent.get("accessionNumber", [""] * count)[i],
            "primaryDocument": recent.get("primaryDocument", [""] * count)[i],
        })

    # SEC keeps older filings in supplemental JSON files.
    for old_file in filings.get("files", []):
        old_name = old_file.get("name")

        if not old_name:
            continue

        old_url = f"https://data.sec.gov/submissions/{old_name}"

        try:
            older = sec_get_json(old_url)
        except Exception as exc:
            print(f"WARNING: Could not read {old_name}: {exc}")
            continue

        old_count = len(older.get("accessionNumber", []))

        for i in range(old_count):
            records.append({
                "form": older.get("form", [""] * old_count)[i],
                "filingDate": older.get("filingDate", [""] * old_count)[i],
                "reportDate": older.get("reportDate", [""] * old_count)[i],
                "accessionNumber": older.get("accessionNumber", [""] * old_count)[i],
                "primaryDocument": older.get("primaryDocument", [""] * old_count)[i],
            })

    return records, data.get("name", "")


def build_filing_url(cik, accession_number, document):
    """Build the URL for the primary SEC filing document."""

    accession_without_dashes = accession_number.replace("-", "")

    return SEC_ARCHIVES_URL.format(
        cik=int(cik),
        accession=accession_without_dashes,
        document=document,
    )


def find_matching_html(ticker, pdf_filename):
    """
    Find the SEC 10-Q matching the fiscal year and quarter in the PDF filename.
    The inline-XBRL DEI fields are used for matching, not just calendar dates.
    """

    target = identify_period(pdf_filename)

    if not target:
        raise ValueError(
            f"Cannot identify fiscal year/quarter from filename: {pdf_filename}"
        )

    _, target_quarter, target_year = target

    cik, company_name = get_company_cik(ticker)
    submissions, sec_company_name = get_company_submissions(cik)

    # Likely candidates first, by report year; fiscal years can differ from
    # calendar years, so allow one year either side.
    candidates = []

    for filing in submissions:
        if filing.get("form") != "10-Q":
            continue

        document = filing.get("primaryDocument")
        accession = filing.get("accessionNumber")

        if not document or not accession:
            continue

        report_date = filing.get("reportDate", "")
        filing_date = filing.get("filingDate", "")
        report_year = report_date[:4] if len(report_date) >= 4 else ""

        try:
            year_distance = abs(int(report_year) - int(target_year))
        except ValueError:
            year_distance = 99

        candidates.append((year_distance, filing_date, filing))

    candidates.sort(key=lambda item: (item[0], item[1]))

    if not candidates:
        raise LookupError(f"No SEC 10-Q filings found for {ticker}.")

    candidates = [item for item in candidates if item[0] <= 1][:12]

    for _, _, filing in candidates:
        url = build_filing_url(
            cik,
            filing["accessionNumber"],
            filing["primaryDocument"],
        )

        try:
            html_bytes = sec_get(url)
            html_text = html_bytes.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"WARNING: Could not fetch {url}: {exc}")
            continue

        form = dei(html_text, "DocumentType")
        fiscal_period = dei(html_text, "DocumentFiscalPeriodFocus")
        fiscal_year = dei(html_text, "DocumentFiscalYearFocus")

        if not form or not fiscal_period or not fiscal_year:
            continue

        # Accept only a 10-Q for the exact requested fiscal period.
        if form.strip().upper() != "10-Q":
            continue

        if fiscal_period.strip().upper() != f"Q{target_quarter}":
            continue

        try:
            year_matches = (
                int(re.search(r"\d{4}", fiscal_year).group()) == int(target_year)
            )
        except (AttributeError, ValueError):
            year_matches = False

        if not year_matches:
            continue

        print(f"Matched SEC filing: {ticker} FY{target_year} Q{target_quarter}")
        print(f"SEC URL: {url}")

        return html_bytes, url, company_name or sec_company_name

    raise LookupError(
        f"No exact SEC 10-Q match found for {ticker} "
        f"FY{target_year} Q{target_quarter}. "
        f"Check the fiscal-year label in {pdf_filename} "
        f"and the SEC DEI fields."
    )


# ---------------------------------------------------------
# Google Cloud Storage
# ---------------------------------------------------------

def save_html_to_gcs(pdf_blob, html_bytes):
    """
    Save the SEC HTML beside its PDF in the same GCS folder.

    Example:
        raw/alphabet/FY2025_Q1_10Q.pdf
        raw/alphabet/FY2025_Q1_10Q.html
    """

    html_name = re.sub(r"\.pdf$", ".html", pdf_blob.name, flags=re.IGNORECASE)

    if html_name == pdf_blob.name:
        raise ValueError(f"Expected a PDF filename, got {pdf_blob.name}")

    bucket = pdf_blob.bucket or storage.Client().bucket(BUCKET)
    html_blob = bucket.blob(html_name)

    html_blob.upload_from_string(
        html_bytes,
        content_type="text/html; charset=utf-8",
    )

    print(f"Uploaded HTML: gs://{bucket.name}/{html_name}")
    return html_blob


# ---------------------------------------------------------
# BigQuery ingestion
# ---------------------------------------------------------

def build_filing_row(
    filing_id,
    ticker,
    pdf_blob,
    html_blob,
    company_name,
):
    """Extract DEI fields and build the BigQuery row."""

    html_bytes = html_blob.download_as_bytes()
    html_text = html_bytes.decode("utf-8", errors="replace")

    filename = os.path.basename(pdf_blob.name)
    period = identify_period(filename)

    fiscal_period = None
    if period:
        _, quarter, year = period
        fiscal_period = f"FY{year} Q{quarter}"

    # Prefer the official company name from the filing.
    company = (
        dei(html_text, "EntityRegistrantName")
        or company_name
        or ticker
    )

    form = dei(html_text, "DocumentType") or "10-Q"
    period_end = dei(html_text, "DocumentPeriodEndDate")

    return {
        "filing_id": filing_id,        # from list_files(), e.g. meta_FY2025_Q1_10Q
        "ticker": ticker,
        "company": company,
        "form": form,
        "fiscal_period": fiscal_period,
        "period_end": period_end,
        "pdf_uri": f"gs://{pdf_blob.bucket.name}/{pdf_blob.name}",
        "html_uri": f"gs://{html_blob.bucket.name}/{html_blob.name}",
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------
# Main processing loop
# ---------------------------------------------------------

def main():
    processed = 0
    failed = 0

    for ticker, filing_id, pdf_blob, html_blob in list_files():
        filename = os.path.basename(pdf_blob.name)

        print(f"\nProcessing: {ticker} | {pdf_blob.name}")

        try:
            # Confirm the PDF filename contains a supported period.
            if not identify_period(filename):
                raise ValueError(f"Unsupported PDF filename format: {filename}")

            company_name = None

            # Reuse an existing HTML file when list_files() finds one.
            if html_blob:
                print(
                    f"Using existing HTML: "
                    f"gs://{html_blob.bucket.name}/{html_blob.name}"
                )
            else:
                html_bytes, source_url, company_name = find_matching_html(
                    ticker, filename
                )
                html_blob = save_html_to_gcs(pdf_blob, html_bytes)

            row = build_filing_row(
                filing_id=filing_id,
                ticker=ticker,
                pdf_blob=pdf_blob,
                html_blob=html_blob,
                company_name=company_name,
            )

            save_rows("filings", filing_id, [row])

            print(
                f"SUCCESS: {filing_id} "
                f"-> {row['fiscal_period']} "
                f"| period end: {row['period_end']}"
            )

            processed += 1

        except Exception as exc:
            print(f"FAILED: {filing_id} | {exc}")
            failed += 1

    print("\n--------------------------------")
    print("Ingestion finished.")
    print(f"Successful: {processed}")
    print(f"Failed:     {failed}")
    print("--------------------------------")


if __name__ == "__main__":
    main()