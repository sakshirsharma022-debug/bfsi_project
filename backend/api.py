"""HTTP API between the web frontend and the ADK agent.

Chat history is kept by ADK itself: every conversation is an ADK session
stored in Firestore, under adk-session/tenq/users/<Firebase uid>/sessions/.

The browser only remembers a session_id.

Run from backend/ (the folder that contains agent/):
    uvicorn api:app --reload --port 8000
Endpoints
    GET    /api/health
    GET    /api/companies
    GET    /api/filings?company=meta
    POST   /api/ask
    POST   /api/ask/stream
    POST   /api/ingest
    GET    /api/ingest/status/{id}
    GET    /api/sessions
    GET    /api/sessions/{id}
    DELETE /api/sessions/{id}
    GET    /api/table/{table_id}
    GET    /api/pdf/{filing_id}
    POST   /api/sessions/{id}/report
    GET    /api/reports/{filename}
"""

# ============================================================
# Imports and environment configuration
# ============================================================

import os
import re
import uuid
import time
import json
import logging
import asyncio
import tempfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Ensure SEC User-Agent has a secure production fallback
if not os.getenv("SEC_USER_AGENT"):
    os.environ["SEC_USER_AGENT"] = (
        "FinancialResearchApp admin@financialresearch.internal"
    )
    logger.warning(
        "SEC_USER_AGENT environment variable not set. "
        "Using default fallback configuration."
    )

# The agent reaches Gemini through Vertex AI.
# Its settings live in agent/.env.
#
# `adk web` loads this file automatically, but uvicorn does not.
# Therefore, load it before importing the agent.
ENV_FILE = Path(__file__).parent / "agent" / ".env"

if ENV_FILE.exists():
    load_dotenv(ENV_FILE)
else:
    print(
        f"WARNING: {ENV_FILE} not found. Gemini will fail unless "
        "GOOGLE_GENAI_USE_VERTEXAI, GOOGLE_CLOUD_PROJECT and "
        "GOOGLE_CLOUD_LOCATION are set in the environment."
    )

# ============================================================
# Third-party imports
# ============================================================

import pymupdf

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
import firebase_admin
from firebase_admin import auth as firebase_auth
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse

from google.adk.apps.app import App
from google.adk.runners import Runner
from google.adk.agents.run_config import RunConfig
from google.adk.agents.invocation_context import LlmCallsLimitExceededError
from google.adk.errors.session_not_found_error import SessionNotFoundError
from google.adk.sessions import InMemorySessionService
from google.adk.integrations.firestore.firestore_session_service import FirestoreSessionService
from google.cloud import firestore
from google.adk.sessions import _session_util
from datetime import datetime, timezone

from google.cloud import bigquery
from google.genai import types
from pydantic import BaseModel

from agent.agent import root_agent, suggestion_agent, report_agent

from common import (
    BUCKET,
    DATASET,
    PROJECT,
    bq,
    embed,
    gcs,
    load_filings,
    make_citation,
    save_rows,
)

from evidence import SEARCH_TOOLS, collect_evidence

from ingest_filings import (
    build_filing_row,
    build_filing_url,
    dei,
    get_company_cik,
    get_company_submissions,
    sec_get,
)

from ingest_tables import (
    build_rows,
    embed_in_batches,
    extract_tables,
    find_page,
)

from ingest_text import (
    make_chunks,
    read_pdf,
    embed_text_in_batches,
)

# ============================================================
# Application configuration
# ============================================================

APP_NAME = "tenq"

# Only used for the temporary suggestion/report sessions.
# Real chats are saved under the signed-in user's Firebase uid.
USER_ID = "analyst"

firebase_admin.initialize_app(options={"projectId": PROJECT})


def verify_user(request: Request) -> dict:
    """Verify the Firebase token sent by the frontend and return its claims."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(401, "Sign in to continue.")
    try:
        return firebase_auth.verify_id_token(header[7:])
    except Exception:
        raise HTTPException(401, "Your session has expired. Sign in again.")


def get_user_id(request: Request) -> str:
    """The signed-in user's Firebase uid."""
    return verify_user(request)["uid"]


# Who may add new filings (each ingestion costs Vertex AI / BigQuery money).
# Comma-separated emails in the INGEST_ADMIN_EMAILS environment variable.
# If it is empty, every signed-in user may ingest (handy for local development).
INGEST_ADMIN_EMAILS = {
    email.strip().lower()
    for email in os.getenv("INGEST_ADMIN_EMAILS", "").split(",")
    if email.strip()
}


def can_ingest(claims: dict) -> bool:
    return not INGEST_ADMIN_EMAILS or (claims.get("email") or "").lower() in INGEST_ADMIN_EMAILS


def get_admin_user_id(request: Request) -> str:
    """Like get_user_id, but only for users allowed to add filings."""
    claims = verify_user(request)
    if not can_ingest(claims):
        raise HTTPException(403, "Only administrators can add new filings.")
    return claims["uid"]

# Chat history lives in Firestore in the same Google Cloud project.
# FIRESTORE_DATABASE only needs setting if you do not use the "(default)" database.
FIRESTORE_DATABASE = os.getenv("FIRESTORE_DATABASE", "(default)")

# Browser origins allowed to call this API. Override with the FRONTEND_ORIGINS
# environment variable (comma-separated) if the frontend address changes.
FRONTEND_ORIGINS = [
    origin.strip().rstrip("/")
    for origin in os.getenv(
        "FRONTEND_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173,http://localhost:5174,"
        "https://quiet-terminal-422106.web.app,https://quiet-terminal-422106.firebaseapp.com",
    ).split(",")
    if origin.strip()
]

# Fair-use limit: questions per user per minute (protects cost and responsiveness).
ASK_LIMIT_PER_MINUTE = int(os.getenv("ASK_LIMIT_PER_MINUTE", "10"))
_ask_history = {}


def check_ask_rate(user_id):
    now = time.time()
    recent = _ask_history.setdefault(user_id, deque())
    while recent and now - recent[0] > 60:
        recent.popleft()
    if len(recent) >= ASK_LIMIT_PER_MINUTE:
        raise HTTPException(
            429,
            "You're asking questions very quickly. Please wait a few seconds and try again.",
        )
    recent.append(now)

# Safety net for latency: a question may use at most this many Gemini calls.
# A normal answer needs 2-5. If the agent is still searching after this many,
# it is stopped and the user is asked to narrow the question, instead of
# waiting minutes.
MAX_LLM_CALLS_PER_QUESTION = 8
RUN_CONFIG = RunConfig(max_llm_calls=MAX_LLM_CALLS_PER_QUESTION)

# This marker is appended to the question when the frontend
# sends company/period filters.
FILTER_MARKER = "\n\n[UI filters:"

# Ingestion progress is stored in Firestore (collection "ingestion_status"),
# so it survives restarts/redeploys and works with any number of instances.
INGESTION_STATUS_COLLECTION = "ingestion_status"
# A job that has not reported progress for this long is treated as interrupted.
INGESTION_STALE_SECONDS = 15 * 60

# ============================================================
# ADK services and runners
# ============================================================


def history_location():
    """Return a safe description of where chat history is stored."""
    return f"Firestore ({PROJECT}, database {FIRESTORE_DATABASE})"


print(f"Chat history is stored in: {history_location()}")

# ------------------------------------------------------------
# Main conversation service
# ------------------------------------------------------------
#
# This is the real user conversation.
# It stores the actual questions and financial answers.
# ------------------------------------------------------------

