"""HTTP API between the web frontend and the ADK agent.

Chat history is kept by ADK itself: every conversation is an ADK *session* stored in a database
(a local SQLite file by default, Cloud SQL in production; see CHAT_DB_URL below).
The browser only remembers a session_id.

Run from backend/ (the folder that contains agent/):
    uvicorn api:app --reload --port 8000

Endpoints
    GET    /api/health
    GET    /api/companies
    GET    /api/filings?company=meta
    POST   /api/ask                  {question, session_id?, ticker?, fiscal_period?}
    GET    /api/sessions             list of past chats
    GET    /api/sessions/{id}        every turn of one chat, with its citations
    DELETE /api/sessions/{id}
    GET    /api/table/{table_id}     one table, for "View table"
    GET    /api/pdf/{filing_id}      the PDF, for "View PDF page" (open with #page=N)
"""
import os
import re
import uuid
from pathlib import Path

from dotenv import load_dotenv

# The agent reaches Gemini through Vertex AI. Its settings live in agent/.env. `adk web` loads that file by
# itself, but `uvicorn` does not, so without this the agent asks for an API key. Load it before importing the agent.
ENV_FILE = Path(__file__).parent / "agent" / ".env"
if ENV_FILE.exists():
    load_dotenv(ENV_FILE)
else:
    print(f"WARNING: {ENV_FILE} not found. Gemini will fail unless GOOGLE_GENAI_USE_VERTEXAI, "
          "GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION are set in the environment.")

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from google.adk.apps.app import App
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.cloud import bigquery
from google.genai import types
from pydantic import BaseModel

from agent.agent import root_agent
from common import DATASET, PROJECT, bq, gcs
from evidence import SEARCH_TOOLS, collect_evidence

APP_NAME = "tenq"
USER_ID = "analyst"                      # later: the logged-in user, so each analyst has their own chats
# Where chat history lives. Locally: a SQLite file. In production: set CHAT_DB_URL to a Cloud SQL URL, e.g.
#   postgresql+asyncpg://USER:PASSWORD@/DBNAME?host=/cloudsql/PROJECT:REGION:INSTANCE
DEFAULT_DB = Path(__file__).parent / "chat_history.db"          # always next to api.py, whatever folder you start from
CHAT_DB_URL = os.getenv("CHAT_DB_URL", f"sqlite+aiosqlite:///{DEFAULT_DB.as_posix()}")
FRONTEND_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]
FILTER_MARKER = "\n\n[UI filters:"       # added to the question so the agent knows the dropdown choices



def history_location():
    """Where chats are stored, for the startup message and /api/health (never shows a password)."""
    if CHAT_DB_URL.startswith("sqlite"):
        return CHAT_DB_URL.split(":///", 1)[1]
    return "external database (set by CHAT_DB_URL)"


print(f"Chat history is stored in: {history_location()}")
service = DatabaseSessionService(CHAT_DB_URL)
runner = Runner(app=App(name=APP_NAME, root_agent=root_agent), session_service=service)

app = FastAPI(title="10-Q research copilot")
app.add_middleware(CORSMiddleware, allow_origins=FRONTEND_ORIGINS, allow_methods=["*"], allow_headers=["*"])


# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------

def table(name):
    return f"`{PROJECT}.{DATASET}.{name}`"


def run_query(sql, params=()):
    job = bq.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=list(params)))
    return [dict(row.items()) for row in job.result()]


def text_of(event):
    if event.content and event.content.parts:
        return "".join(part.text or "" for part in event.content.parts)
    return ""


def build_answer(answer, events):
    """Answer text + structured evidence read from the tool results of this turn."""
    evidence = collect_evidence(events, answer)
    # "not found": the search came back empty, or the answer itself says it could not find it
    not_found = evidence["refused"] or (evidence["searched"] and not evidence["grounded"])
    # "unverified": the answer contains figures but the agent never looked anything up, so nothing backs it
    unsourced = (not evidence["searched"]) and "list_filings" not in evidence["tools_used"] \
        and re.search(r"\d", answer) is not None
    status = "insufficient_evidence" if not_found else "unverified" if unsourced else "answered"
    result = {"status": status, "answer": answer,
              "citations": evidence["citations"], "figures": evidence["figures"],
              "calculations": evidence["calculations"], "tools_used": evidence["tools_used"]}
    if status != "answered":
        result["message"] = answer
    return result


def turns_from_events(events):
    """Group a session's stored events into question/answer turns (one per invocation)."""
    groups, order = {}, []
    for event in events:
        if event.invocation_id not in groups:
            groups[event.invocation_id] = []
            order.append(event.invocation_id)
        groups[event.invocation_id].append(event)

    turns = []
    for key in order:
        events_in_turn = groups[key]
        question = next((text_of(e) for e in events_in_turn if e.author == "user"), "")
        finals = [text_of(e) for e in events_in_turn
                  if e.author != "user" and e.is_final_response() and text_of(e)]
        turns.append({"question": question.split(FILTER_MARKER)[0],
                      **build_answer(finals[-1] if finals else "", events_in_turn)})
    return turns