class BufferedFirestoreSessionService(FirestoreSessionService):
    """Firestore chat storage that saves a whole question in ONE write.

    ADK records every agent step (the question, each tool call, each tool
    result, the answer) with append_event. The stock Firestore service runs a
    separate transaction (about 3 network round trips) per step, and one
    question has 8-12 steps, so storage alone added many seconds per answer.

    Here the steps are kept in memory while the agent works and written to
    Firestore together in a single batch (flush_session) right after the
    answer. Reading, listing and deleting chats is unchanged.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pending = {}          

    async def append_event(self, session, event):
        if event.partial:
            return event

        self._apply_temp_state(session, event)
        event = self._trim_temp_delta_state(event)

        key = (session.app_name, session.user_id, session.id)
        _, events = self._pending.setdefault(key, (session, []))
        events.append(event)

        # Update the in-memory session the agent is using (no network).
        return self._commit_event_to_session(session, event)

    async def flush_session(self, app_name, user_id, session_id):
        """Write all buffered steps of one chat in a single Firestore batch."""
        session, events = self._pending.pop((app_name, user_id, session_id), (None, []))
        if not events:
            return

        session_ref = self._get_sessions_ref(app_name, user_id).document(session_id)
        events_ref = session_ref.collection(self.events_collection)
        batch = self.client.batch()

        for event in events:
            batch.set(events_ref.document(event.id), {
                "event_data": event.model_dump(exclude_none=True, mode="json"),
                # Distinct per step so the chat reads back in the right order.
                "timestamp": datetime.fromtimestamp(event.timestamp, tz=timezone.utc),
                "appName": app_name,
                "userId": user_id,
            })

        session_state = {
            k: v for k, v in session.state.items()
            if not k.startswith(("app:", "user:", "temp:"))
        }
        batch.update(session_ref, {
            "state": json.dumps(_session_util.make_json_safe_state(session_state)),
            "updateTime": firestore.SERVER_TIMESTAMP,
            "revision": firestore.Increment(len(events)),
        })

        await batch.commit()


service = BufferedFirestoreSessionService(
    client=firestore.AsyncClient(project=PROJECT, database=FIRESTORE_DATABASE),
)


def timing_breakdown(events, started, storage_seconds):
    """Split one answer's time into model / tools / storage (for the log)."""
    model = tools = 0.0
    previous = None
    for e in events:
        if previous is not None:
            gap = max(0.0, e.timestamp - previous)
            if e.get_function_responses():
                tools += gap
            else:
                model += gap
        previous = e.timestamp
    total = time.time() - started
    return (
        f"seconds={total:.1f} model={model:.1f} tools={tools:.1f} "
        f"storage={storage_seconds:.1f}"
    )

runner = Runner(
    app=App(
        name=APP_NAME,
        root_agent=root_agent,
    ),
    session_service=service,
)

# ------------------------------------------------------------
# Follow-up suggestion service
# ------------------------------------------------------------
#
# Suggestions should NOT become part of the user's actual
# financial conversation history.
#
# Therefore the suggestion agent gets its own temporary,
# in-memory ADK session.
# ------------------------------------------------------------

suggestion_service = InMemorySessionService()

suggestion_runner = Runner(
    app=App(
        name="tenq_follow_up_suggestions",
        root_agent=suggestion_agent,
    ),
    session_service=suggestion_service,
)

# ------------------------------------------------------------
# Wrap-up report service
# ------------------------------------------------------------
# The report agent is isolated from the real financial session.
# It receives structured, already-grounded research data and has
# no financial search tools.
# ------------------------------------------------------------
report_service = InMemorySessionService()

report_runner = Runner(
    app=App(
        name="tenq_wrap_up_report",
        root_agent=report_agent,
    ),
    session_service=report_service,
)

# ============================================================
# FastAPI application
# ============================================================

app = FastAPI(title="10-Q research copilot")

app.add_middleware(
    CORSMiddleware,
    allow_origins=FRONTEND_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# General helpers
# ============================================================


def table(name):
    """Return a fully qualified BigQuery table name."""
    return f"`{PROJECT}.{DATASET}.{name}`"


def run_query(sql, params=()):
    """Run a BigQuery query and return rows as dictionaries."""
    job = bq.query(
        sql,
        job_config=bigquery.QueryJobConfig(
            query_parameters=list(params)
        ),
    )
    return [dict(row.items()) for row in job.result()]


def text_of(event):
    """Extract text from an ADK event."""
    if event.content and event.content.parts:
        return "".join(
            part.text or ""
            for part in event.content.parts
        )
    return ""


# ============================================================
# Answer + evidence processing
# ============================================================

def build_answer(answer, events):
    """
    Build the final structured response from the financial
    agent's answer and the tool events generated during the turn.
    """
    evidence = collect_evidence(events, answer)

    # "not found":
    # The search came back empty, or the answer itself says
    # the information could not be found.
    not_found = (
        evidence["refused"]
        or (
            evidence["searched"]
            and not evidence["grounded"]
        )
    )

    # "unverified":
    # The answer contains figures but the agent never looked
    # anything up, or most of its numbers are not backed by
    # anything the tools returned.
    unsourced = (
        (
            (not evidence["searched"])
            and "list_filings" not in evidence["tools_used"]
            and re.search(r"\d", answer) is not None
        )
        or evidence.get("mostly_unsupported", False)
    )

    status = (
        "insufficient_evidence"
        if not_found
        else "unverified"
        if unsourced
        else "answered"
    )

    result = {
        "status": status,
        "answer": answer,
        "citations": evidence["citations"],
        "figures": evidence["figures"],
        "calculations": evidence["calculations"],
        "tools_used": evidence["tools_used"],
        "unsupported_numbers": evidence.get("unsupported_numbers", []),
    }

    if status != "answered":
        result["message"] = answer

    return result


# ============================================================
# Conversation history helpers
# ============================================================

def turns_from_events(events):
    """
    Group a session's stored events into question/answer turns.
    One turn corresponds to one ADK invocation.
    This function is also used by the suggestion feature to
    retrieve previous questions and answers from the existing
    conversation.
    """
    groups = {}
    order = []

    for event in events:
        if event.invocation_id not in groups:
            groups[event.invocation_id] = []
            order.append(event.invocation_id)
        groups[event.invocation_id].append(event)

    turns = []

    for key in order:
        events_in_turn = groups[key]

        question = next(
            (
                text_of(e)
                for e in events_in_turn
                if e.author == "user"
            ),
            "",
        )

        finals = [
            text_of(e)
            for e in events_in_turn
            if (
                e.author != "user"
                and e.is_final_response()
                and text_of(e)
            )
        ]

        turns.append(
            {
                "question": question.split(FILTER_MARKER)[0],
                **build_answer(
                    finals[-1] if finals else "",
                    events_in_turn,
                ),
            }
        )

    return turns


async def ensure_session(user_id, session_id, title=""):
    """
    Create the main ADK conversation session if it does not exist.

    The chat title is saved in the session state, so the sidebar can list
    chats without reading every message of every chat from Firestore.
    """
    existing = await service.get_session(
        app_name=APP_NAME,
        user_id=user_id,
        session_id=session_id,
    )

    if existing is None:
        await service.create_session(
            app_name=APP_NAME,
            user_id=user_id,
            session_id=session_id,
            state={"title": title[:70]},
        )


# ============================================================
# FOLLOW-UP QUESTION GENERATION
# ============================================================

async def generate_suggested_questions(
    user_id,
    session_id,
    current_question,
    current_answer,
    citations,
    previous_turns=None,
):
    """
    Generate 2-3 contextual follow-up questions.

    The suggestion agent receives:
        - current question
        - current answer
        - current citations
        - recent previous Q&A

    The suggestion agent does not have access to the financial
    search tools.

    If suggestion generation fails, this function returns an
    empty list so that the main financial answer is never broken.
    """
    try:
        # ----------------------------------------------------
        # 1. Retrieve the existing conversation session.
        # ----------------------------------------------------
        #
        # The main ADK session already contains the user's
        # previous questions and answers.
        # ----------------------------------------------------
        # The frontend sends the earlier questions/answers it already has,
        # so the whole chat does not have to be read from Firestore again.
        if previous_turns is None:
            session = await service.get_session(
                app_name=APP_NAME,
                user_id=user_id,
                session_id=session_id,
            )

            if session is None:
                return []

            previous_turns = turns_from_events(session.events)[:-1]

        previous_turns = previous_turns[-4:]

        # ----------------------------------------------------
        # 5. Convert previous turns into readable text.
        # ----------------------------------------------------
        history_parts = []

        for turn in previous_turns:
            previous_question = turn.get("question", "").strip()
            previous_answer = turn.get("answer", "").strip()

            if not previous_question:
                continue

            history_parts.append(
                f"User question:\n"
                f"{previous_question}\n\n"
                f"Assistant answer:\n"
                f"{previous_answer}"
            )

        previous_history = (
            "\n\n--- Previous Turn ---\n\n".join(history_parts)
            if history_parts
            else "No previous conversation turns."
        )

        # ----------------------------------------------------
        # 6. Convert current citations into a simple text block.
        # ----------------------------------------------------
        if citations:
            citations_text = "\n".join(
                f"- {citation}"
                for citation in citations
            )
        else:
            citations_text = "No citations were returned."

        # ----------------------------------------------------
        # 7. Build the suggestion-agent prompt.
        #
        #    THIS is how the suggestion agent knows about:
        #
        #       - current question
        #       - current answer
        #       - previous questions
        #       - previous answers
        #       - current evidence
        # ----------------------------------------------------
        suggestion_prompt = f"""
Generate follow-up research questions for this financial 10-Q conversation.

CURRENT USER QUESTION:
{current_question}

CURRENT FINANCIAL ASSISTANT ANSWER:
{current_answer}

CURRENT ANSWER CITATIONS:
{citations_text}

PREVIOUS CONVERSATION:
{previous_history}

Return exactly 3 questions as a JSON array of objects with these fields:
- "angle": one of drivers, profitability_or_ratio, metric_relationship,
  trend, segment_or_product, cash_flow, risk_or_disclosure,
  period_or_peer_comparison, other_metric.
- "question": the question string shown to the user.

MANDATORY DIVERSITY RULES:
- Use a different angle for each of the 3 questions.
- Do not repeat or paraphrase the current question or a question from
  the previous conversation.
- Do not make all 3 questions simple year-over-year / quarter-over-
  quarter comparisons of different line items.
- If the current question already asks how a metric changed year over
  year or quarter over quarter, do not ask the same type of change
  question for another metric. Explore causes, margins/ratios,
  relationships between metrics, cash flow, segment/product detail,
  trends, or disclosures instead, choosing only angles relevant to
  the supplied context.
- Avoid near-identical wording and do not just swap the metric name
  in an otherwise identical sentence.
- Do not invent financial values, companies, periods, or facts.
- Do not answer the questions or include citations in the questions.
- Questions should be concise and plausibly answerable using the
  indexed filings. Do not imply a detail is definitely present if the
  supplied context does not support that assumption.
Return ONLY valid JSON, with exactly 3 objects and no Markdown fences.

Example:
[
  {{"angle": "drivers", "question": "Which expense categories contributed most to the change in operating income?"}},
  {{"angle": "metric_relationship", "question": "Did revenue growth outpace operating-income growth during the quarter?"}},
  {{"angle": "cash_flow", "question": "How did operating cash flow compare with net income for the quarter?"}}
]
"""

        # ----------------------------------------------------
        # 8. Create a temporary suggestion-agent session.
        #
        #    This session is separate from the real user chat.
        # ----------------------------------------------------
        suggestion_session_id = (
            f"suggestion-{uuid.uuid4().hex[:10]}"
        )

        await suggestion_service.create_session(
            app_name="tenq_follow_up_suggestions",
            user_id=USER_ID,
            session_id=suggestion_session_id,
        )

        # ----------------------------------------------------
        # 9. Send the prompt to the suggestion agent.
        # ----------------------------------------------------
        message = types.Content(
            role="user",
            parts=[
                types.Part(
                    text=suggestion_prompt
                )
            ],
        )

        suggestion_events = []

        try:
            async for event in suggestion_runner.run_async(
                user_id=USER_ID,
                session_id=suggestion_session_id,
                new_message=message,
            ):
                suggestion_events.append(event)
        finally:
            # The temporary session is not needed any more; delete it
            # so in-memory sessions do not pile up.
            try:
                await suggestion_service.delete_session(
                    app_name="tenq_follow_up_suggestions",
                    user_id=USER_ID,
                    session_id=suggestion_session_id,
                )
            except Exception:
                pass

        # ----------------------------------------------------
        # 10. Extract the final suggestion-agent response.
        # ----------------------------------------------------
        final_texts = [
            text_of(event)
            for event in suggestion_events
            if (
                event.author != "user"
                and event.is_final_response()
                and text_of(event)
            )
        ]

        if not final_texts:
            return []

        raw_suggestions = final_texts[-1].strip()

        # ----------------------------------------------------
        # 11. Clean accidental Markdown code fences.
        #
        #    Sometimes an LLM may return:
        #
        #    ```json
        #    [...]
        #    ```
        #
        #    even when asked for JSON only.
        # ----------------------------------------------------
        if raw_suggestions.startswith("```"):
            raw_suggestions = re.sub(
                r"^```(?:json)?\s*",
                "",
                raw_suggestions,
                flags=re.IGNORECASE,
            )
            raw_suggestions = re.sub(
                r"\s*```$",
                "",
                raw_suggestions,
            ).strip()

        # ----------------------------------------------------
        # 12. Parse the JSON response.
        # ----------------------------------------------------
        suggestions = json.loads(raw_suggestions)

        # The model returns {angle, question} objects, but the frontend
        # continues to receive a simple list of strings.
        if not isinstance(suggestions, list):
            return []

        allowed_angles = {
            "drivers",
            "profitability_or_ratio",
            "metric_relationship",
            "trend",
            "segment_or_product",
            "cash_flow",
            "risk_or_disclosure",
            "period_or_peer_comparison",
            "other_metric",
        }

        def normalize_question(value):
            return " ".join(re.findall(r"[a-z0-9]+", value.lower()))

        def is_near_duplicate(candidate, others):
            candidate_key = normalize_question(candidate)
            candidate_tokens = set(candidate_key.split())

            if not candidate_tokens:
                return True

            for other in others:
                other_key = normalize_question(other)

                if not other_key:
                    continue

                if candidate_key == other_key:
                    return True

                other_tokens = set(other_key.split())
                shared = len(candidate_tokens & other_tokens)
                denominator = max(1, min(len(candidate_tokens), len(other_tokens)))

                # Catch questions that are almost the same sentence with
                # only small wording changes.
                if shared / denominator >= 0.88:
                    return True

            return False

        def is_direct_period_change_question(value):
            period_reference = re.search(
                r"\b(?:year[\s-]*over[\s-]*year|quarter[\s-]*over[\s-]*quarter|"
                r"prior[\s-]*year|previous[\s-]*year|prior[\s-]*period|"
                r"previous[\s-]*period|same quarter last year|same period last year)\b",
                value,
                flags=re.IGNORECASE,
            )

            direct_change_wording = re.search(
                r"\bhow\s+(?:did|has|have)\b.{0,140}\b(?:change|increase|decrease|compare|grow|growth)\b"
                r"|\bwhat\s+(?:was|were)\b.{0,180}\b(?:change|increase|decrease|compare|compared)\b",
                value,
                flags=re.IGNORECASE | re.DOTALL,
            )

            return bool(period_reference and direct_change_wording)

        # If the user's question already asks for a period-over-period
        # change, don't suggest another question using that same template.
        period_change_limit = 0 if is_direct_period_change_question(current_question) else 1
        period_change_count = 0

        previous_questions = [
            turn.get("question", "").strip()
            for turn in previous_turns
            if turn.get("question", "").strip()
        ]

        reference_questions = [current_question.strip(), *previous_questions]

        cleaned_suggestions = []
        seen_angles = set()
        seen_questions = []

        for item in suggestions:
            if isinstance(item, dict):
                angle = str(item.get("angle", "")).strip().lower()
                question_text = item.get("question", "")
            elif isinstance(item, str):
                # Backward compatibility in case the model returns the old
                # array-of-strings format despite the updated prompt.
                angle = ""
                question_text = item
            else:
                continue

            if not isinstance(question_text, str):
                continue

            question_text = question_text.strip()

            if not question_text:
                continue

            if angle:
                angle = re.sub(r"[^a-z0-9_]+", "_", angle)

                if angle not in allowed_angles or angle in seen_angles:
                    continue

            if is_near_duplicate(question_text, [*reference_questions, *seen_questions]):
                continue

            is_period_change = is_direct_period_change_question(question_text)

            if is_period_change and period_change_count >= period_change_limit:
                continue

            if is_period_change:
                period_change_count += 1

            if angle:
                seen_angles.add(angle)

            seen_questions.append(question_text)
            cleaned_suggestions.append(question_text)

            if len(cleaned_suggestions) == 3:
                break

        # Keep suggestions useful rather than showing repetitive ones.
        # The frontend still receives a list[str], just as before.
        return cleaned_suggestions[:3]

    except Exception as exc:
        # ----------------------------------------------------
        # IMPORTANT:
        #
        # Suggestion generation must NEVER cause the main
        # financial Q&A request to fail.
        # ----------------------------------------------------
        logger.warning(
            "Could not generate follow-up suggestions: %s",
            exc,
        )
        return []


# ============================================================
# Wrap-up report helpers
# ============================================================

# Reports are rendered to a temporary folder, then stored in Cloud Storage
# (gs://<bucket>/reports/...) so they survive restarts and work on any instance.
REPORT_DIR = Path(tempfile.gettempdir()) / "quarterly_reports"
REPORTS_PREFIX = "reports"


def build_report_research_data(session):
    """
    Build structured research data from the existing session.

    This intentionally does NOT create a chat transcript. Each item is
    a research finding containing the question, answer, figures,
    calculations, and verified citations already produced by the
    financial evidence pipeline.
    """
    turns = turns_from_events(session.events)
    findings = []

    for turn in turns:
        question = (turn.get("question") or "").strip()
        answer = (turn.get("answer") or "").strip()

        if not question or not answer:
            continue

        # Don't put failed/unverified turns into the factual report.
        if turn.get("status") != "answered":
            continue

        findings.append({
            "question": question,
            "answer": answer,
            "figures": turn.get("figures", []),
            "calculations": turn.get("calculations", []),
            "sources": turn.get("citations", []),
        })

    return {
        "session_id": session.id,
        "findings": findings,
    }


def report_prompt(report_data):
    return f"""
Create a professional financial research wrap-up from the VERIFIED
research data below.

This is a research report, NOT a conversation transcript.

STRICT RULES:
- Use only the supplied research data.
- Never invent a financial figure, percentage, company, filing, period,
  explanation, or source.
- Never change a supplied number.
- Preserve source citation text and page numbers.
- Do not include unsupported claims.
- Do not repeat the user questions as a Q&A transcript.
- Combine related findings into a coherent analyst-style report.
- Keep different reporting periods separate. Never mix three-month and
  six-month figures.
- If a section has no verified evidence, omit it.

Return ONLY valid JSON with this exact structure:
{{
  "title": "Quarterly Research Wrap-Up",
  "company": "",
  "filing": "",
  "period": "",
  "executive_summary": "",
  "key_findings": [
    {{
      "title": "",
      "summary": "",
      "figures": [],
      "sources": []
    }}
  ],
  "business_analysis": [
    {{
      "title": "",
      "summary": "",
      "sources": []
    }}
  ],
  "trends_and_insights": [],
  "questions_investigated": [],
  "verified_sources": []
}}

VERIFIED RESEARCH DATA:
{json.dumps(report_data, indent=2, ensure_ascii=False, default=str)}
"""


def _extract_json(text):
    """Parse JSON even if the model accidentally adds a code fence."""
    text = (text or "").strip()

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")

        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])

        raise