async def ensure_session(session_id):
    existing = await service.get_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
    if existing is None:
        await service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)


# ---------------------------------------------------------
# Library: companies and filings
# ---------------------------------------------------------

@app.get("/api/health")
def health():
    return {"status": "ok", "history_store": history_location()}


@app.get("/api/companies")
def companies():
    return run_query(f"SELECT ticker, ANY_VALUE(company) AS company FROM {table('filings')} "
                     "GROUP BY ticker ORDER BY ticker")


@app.get("/api/filings")
def filings(request: Request, company: str = ""):
    rows = run_query(
        f"SELECT filing_id, ticker, company, form, fiscal_period, period_end FROM {table('filings')} "
        "WHERE (@c = '' OR LOWER(ticker) = LOWER(@c)) ORDER BY ticker, period_end",
        [bigquery.ScalarQueryParameter("c", "STRING", company)])
    base = str(request.base_url).rstrip("/")
    for row in rows:
        row["pdf_url"] = f"{base}/api/pdf/{row['filing_id']}"
    return rows


# ---------------------------------------------------------
# Asking questions (history is handled by the ADK session)
# ---------------------------------------------------------

class AskRequest(BaseModel):
    question: str
    session_id: str | None = None
    ticker: str = ""
    fiscal_period: str = ""


@app.post("/api/ask")
async def ask(req: AskRequest):
    question = req.question.strip()
    if not question:
        raise HTTPException(400, "The question is empty.")

    session_id = req.session_id or f"chat-{uuid.uuid4().hex[:10]}"
    await ensure_session(session_id)

    text = question
    if req.ticker or req.fiscal_period:
        text += f"{FILTER_MARKER} company={req.ticker or 'all'}; fiscal period={req.fiscal_period or 'any'}]"

    events, answer = [], ""
    try:
        message = types.Content(role="user", parts=[types.Part(text=text)])
        async for event in runner.run_async(user_id=USER_ID, session_id=session_id, new_message=message):
            events.append(event)
            if event.is_final_response() and event.content and event.content.parts:
                answer = text_of(event)
    except Exception as exc:
        if "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc):
            raise HTTPException(429, "The model is rate limited. Wait a minute and try again.")
        raise HTTPException(500, f"The agent failed: {str(exc)[:300]}")

    result = build_answer(answer, events)
    print(f"ask: status={result['status']} tools={result['tools_used']} sources={len(result['citations'])}", flush=True)
    return {"session_id": session_id, **result}


# ---------------------------------------------------------
# Chat history
# ---------------------------------------------------------

@app.get("/api/sessions")
async def list_chats():
    listing = await service.list_sessions(app_name=APP_NAME, user_id=USER_ID)
    chats = []
    for item in listing.sessions:
        full = await service.get_session(app_name=APP_NAME, user_id=USER_ID, session_id=item.id)
        first = next((text_of(e) for e in full.events if e.author == "user"), "")
        if first:                                          # skip chats that were created but never used
            chats.append({"session_id": item.id, "title": first.split(FILTER_MARKER)[0][:70],
                          "updated_at": full.last_update_time})
    return sorted(chats, key=lambda chat: chat["updated_at"], reverse=True)


@app.get("/api/sessions/{session_id}")
async def get_chat(session_id: str):
    session = await service.get_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
    if session is None:
        raise HTTPException(404, "Chat not found.")
    return {"session_id": session_id, "turns": turns_from_events(session.events)}


@app.delete("/api/sessions/{session_id}")
async def delete_chat(session_id: str):
    await service.delete_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
    return {"deleted": session_id}


# ---------------------------------------------------------
# Sources: table viewer and PDF
# ---------------------------------------------------------

@app.get("/api/table/{table_id}")
def get_table(table_id: str):
    rows = run_query(f"SELECT table_id, table_title, page, scale, markdown, citation FROM {table('tables')} "
                     "WHERE table_id = @id", [bigquery.ScalarQueryParameter("id", "STRING", table_id)])
    if not rows:
        raise HTTPException(404, "Table not found.")
    return rows[0]


@app.get("/api/pdf/{filing_id}")
def get_pdf(filing_id: str):
    rows = run_query(f"SELECT pdf_uri FROM {table('filings')} WHERE filing_id = @f",
                     [bigquery.ScalarQueryParameter("f", "STRING", filing_id)])
    if not rows or not rows[0]["pdf_uri"].startswith("gs://"):
        raise HTTPException(404, "PDF not found.")
    bucket_name, blob_name = rows[0]["pdf_uri"][5:].split("/", 1)
    content = gcs.bucket(bucket_name).blob(blob_name).download_as_bytes()
    return Response(content, media_type="application/pdf", headers={"Content-Disposition": "inline"})