def _fallback_report(report_data):
    """
    Deterministic fallback. If the Report Agent fails, the feature still
    produces a useful report from the already verified findings.
    """
    findings = report_data.get("findings", [])
    key_findings = []
    source_map = {}
    questions = []

    for item in findings:
        questions.append(item["question"])
        sources = item.get("sources", []) or []

        for source in sources:
            key = source.get("citation", "")
            if key:
                source_map[key] = source

        key_findings.append({
            "title": item["question"],
            "summary": item["answer"],
            "figures": item.get("figures", []),
            "sources": sources,
        })

    return {
        "title": "Quarterly Research Wrap-Up",
        "company": "",
        "filing": "",
        "period": "",
        "executive_summary": (
            "This report summarizes the verified findings collected during "
            "the research session."
        ),
        "key_findings": key_findings,
        "business_analysis": [],
        "trends_and_insights": [],
        "questions_investigated": questions,
        "verified_sources": list(source_map.values()),
    }


def _safe_report(report, report_data):
    """Normalize the model response and prevent malformed report JSON."""
    if not isinstance(report, dict):
        return _fallback_report(report_data)

    required = [
        "title", "company", "filing", "period", "executive_summary",
        "key_findings", "business_analysis", "trends_and_insights",
        "questions_investigated", "verified_sources",
    ]

    for key in required:
        if key not in report:
            report[key] = [] if key in {
                "key_findings", "business_analysis", "trends_and_insights",
                "questions_investigated", "verified_sources",
            } else ""

    # Never allow the LLM to manufacture a source list. Sources in the
    # final report are restricted to citations already present in the
    # verified session data.
    verified_sources = {
        source.get("citation"): source
        for finding in report_data.get("findings", [])
        for source in (finding.get("sources", []) or [])
        if source.get("citation")
    }

    def keep_sources(items):
        cleaned = []

        for source in items or []:
            if not isinstance(source, dict):
                continue

            citation = source.get("citation")

            if citation in verified_sources:
                cleaned.append(verified_sources[citation])

        return cleaned

    report["verified_sources"] = keep_sources(report.get("verified_sources"))

    # If the model omitted sources, retain all verified session sources.
    if not report["verified_sources"]:
        report["verified_sources"] = list(verified_sources.values())

    for finding in report.get("key_findings", []):
        if isinstance(finding, dict):
            finding["sources"] = keep_sources(finding.get("sources"))

    for item in report.get("business_analysis", []):
        if isinstance(item, dict):
            item["sources"] = keep_sources(item.get("sources"))

    return report


def _clean_report_text(value):
    """Convert common Markdown formatting into ReportLab-safe inline markup."""
    if value is None:
        return ""

    text = str(value)

    # Escape XML-sensitive characters first because ReportLab Paragraph
    # interprets the text as a small subset of HTML/XML.
    from xml.sax.saxutils import escape
    text = escape(text)

    # Markdown bold: **text** -> <b>text</b>
    text = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)

    # Markdown italic: *text* -> <i>text</i>
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", text)

    # Markdown inline code: `text` -> text
    text = re.sub(r"`([^`]+)`", r"\1", text)

    # Remove Markdown heading markers when they appear at the beginning
    # of a line. The PDF already supplies its own heading styles.
    text = re.sub(r"(?m)^\s*#{1,6}\s+", "", text)

    # Convert Markdown bullets into the bullet character already used by
    # the PDF renderer.
    text = re.sub(r"(?m)^\s*[-*]\s+", "• ", text)

    # Preserve paragraph breaks while preventing excessive blank lines.
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.replace("\n", "<br/>")

    return text.strip()


def render_report_pdf(report):
    """Render the structured report to a deterministic PDF."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate,
        Paragraph,
        Spacer,
        Table,
        TableStyle,
        KeepTogether,
    )

    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    # Full random id: the download link cannot be guessed.
    filename = f"quarterly_wrapup_{uuid.uuid4().hex}.pdf"
    output_path = REPORT_DIR / filename

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "ReportTitle",
        parent=styles["Title"],
        alignment=TA_CENTER,
        spaceAfter=10,
    )

    heading = ParagraphStyle(
        "ReportHeading",
        parent=styles["Heading2"],
        spaceBefore=12,
        spaceAfter=6,
    )

    subheading = ParagraphStyle(
        "ReportSubheading",
        parent=styles["Heading3"],
        spaceBefore=8,
        spaceAfter=4,
    )

    body = ParagraphStyle(
        "ReportBody",
        parent=styles["BodyText"],
        leading=14,
        spaceAfter=6,
    )

    small = ParagraphStyle(
        "ReportSmall",
        parent=styles["BodyText"],
        fontSize=8.5,
        leading=11,
        spaceAfter=3,
    )

    story = [
        Paragraph(_clean_report_text(report.get("title") or "Quarterly Research Wrap-Up"), title_style),
    ]

    metadata = []

    for label, value in (
        ("Company", report.get("company", "")),
        ("Filing", report.get("filing", "")),
        ("Period", report.get("period", "")),
    ):
        if value:
            metadata.append([Paragraph(f"<b>{label}</b>", body), Paragraph(_clean_report_text(value), body)])

    if metadata:
        meta_table = Table(metadata, colWidths=[32 * mm, 140 * mm])
        meta_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.extend([meta_table, Spacer(1, 8)])

    summary = report.get("executive_summary", "")

    if summary:
        story.extend([
            Paragraph("Executive Summary", heading),
            Paragraph(_clean_report_text(summary), body),
        ])

    findings = report.get("key_findings", []) or []

    if findings:
        story.append(Paragraph("Key Findings", heading))

        for finding in findings:
            if not isinstance(finding, dict):
                continue

            block = [
                Paragraph(_clean_report_text(finding.get("title", "")), subheading),
                Paragraph(_clean_report_text(finding.get("summary", "")), body),
            ]

            for figure in finding.get("figures", []) or []:
                if not isinstance(figure, dict):
                    continue

                label = figure.get("item") or figure.get("label") or "Figure"
                value = figure.get("value", "")
                column = figure.get("column", "")
                units = figure.get("units", "")

                block.append(Paragraph(
                    _clean_report_text(
                        f"• **{label}**: {value} {units} ({column})"
                    ),
                    body,
                ))

            for source in finding.get("sources", []) or []:
                if isinstance(source, dict):
                    block.append(Paragraph(
                        _clean_report_text(
                            f"Source: {source.get('citation', '')} · p. {source.get('page', '')}"
                        ),
                        small,
                    ))

            story.append(KeepTogether(block))

    business = report.get("business_analysis", []) or []

    if business:
        story.append(Paragraph("Business / Segment Analysis", heading))

        for item in business:
            if not isinstance(item, dict):
                continue

            story.append(Paragraph(_clean_report_text(item.get("title", "")), subheading))
            story.append(Paragraph(_clean_report_text(item.get("summary", "")), body))

    trends = report.get("trends_and_insights", []) or []

    if trends:
        story.append(Paragraph("Important Trends and Insights", heading))

        for trend in trends:
            story.append(Paragraph(_clean_report_text(f"• {str(trend)}"), body))

    questions = report.get("questions_investigated", []) or []

    if questions:
        story.append(Paragraph("Questions Investigated", heading))

        for question in questions:
            story.append(Paragraph(_clean_report_text(f"• {str(question)}"), body))

    sources = report.get("verified_sources", []) or []

    if sources:
        story.append(Paragraph("Verified Sources", heading))

        for source in sources:
            if not isinstance(source, dict):
                continue

            story.append(Paragraph(
                _clean_report_text(
                    f"• {source.get('citation', '')} · p. {source.get('page', '')}"
                ),
                small,
            ))

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=report.get("title") or "Quarterly Research Wrap-Up",
    )

    doc.build(story)

    return output_path


async def generate_wrap_up_report(session):
    """Generate a report from verified session findings."""
    report_data = build_report_research_data(session)

    if not report_data["findings"]:
        raise HTTPException(
            400,
            "There are no completed, evidence-backed questions in this session.",
        )

    prompt = report_prompt(report_data)

    report_session_id = f"report-{uuid.uuid4().hex[:10]}"

    await report_service.create_session(
        app_name="tenq_wrap_up_report",
        user_id=USER_ID,
        session_id=report_session_id,
    )

    message = types.Content(
        role="user",
        parts=[types.Part(text=prompt)],
    )

    events = []

    try:
        async for event in report_runner.run_async(
            user_id=USER_ID,
            session_id=report_session_id,
            new_message=message,
        ):
            events.append(event)

        final_texts = [
            text_of(event)
            for event in events
            if (
                event.author != "user"
                and event.is_final_response()
                and text_of(event)
            )
        ]

        if not final_texts:
            raise RuntimeError("Report Agent returned no final response.")

        report = _safe_report(
            _extract_json(final_texts[-1]),
            report_data,
        )

    except Exception as exc:
        logger.warning(
            "Report Agent failed; using deterministic fallback: %s",
            exc,
        )
        report = _fallback_report(report_data)

    finally:
        # The temporary report session is not needed any more.
        try:
            await report_service.delete_session(
                app_name="tenq_wrap_up_report",
                user_id=USER_ID,
                session_id=report_session_id,
            )
        except Exception:
            pass

    return render_report_pdf(report)


# Both URL spellings work: /api/sessions/... (backend) and /api/session/... (older frontend).
@app.post("/api/sessions/{session_id}/report")
@app.post("/api/session/{session_id}/report")
async def create_wrap_up_report(session_id: str, user_id: str = Depends(get_user_id)):
    """Generate a professional PDF report from one completed chat session."""
    session = await service.get_session(
        app_name=APP_NAME,
        user_id=user_id,
        session_id=session_id,
    )

    if session is None:
        raise HTTPException(404, "Chat not found.")

    try:
        pdf_path = await generate_wrap_up_report(session)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "Wrap-up report generation failed: %s",
            exc,
            exc_info=True,
        )
        raise HTTPException(
            500,
            "Failed to generate the wrap-up report.",
        )

    try:
        blob = gcs.bucket(BUCKET).blob(f"{REPORTS_PREFIX}/{pdf_path.name}")
        await asyncio.to_thread(
            blob.upload_from_filename,
            str(pdf_path),
            content_type="application/pdf",
        )
    except Exception:
        logger.error("Could not store report %s", pdf_path.name, exc_info=True)
        raise HTTPException(500, "The report was created but could not be saved. Please try again.")
    finally:
        pdf_path.unlink(missing_ok=True)

    return {
        "success": True,
        "filename": pdf_path.name,
        "download_url": f"/api/reports/{pdf_path.name}",
    }


@app.get("/api/reports/{filename}")
def download_wrap_up_report(filename: str):
    """Download a generated wrap-up PDF."""
    safe_name = Path(filename).name

    if not re.fullmatch(r"quarterly_wrapup_[0-9a-f]{10,32}\.pdf", safe_name):
        raise HTTPException(404, "Report not found.")

    blob = gcs.bucket(BUCKET).blob(f"{REPORTS_PREFIX}/{safe_name}")

    try:
        content = blob.download_as_bytes()
    except Exception:
        raise HTTPException(404, "Report not found.")

    return Response(
        content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{safe_name}"'},
    )


# ============================================================
# Ingestion status helpers
# ============================================================

_status_db = None


def _status_collection():
    global _status_db
    if _status_db is None:
        _status_db = firestore.Client(project=PROJECT, database=FIRESTORE_DATABASE)
    return _status_db.collection(INGESTION_STATUS_COLLECTION)


def update_status(
    filing_id,
    step,
    status="running",
    error=None,
):
    """Save ingestion progress in Firestore for the frontend to poll."""
    try:
        _status_collection().document(filing_id).set({
            "filing_id": filing_id,
            "step": step,
            "status": status,
            "error": error,
            "updated_at": time.time(),
        })
    except Exception:
        logger.exception("Could not save ingestion status for %s", filing_id)

    print(
        f"[{filing_id}] {step} "
        f"(Status: {status})",
        flush=True,
    )


def read_status(filing_id):
    """Latest saved progress for a filing, or None."""
    try:
        snapshot = _status_collection().document(filing_id).get()
    except Exception:
        logger.exception("Could not read ingestion status for %s", filing_id)
        return None
    if not snapshot.exists:
        return None
    data = snapshot.to_dict() or {}
    # A job that stopped reporting (server restarted mid-way) is shown as failed,
    # so the UI never waits forever.
    if (
        data.get("status") == "running"
        and time.time() - float(data.get("updated_at") or 0) > INGESTION_STALE_SECONDS
    ):
        data.update(
            status="failed",
            step="Interrupted before finishing. Please start it again.",
            error="Interrupted",
        )
    return data


def cleanup_partial_filing(filing_id):
    """Purge partial/orphaned records from previous failed ingestion attempts."""
    try:
        target_tables = [
            "table_cells",
            "table_rows",
            "tables",
            "text_chunks",
            "filings",
        ]

        for t in target_tables:
            sql = (
                f"DELETE FROM `{PROJECT}.{DATASET}.{t}` "
                f"WHERE filing_id = @fid"
            )

            job = bq.query(
                sql,
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[
                        bigquery.ScalarQueryParameter(
                            "fid",
                            "STRING",
                            filing_id,
                        )
                    ]
                ),
            )

            job.result()

        logger.info(
            f"[{filing_id}] Pre-cleanup: "
            f"Purged any existing partial records."
        )

    except Exception as e:
        logger.warning(
            f"[{filing_id}] Pre-cleanup notice "
            f"(safe to ignore on fresh runs): {e}"
        )


# ============================================================
# Library: companies and filings
# ============================================================

@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "history_store": history_location(),
        "sec_user_agent_configured": bool(
            os.getenv("SEC_USER_AGENT")
        ),
    }


@app.get("/api/me")
def me(request: Request):
    """Who is signed in, and whether they may add filings (used to show/hide that panel)."""
    claims = verify_user(request)
    return {
        "uid": claims["uid"],
        "email": claims.get("email", ""),
        "can_ingest": can_ingest(claims),
    }


@app.get("/api/companies")
def companies(user_id: str = Depends(get_user_id)):
    return run_query(
        f"SELECT ticker, ANY_VALUE(company) AS company "
        f"FROM {table('filings')} "
        f"GROUP BY ticker "
        f"ORDER BY ticker"
    )


@app.get("/api/filings")
def filings(
    request: Request,
    company: str = "",
    user_id: str = Depends(get_user_id),
):
    rows = run_query(
        f"""
        SELECT
            filing_id,
            ticker,
            company,
            form,
            fiscal_period,
            period_end
        FROM {table('filings')}
        WHERE (
            @c = ''
            OR LOWER(ticker) = LOWER(@c)
        )
        ORDER BY ticker, period_end
        """,
        [
            bigquery.ScalarQueryParameter(
                "c",
                "STRING",
                company,
            )
        ],
    )

    base = str(request.base_url).rstrip("/")

    for row in rows:
        row["pdf_url"] = (
            f"{base}/api/pdf/{row['filing_id']}"
        )

    return rows


# ============================================================
# Incremental ingestion & status endpoints
# ============================================================

class IngestRequest(BaseModel):
    ticker: str
    fiscal_year: str
    quarter: str


@app.get("/api/ingest/status/{filing_id}")
def get_ingest_status(filing_id: str, user_id: str = Depends(get_user_id)):
    """Return current ingestion status for the UI."""
    return read_status(filing_id) or {
        "filing_id": filing_id,
        "step": "Not started",
        "status": "idle",
    }


@app.post("/api/ingest")
async def ingest_filing(
    req: IngestRequest,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(get_admin_user_id),
):
    ticker = req.ticker.strip().upper()
    year = req.fiscal_year.strip()
    quarter = req.quarter.strip().upper()

    if not ticker or not year or not quarter:
        raise HTTPException(
            400,
            "Ticker, fiscal year, and quarter are required.",
        )

    # Reject malformed input early (10-Qs only exist for Q1-Q3).
    if (
        not re.fullmatch(r"[A-Z0-9.\-]{1,10}", ticker)
        or not re.fullmatch(r"\d{4}", year)
        or quarter not in {"Q1", "Q2", "Q3"}
    ):
        raise HTTPException(
            400,
            "Use a valid ticker, a 4-digit year, and quarter Q1, Q2 or Q3.",
        )

    filing_id = (
        f"{ticker.lower()}_FY{year}_{quarter}_10Q"
    )

    # --------------------------------------------------------
    # Check whether the filing has already been fully ingested.
    # --------------------------------------------------------
    completed_check = run_query(
        f"""
        SELECT filing_id
        FROM {table('text_chunks')}
        WHERE filing_id = @fid
        LIMIT 1
        """,
        [
            bigquery.ScalarQueryParameter(
                "fid",
                "STRING",
                filing_id,
            )
        ],
    )

    if completed_check:
        return {
            "status": "already_exists",
            "filing_id": filing_id,
            "message": (
                f"Filing {filing_id} is already fully "
                f"ingested and ready for Q/A!"
            ),
        }

    # --------------------------------------------------------
    # Do not start a second pipeline for a filing that is
    # already being ingested (e.g. a double click).
    # --------------------------------------------------------
    current = read_status(filing_id)

    if current and current.get("status") == "running":
        return {
            "status": "queued",
            "filing_id": filing_id,
            "message": "Ingestion is already in progress for this filing.",
        }

    # --------------------------------------------------------
    # Queue background ingestion.
    # --------------------------------------------------------
    update_status(
        filing_id,
        "Queued for processing...",
    )

    background_tasks.add_task(
        run_incremental_pipeline,
        ticker,
        year,
        quarter,
        filing_id,
    )

    return {
        "status": "queued",
        "filing_id": filing_id,
        "message": (
            f"Incremental ingestion started for "
            f"{ticker} FY{year} {quarter}."
        ),
    }


# ------------------------------------------------------------
# Fast single-filing ingestion
# ------------------------------------------------------------

_CIK_CACHE = {}


def _cached_cik(ticker):
    """SEC's ticker list is large; look each company up only once per server run."""
    key = ticker.upper()
    if key not in _CIK_CACHE:
        _CIK_CACHE[key] = get_company_cik(ticker)
    return _CIK_CACHE[key]


def _recent_submissions(cik):
    """Only SEC's 'recent' filing list (one small download). Older archive
    files are fetched only if the quarter is not found here."""
    data = json.loads(
        sec_get(f"https://data.sec.gov/submissions/CIK{cik}.json").decode("utf-8")
    )
    recent = data.get("filings", {}).get("recent", {})
    count = len(recent.get("accessionNumber", []))
    keys = ("form", "filingDate", "reportDate", "accessionNumber", "primaryDocument")
    filings = [
        {k: (recent.get(k) or [""] * count)[i] for k in keys}
        for i in range(count)
    ]
    return filings, data.get("name", "")


def _find_10q(cik, year, quarter, submissions):
    """Return (url, html_bytes, html_text) for the exact 10-Q, or None.

    Candidates from the requested year are tried first, so usually the
    first or second download is the right filing.
    """
    candidates = []
    for f in submissions:
        if f.get("form") != "10-Q" or not f.get("accessionNumber") or not f.get("primaryDocument"):
            continue
        try:
            distance = abs(int((f.get("reportDate") or "")[:4]) - int(year))
        except ValueError:
            continue
        if distance <= 1:
            candidates.append((distance, f))

    candidates.sort(key=lambda item: item[0])      # stable: newest first within a year

    for _, f in candidates[:12]:
        url = build_filing_url(cik, f["accessionNumber"], f["primaryDocument"])
        try:
            html_bytes = sec_get(url)
        except Exception:
            continue
        text = html_bytes.decode("utf-8", errors="replace")
        fp = dei(text, "DocumentFiscalPeriodFocus")
        fy = dei(text, "DocumentFiscalYearFocus")
        if fp and fp.upper() == quarter and fy and year in fy:
            return url, html_bytes, text
    return None


def run_incremental_pipeline(
    ticker,
    year,
    quarter,
    filing_id,
):
    """
    Download, parse, embed and index one 10-Q.

    Speed-ups compared with the original version:
      * the matching filing is downloaded once (it was downloaded twice) and
        never re-read from Cloud Storage;
      * SEC's company list is cached and only the recent filing list is read;
      * PDF conversion, uploads and parsing overlap;
      * table, row and text embeddings run at the same time, each in
        parallel batches (see common.embed);
      * the five BigQuery tables are written at the same time. Each write
        replaces the filing's old rows, so a separate clean-up is not needed.
    """
    pipeline_start = time.time()

    def step(text):
        update_status(filing_id, text)

    def took(label, since):
        logger.info(f"[{filing_id}] {label} took {time.time() - since:.1f}s")

    try:
        # ----------------------------------------------------
        # 1. Find the exact 10-Q on SEC EDGAR.
        # ----------------------------------------------------
        step("Finding the matching 10-Q on SEC EDGAR...")
        t = time.time()

        cik, company_name = _cached_cik(ticker)
        submissions, sec_company_name = _recent_submissions(cik)
        found = _find_10q(cik, year, quarter, submissions)

        if not found:
            # Older quarters live in SEC's archive files: try those too.
            all_submissions, _ = get_company_submissions(cik)
            found = _find_10q(cik, year, quarter, all_submissions)

        if not found:
            raise RuntimeError(
                f"Could not find matching SEC 10-Q for {ticker} FY{year} {quarter}"
            )

        target_url, html_bytes, html_text = found
        took("SEC lookup + download", t)

        # ----------------------------------------------------
        # 2. Convert to PDF, upload both files, parse tables
        #    and text (uploads run while parsing continues).
        # ----------------------------------------------------
        step("Converting to PDF, uploading and extracting tables and text...")
        t = time.time()

        bucket = gcs.bucket(BUCKET)
        pdf_blob = bucket.blob(f"raw/{ticker.lower()}/FY{year}_{quarter}_10Q.pdf")
        html_blob = bucket.blob(f"raw/{ticker.lower()}/FY{year}_{quarter}_10Q.html")

        with ThreadPoolExecutor(max_workers=2) as uploads:
            html_upload = uploads.submit(
                html_blob.upload_from_string,
                html_bytes,
                content_type="text/html; charset=utf-8",
            )

            with pymupdf.open(stream=html_bytes, filetype="html") as html_doc:
                pdf_bytes = html_doc.convert_to_pdf()

            pdf_upload = uploads.submit(
                pdf_blob.upload_from_string,
                pdf_bytes,
                content_type="application/pdf",
            )

            filing_row = {
                "filing_id": filing_id,
                "ticker": ticker,
                "company": (
                    dei(html_text, "EntityRegistrantName")
                    or company_name
                    or sec_company_name
                    or ticker
                ),
                "form": dei(html_text, "DocumentType") or "10-Q",
                "fiscal_period": f"FY{year} {quarter}",
                "period_end": dei(html_text, "DocumentPeriodEndDate"),
                "pdf_uri": f"gs://{BUCKET}/{pdf_blob.name}",
                "html_uri": f"gs://{BUCKET}/{html_blob.name}",
                "ingested_at": datetime.now(timezone.utc).isoformat(),
            }

            tables = extract_tables(html_bytes)

            with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
                pdf_pages = [" ".join(page.get_text().split()).lower() for page in doc]

            table_rows, row_rows, cell_rows = build_rows(filing_row, tables, pdf_pages)
            chunks = make_chunks(read_pdf(pdf_bytes))

            html_upload.result()
            pdf_upload.result()

        took("PDF + upload + parsing", t)

        chunk_rows = [
            {
                "chunk_id": f"{filing_id}:c{i:04d}",
                "filing_id": filing_id,
                "ticker": ticker.lower(),
                **chunk,
                "citation": make_citation(filing_row, chunk["page_start"], chunk["section"]),
            }
            for i, chunk in enumerate(chunks)
        ]
        chunk_texts = [
            f"{filing_row['company']} {filing_row['form']} "
            f"{filing_row['fiscal_period']} | {c['section']}\n{c['text']}"
            for c in chunks
        ]

        # ----------------------------------------------------
        # 3. Embeddings: tables, rows and text at the same time.
        # ----------------------------------------------------
        step(
            f"Generating embeddings for {len(table_rows)} tables, "
            f"{len(row_rows)} line items and {len(chunk_rows)} text chunks..."
        )
        t = time.time()

        jobs = {
            "tables": (table_rows, [r["text"] for r in table_rows]),
            "rows": (row_rows, [r["text"] for r in row_rows]),
            "chunks": (chunk_rows, chunk_texts),
        }

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {
                name: pool.submit(embed, texts)
                for name, (_, texts) in jobs.items()
                if texts
            }
            for name, future in futures.items():
                rows, texts = jobs[name]
                vectors = future.result()
                if len(vectors) != len(rows):
                    raise RuntimeError(
                        f"Expected {len(rows)} {name} embeddings, got {len(vectors)}"
                    )
                for row, vector in zip(rows, vectors):
                    row["embedding"] = vector

        took("Embeddings", t)

        # ----------------------------------------------------
        # 4. Write all five BigQuery tables at the same time.
        #    save_rows replaces this filing's old rows in each table.
        # ----------------------------------------------------
        step("Saving to BigQuery...")
        t = time.time()

        writes = [
            ("filings", [filing_row]),
            ("tables", table_rows),
            ("table_rows", row_rows),
            ("table_cells", cell_rows),
            ("text_chunks", chunk_rows),
        ]

        with ThreadPoolExecutor(max_workers=len(writes)) as pool:
            for future in [
                pool.submit(save_rows, name, filing_id, rows)
                for name, rows in writes
            ]:
                future.result()

        took("BigQuery writes", t)

        logger.info(
            f"[{filing_id}] Total ingestion finished in "
            f"{time.time() - pipeline_start:.1f}s"
        )

        update_status(
            filing_id,
            "Ingestion completed successfully!",
            status="success",
        )

    except Exception as exc:
        err_msg = str(exc)[:300]

        logger.error(
            f"[{filing_id}] Ingestion failed: {exc}",
            exc_info=True,
        )

        update_status(
            filing_id,
            f"Failed: {err_msg}",
            status="failed",
            error=err_msg,
        )


# ============================================================
# Asking questions
# ============================================================

class AskRequest(BaseModel):
    question: str
    session_id: str | None = None
    ticker: str = ""
    fiscal_period: str = ""


# ============================================================
# Normal question endpoint
# ============================================================

@app.post("/api/ask")
async def ask(req: AskRequest, user_id: str = Depends(get_user_id)):
    check_ask_rate(user_id)
    question = req.question.strip()

    if not question:
        raise HTTPException(
            400,
            "The question is empty.",
        )

    started = time.time()
    storage_seconds = 0.0

    # --------------------------------------------------------
    # Reuse the existing chat when the frontend supplies one
    # (the runner loads it; no extra read here). Otherwise
    # create a new chat, with its title.
    # --------------------------------------------------------
    if req.session_id:
        session_id = req.session_id
    else:
        session_id = f"chat-{uuid.uuid4().hex[:10]}"
        t = time.time()
        await service.create_session(
            app_name=APP_NAME,
            user_id=user_id,
            session_id=session_id,
            state={"title": question[:70]},
        )
        storage_seconds += time.time() - t

    # --------------------------------------------------------
    # Add optional UI filters to the message sent to the
    # financial agent.
    # --------------------------------------------------------
    text = question

    if req.ticker or req.fiscal_period:
        text += (
            f"{FILTER_MARKER} "
            f"company={req.ticker or 'all'}; "
            f"fiscal period={req.fiscal_period or 'any'} "
            f"(default scope; the question overrides it)]"
        )

    events = []
    answer = ""

    # --------------------------------------------------------
    # Run the main financial Q&A agent.
    # --------------------------------------------------------
    try:
        message = types.Content(
            role="user",
            parts=[
                types.Part(text=text)
            ],
        )

        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=message,
            run_config=RUN_CONFIG,
        ):
            events.append(event)

            if (
                event.is_final_response()
                and event.content
                and event.content.parts
            ):
                answer = text_of(event)

    except SessionNotFoundError:
        raise HTTPException(
            404,
            "This chat no longer exists. Start a new research.",
        )

    except LlmCallsLimitExceededError:
        answer = (
            "This question needed too many lookups to answer quickly. "
            "Please narrow it down, for example to one company, one metric "
            "or one period at a time."
        )

    except Exception as exc:
        if (
            "429" in str(exc)
            or "RESOURCE_EXHAUSTED" in str(exc)
        ):
            raise HTTPException(
                429,
                "The model is rate limited. "
                "Wait a minute and try again.",
            )

        logger.error("Agent run failed", exc_info=exc)
        raise HTTPException(
            500,
            "The research agent hit an error. Please try again.",
        )

    finally:
        # Save everything this question produced in ONE Firestore write.
        t = time.time()
        try:
            await service.flush_session(APP_NAME, user_id, session_id)
        except Exception:
            logger.exception("Could not save chat %s to Firestore", session_id)
        storage_seconds += time.time() - t

    # --------------------------------------------------------
    # Build the normal financial answer and evidence.
    # --------------------------------------------------------
    result = build_answer(
        answer,
        events,
    )

    # Follow-up suggestions are NOT generated here any more: the user gets
    # the answer immediately and the frontend asks /api/suggestions next.
    result["suggested_questions"] = []

    llm_calls = sum(
        1 for e in events
        if e.author != "user" and e.content and e.content.role == "model"
    )

    print(
        f"ask: "
        f"{timing_breakdown(events, started, storage_seconds)} "
        f"llm_calls={llm_calls} "
        f"status={result['status']} "
        f"tools={result['tools_used']} "
        f"sources={len(result['citations'])} "
        f"unsupported={result.get('unsupported_numbers')}",
        flush=True,
    )

    return {
        "session_id": session_id,
        **result,
    }


# ============================================================
# Streaming question endpoint
# ============================================================

@app.post("/api/ask/stream")
async def ask_stream(req: AskRequest, user_id: str = Depends(get_user_id)):
    """Server-Sent Events streaming endpoint."""
    question = req.question.strip()

    if not question:
        raise HTTPException(
            400,
            "The question is empty.",
        )

    if req.session_id:
        session_id = req.session_id
    else:
        session_id = f"chat-{uuid.uuid4().hex[:10]}"
        await service.create_session(
            app_name=APP_NAME,
            user_id=user_id,
            session_id=session_id,
            state={"title": question[:70]},
        )

    text = question

    if req.ticker or req.fiscal_period:
        text += (
            f"{FILTER_MARKER} "
            f"company={req.ticker or 'all'}; "
            f"fiscal period={req.fiscal_period or 'any'} "
            f"(default scope; the question overrides it)]"
        )

    async def event_generator():
        message = types.Content(
            role="user",
            parts=[
                types.Part(text=text)
            ],
        )

        events = []

        # ----------------------------------------------------
        # Send session metadata first.
        # ----------------------------------------------------
        yield (
            f"data: {json.dumps({'type': 'init', 'session_id': session_id})}\n\n"
        )

        try:
            # ------------------------------------------------
            # Stream financial agent response.
            # ------------------------------------------------
            async for event in runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=message,
                run_config=RUN_CONFIG,
            ):
                events.append(event)

                if (
                    event.content
                    and event.content.parts
                ):
                    for part in event.content.parts:
                        if part.text:
                            yield (
                                "data: "
                                + json.dumps(
                                    {
                                        "type": "text_delta",
                                        "content": part.text,
                                    }
                                )
                                + "\n\n"
                            )

            await service.flush_session(APP_NAME, user_id, session_id)

            # ------------------------------------------------
            # Get final financial answer.
            # ------------------------------------------------
            finals = [
                text_of(e)
                for e in events
                if (
                    e.author != "user"
                    and e.is_final_response()
                    and text_of(e)
                )
            ]

            final_answer = (
                finals[-1]
                if finals
                else ""
            )

            # ------------------------------------------------
            # Build final evidence/citation response.
            # ------------------------------------------------
            result = build_answer(
                final_answer,
                events,
            )

            # =================================================
            # Generate suggestions after the answer is
            # completely finished.
            #
            # We do NOT stream suggestions token-by-token.
            # They are sent together with the final result.
            # =================================================
            if result["status"] == "answered":
                result["suggested_questions"] = (
                    await generate_suggested_questions(
                        user_id=user_id,
                        session_id=session_id,
                        current_question=question,
                        current_answer=result["answer"],
                        citations=result["citations"],
                    )
                )
            else:
                result["suggested_questions"] = []

            # ------------------------------------------------
            # Send final response.
            # ------------------------------------------------
            yield (
                "data: "
                + json.dumps(
                    {
                        "type": "final",
                        "result": result,
                    }
                )
                + "\n\n"
            )

        except Exception as exc:
            err_msg = str(exc)[:300]

            yield (
                "data: "
                + json.dumps(
                    {
                        "type": "error",
                        "message": err_msg,
                    }
                )
                + "\n\n"
            )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
    )


# ============================================================
# Follow-up suggestions (called by the frontend AFTER the answer
# is already on screen, so they never slow the answer down)
# ============================================================

class SuggestionsRequest(BaseModel):
    session_id: str = ""
    question: str
    answer: str
    citations: list = []
    previous_turns: list = []      # [{"question": ..., "answer": ...}], oldest first


@app.post("/api/suggestions")
async def suggestions(req: SuggestionsRequest, user_id: str = Depends(get_user_id)):
    questions = await generate_suggested_questions(
        user_id=user_id,
        session_id=req.session_id,
        current_question=req.question,
        current_answer=req.answer,
        citations=req.citations,
        previous_turns=[
            t for t in req.previous_turns[-4:]
            if isinstance(t, dict)
        ],
    )
    return {"suggested_questions": questions}


# ============================================================
# Chat history
# ============================================================

@app.get("/api/sessions")
async def list_chats(user_id: str = Depends(get_user_id)):
    listing = await service.list_sessions(
        app_name=APP_NAME,
        user_id=user_id,
    )

    chats = []

    for item in listing.sessions:
        title = (item.state or {}).get("title", "")

        # Older chats without a saved title: read the first question once.
        if not title:
            full = await service.get_session(
                app_name=APP_NAME,
                user_id=user_id,
                session_id=item.id,
            )

            first = next(
                (
                    text_of(e)
                    for e in (full.events if full else [])
                    if e.author == "user"
                ),
                "",
            )
            title = first.split(FILTER_MARKER)[0][:70]

        # Skip sessions that were created but never used.
        if title:
            chats.append(
                {
                    "session_id": item.id,
                    "title": title,
                    "updated_at": item.last_update_time,
                }
            )

    return sorted(
        chats,
        key=lambda chat: chat["updated_at"],
        reverse=True,
    )


FILTER_RE = re.compile(
    r"\[UI filters:\s*company=([^;\]]*);\s*fiscal period=([^\]\(]*)"
)


def filters_from_events(events):
    """The company/period filters used for the chat's latest question.

    Every question is saved with its filters appended ("[UI filters: ...]"),
    so reopening a chat can put the same filters back in the UI. If the
    latest question had no filters, none are returned.
    """
    for event in reversed(events):
        if event.author != "user":
            continue
        match = FILTER_RE.search(text_of(event))
        if not match:
            break
        company = match.group(1).strip()
        period = match.group(2).strip()
        return {
            "ticker": "" if company.lower() == "all" else company,
            "fiscal_period": "" if period.lower() == "any" else period,
        }
    return {"ticker": "", "fiscal_period": ""}


@app.get("/api/sessions/{session_id}")
async def get_chat(session_id: str, user_id: str = Depends(get_user_id)):
    session = await service.get_session(
        app_name=APP_NAME,
        user_id=user_id,
        session_id=session_id,
    )

    if session is None:
        raise HTTPException(
            404,
            "Chat not found.",
        )

    return {
        "session_id": session_id,
        "turns": turns_from_events(
            session.events
        ),
        "filters": filters_from_events(session.events),
    }


@app.delete("/api/sessions/{session_id}")
async def delete_chat(session_id: str, user_id: str = Depends(get_user_id)):
    await service.delete_session(
        app_name=APP_NAME,
        user_id=user_id,
        session_id=session_id,
    )

    return {
        "deleted": session_id
    }


# ============================================================
# Sources: table viewer and PDF
# ============================================================

@app.get("/api/table/{table_id}")
def get_table(table_id: str, user_id: str = Depends(get_user_id)):
    rows = run_query(
        f"""
        SELECT
            table_id,
            table_title,
            page,
            scale,
            markdown,
            citation
        FROM {table('tables')}
        WHERE table_id = @id
        """,
        [
            bigquery.ScalarQueryParameter(
                "id",
                "STRING",
                table_id,
            )
        ],
    )

    if not rows:
        raise HTTPException(
            404,
            "Table not found.",
        )

    return rows[0]


def highlight_pdf(content, page, terms):
    """Return the PDF with yellow highlights on the cited page.

    Each term (a number, a row label or a sentence) is searched on the cited
    page and the next one (a text passage can continue over the page break).
    Long sentences are searched in 6-word pieces, because a PDF line break in
    the middle of a sentence stops a whole-sentence match.
    """
    doc = pymupdf.open(stream=content, filetype="pdf")
    try:
        first = max(0, page - 1)
        pages = [doc[i] for i in range(first, min(first + 2, len(doc)))]

        for term in terms:
            words = term.split()
            pieces = (
                [" ".join(words[i:i + 6]) for i in range(0, len(words), 6)]
                if len(words) > 6
                else [term]
            )
            for pdf_page in pages:
                for piece in pieces:
                    if len(piece) < 2:
                        continue
                    for rect in pdf_page.search_for(piece):
                        pdf_page.add_highlight_annot(rect)

        return doc.tobytes()
    finally:
        doc.close()


@app.get("/api/pdf/{filing_id}")
def get_pdf(
    filing_id: str,
    page: int = 0,
    hl: list[str] = Query(default=[]),
):
    rows = run_query(
        f"""
        SELECT pdf_uri
        FROM {table('filings')}
        WHERE filing_id = @f
        """,
        [
            bigquery.ScalarQueryParameter(
                "f",
                "STRING",
                filing_id,
            )
        ],
    )

    if (
        not rows
        or not rows[0]["pdf_uri"].startswith("gs://")
    ):
        raise HTTPException(
            404,
            "PDF not found.",
        )

    bucket_name, blob_name = (
        rows[0]["pdf_uri"][5:].split("/", 1)
    )

    content = (
        gcs
        .bucket(bucket_name)
        .blob(blob_name)
        .download_as_bytes()
    )

    # Citation links pass ?page=N&hl=term&hl=term...: highlight those terms.
    terms = [t.strip()[:300] for t in hl if t and t.strip()][:12]
    if page > 0 and terms:
        try:
            content = highlight_pdf(content, page, terms)
        except Exception:
            logger.exception("Could not highlight %s", filing_id)

    return Response(
        content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": "inline"
        },
    )