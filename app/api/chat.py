from __future__ import annotations

import re
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from collections import deque
from app.models.chat_query import ChatQuery


from openai import OpenAI

import time

from app.api.deps import get_current_user, get_db
from app.core.config import settings
from app.models.user import User
from app.services.sql_sandbox import sanitize_sql, SQLSandboxError

router = APIRouter(prefix="/chat", tags=["chat"])

# ---- canned intents cache (in-memory, TTL) ----
_CANNED_CACHE_TTL_SECONDS = 600  # 10 minutes

# key -> (expires_at, value)
# value = {"answer": str, "sql": str, "table": {"columns": [...], "rows": [[...], ...]}}
_CANNED_CACHE: dict[tuple, tuple[float, dict]] = {}

# ---- rate limit (in-memory) ----
_RATE_LIMIT_PER_MIN = 20  # requests per minute per user
_RATE_BUCKET: dict[int, deque[float]] = {}  # user_id -> timestamps


def _rate_limit(user_id: int) -> None:
    now = time.time()
    q = _RATE_BUCKET.get(user_id)
    if q is None:
        q = deque()
        _RATE_BUCKET[user_id] = q

    # drop older than 60s
    cutoff = now - 60.0
    while q and q[0] < cutoff:
        q.popleft()

    if len(q) >= _RATE_LIMIT_PER_MIN:
        raise HTTPException(status_code=429, detail="Rate limit exceeded (per minute)")

    q.append(now)


def _detect_target_currency(question: str) -> str:
    q = question.lower()
    if "руб" in q or " rub" in q or "rub" in q:
        return "RUB"
    if "евро" in q or " eur" in q or "eur" in q:
        return "EUR"
    if "франк" in q or " chf" in q or "chf" in q:
        return "CHF"
    return "USD"


def _cache_get(key: tuple) -> dict | None:
    item = _CANNED_CACHE.get(key)
    if not item:
        return None
    expires_at, value = item
    if expires_at < time.time():
        _CANNED_CACHE.pop(key, None)
        return None
    return value


def _cache_set(key: tuple, value: dict) -> None:
    _CANNED_CACHE[key] = (time.time() + _CANNED_CACHE_TTL_SECONDS, value)


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class ChatResponse(BaseModel):
    answer: str
    sql: str | None = None
    source: str  # canned | llm
    table: dict | None = None  # {"columns":[...], "rows":[[...], ...]}


# --------- intent router (canned) ----------

@dataclass(frozen=True)
class RoutedQuery:
    sql: str
    params: dict[str, Any]
    source: str  # canned | llm
    intent: str | None = None  # for caching canned intents



def _month_range(dt: datetime) -> tuple[str, str]:
    """Returns [start_of_month, start_of_next_month) in ISO format."""
    start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        next_m = start.replace(year=start.year + 1, month=1)
    else:
        next_m = start.replace(month=start.month + 1)
    return start.date().isoformat(), next_m.date().isoformat()


def _last_month_range(now: datetime) -> tuple[str, str]:
    if now.month == 1:
        last = now.replace(year=now.year - 1, month=12)
    else:
        last = now.replace(month=now.month - 1)
    return _month_range(last)


def _route_canned(question: str) -> RoutedQuery | None:
    q = question.strip().lower()

    # total this month
    if re.search(r"\b(total|sum)\b.*\b(this month|current month)\b", q) or re.search(r"\bspent\b.*\bthis month\b", q):
        start, end = _month_range(datetime.now(timezone.utc))
        return RoutedQuery(
            sql="""
            SELECT COALESCE(SUM(total_usd), 0) AS total_spent
            FROM receipts
            WHERE purchase_datetime >= :start AND purchase_datetime < :end
            AND receipts.status = 'done'

            """,
            params={"start": start, "end": end},
            source="canned",
            intent="total_this_month",
        )

    # total last month
    if re.search(r"\b(total|sum)\b.*\blast month\b", q) or re.search(r"\bspent\b.*\blast month\b", q):
        start, end = _last_month_range(datetime.now(timezone.utc))
        return RoutedQuery(
            sql="""
            SELECT COALESCE(SUM(total_usd), 0) AS total_spent
            FROM receipts
            WHERE purchase_datetime >= :start AND purchase_datetime < :end
            AND receipts.status = 'done'

            """,
            params={"start": start, "end": end},
            source="canned",
            intent="total_last_month",
        )

    # top merchants
    if re.search(r"\b(top|most)\b.*\b(merchant|store|shops?)\b", q):
        return RoutedQuery(
            sql="""
            SELECT merchant, COUNT(*) AS receipts_count, COALESCE(SUM(total_usd),0) AS total_spent
            FROM receipts
            WHERE merchant IS NOT NULL
            GROUP BY merchant
            ORDER BY total_spent DESC
            LIMIT 10
            """,
            params={},
            source="canned",
            intent="top_merchants",
        )

    # most expensive receipt
    if re.search(r"\b(most expensive|largest|biggest)\b", q):
        return RoutedQuery(
            sql="""
            SELECT id, merchant, purchase_datetime, total_usd, currency
            FROM receipts
            WHERE total_usd IS NOT NULL
            ORDER BY total_usd DESC
            LIMIT 1
            """,
            params={},
            source="canned",
            total="most_expensive_receipt"
        )

    return None


# --------- LLM -> SQL ----------

_SCHEMA_HINT = """
SQLite schema:

Table receipts:
- id INTEGER
- user_id INTEGER
- uploaded_at DATETIME
- status TEXT
- merchant TEXT
- purchase_datetime DATETIME
- total FLOAT
- total_usd FLOAT
- currency TEXT
- image_path TEXT
- raw_ocr_text TEXT
- raw_llm_json TEXT
- error TEXT
- version INTEGER
- category TEXT  -- one of GROCERIES, CAFE, RESTAURANT, TRANSPORT, PHARMACY, UTILITIES, ENTERTAINMENT, CLOTHING, ELECTRONICS, OTHER

Table receipt_items:
- id INTEGER
- receipt_id INTEGER (FK -> receipts.id)
- name TEXT
- quantity FLOAT
- unit_price FLOAT
- line_total FLOAT
- line_total_usd FLOAT
"""

_SQL_SYSTEM = (
    "You generate read-only SQLite SELECT queries for receipt analytics.\n"
    "Rules:\n"
    "- Output ONLY SQL. No markdown, no code fences, no explanations.\n"
    "- Single statement. No semicolons.\n"
    "- SELECT-only (WITH ... SELECT is ok).\n"
    "- Always reference the receipts table in FROM (directly or via JOIN).\n"
    "- Do NOT filter by user_id; it will be enforced externally.\n"
    "- Prefer ISO date comparisons.\n"
    "- If question is about items, JOIN receipt_items ON receipt_items.receipt_id = receipts.id.\n"
    "- All monetary analytics must be in USD. Prefer receipts.total_usd and receipt_items.line_total_usd.\n"
    "- For totals, always use COALESCE(SUM(...), 0) so the query returns one row even if no receipts match.\n"
    "- Use receipts.total_usd and receipt_items.line_total_usd for money.\n"
    "- receipts.category is one of: GROCERIES, CAFE, RESTAURANT, TRANSPORT, PHARMACY, UTILITIES, ENTERTAINMENT, CLOTHING, ELECTRONICS, OTHER.\n"
    "- If the question mentions a category (e.g., 'кафе', 'coffee', 'restaurant', 'аптека', 'транспорт'), add a WHERE filter on receipts.category with the best matching category.\n"
)


def _extract_sql(text: str) -> str:
    t = (text or "").strip()
    # strip code fences if model returns them
    m = re.search(r"```(?:sql)?\s*(.*?)```", t, re.DOTALL | re.IGNORECASE)
    if m:
        t = m.group(1).strip()
    return t.strip().strip(";").strip()


def _llm_generate_sql(question: str) -> str:
    client = OpenAI(api_key=settings.OPENAI_API_KEY or None)

    resp = client.responses.create(
        model=getattr(settings, "OPENAI_SQL_MODEL", settings.OPENAI_OCR_MODEL),
        input=[
            {"role": "system", "content": _SQL_SYSTEM + "\n" + _SCHEMA_HINT},
            {"role": "user", "content": f"Question: {question}\nSQL:"},
        ],
        temperature=0,
        max_output_tokens=600,
    )
    sql = _extract_sql(resp.output_text)
    if not sql:
        raise HTTPException(status_code=500, detail="LLM returned empty SQL")
    return sql


# --------- query engine (sandbox -> execute) ----------

def _execute_sql(db: Session, sql: str, params: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    result = db.execute(sql_text(sql), params)
    rows = result.fetchall()
    cols = list(result.keys())
    # convert to plain lists (json friendly)
    out_rows = [list(r) for r in rows]
    return cols, out_rows


# --------- answer summarizer ----------

_SUMMARY_SYSTEM = (
    "You answer questions about spending based on the provided SQL table.\n"
    "Rules:\n"
    "- Answer in the same language as the question.\n"
    "- Monetary amounts in the table are in USD.\n"
    "- If the user asks for a different currency, convert USD amounts using the provided FX rates.\n"
    "- If the table is empty (row_count=0):\n"
    "  * For 'how much / сколько потратил' questions: answer 0 in the requested currency.\n"
    "  * For yes/no questions like 'были ли': answer 'Нет'.\n"
    "  * Otherwise: say there is no data for this question.\n"
    "- Be concise (1-3 sentences). Do not mention SQL/databases.\n"
)


def _llm_summarize(question: str, columns: list[str], rows: list[list[Any]], target_currency: str) -> str:
    client = OpenAI(api_key=settings.OPENAI_API_KEY or None)

    # limit context size
    preview_rows = rows[:30]

    rub_to_usd = float(getattr(settings, "FX_RUB_TO_USD", 0.0) or 0.0)
    eur_to_usd = float(getattr(settings, "FX_EUR_TO_USD", 0.0) or 0.0)
    chf_to_usd = float(getattr(settings, "FX_CHF_TO_USD", 0.0) or 0.0)

    fx = {
        "target_currency": target_currency,
        "usd_to_rub": (1.0 / rub_to_usd) if rub_to_usd else None,                      # 1 USD = X RUB
        "usd_to_eur": (1.0 / eur_to_usd) if eur_to_usd else None,  # derived
        "usd_to_chf": (1.0 / chf_to_usd) if chf_to_usd else None,  # derived
    }


    payload = {"columns": columns, "rows": preview_rows, "row_count": len(rows), "fx": fx}

    resp = client.responses.create(
        model=getattr(settings, "OPENAI_SUMMARY_MODEL", settings.OPENAI_OCR_MODEL),
        input=[
            {"role": "system", "content": _SUMMARY_SYSTEM},
            {"role": "user", "content": f"Question: {question}\nTable JSON: {payload}\nAnswer:"},
        ],
        temperature=0.2,
        max_output_tokens=250,
    )
    ans = (resp.output_text or "").strip()
    return ans or "I couldn't generate an answer from the data."


@router.post("", response_model=ChatResponse)
def chat(
    payload: ChatRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    question = payload.question.strip()

    # ---- rate limit ----
    t0 = time.perf_counter()
    chosen_source = "unknown"
    generated_sql: str | None = None
    sandbox_sql: str | None = None
    error: str | None = None

    try:
        _rate_limit(user.id)

        # 1) intent router: canned first
        routed = _route_canned(question)
        if routed is None:
            # 2) fallback: LLM -> SQL
            generated_sql = _llm_generate_sql(question)
            routed = RoutedQuery(sql=generated_sql, params={}, source="llm")
        else:
            generated_sql = routed.sql

        chosen_source = routed.source

        # 3) sandbox (enforce SELECT-only, forbid dangerous, enforce user filter, enforce limit)
        try:
            sand = sanitize_sql(routed.sql, user_id=user.id)
        except SQLSandboxError as e:
            error = f"sandbox: {e}"
            raise HTTPException(status_code=400, detail=f"SQL rejected by sandbox: {e}")

        sandbox_sql = sand.sql
        exec_params = {**sand.params, **routed.params}  # uid + template params

        # ---- canned cache (TTL, in-memory) ----
        cache_key = None
        if routed.source == "canned" and getattr(routed, "intent", None):
            params_key = tuple(sorted((k, str(v)) for k, v in routed.params.items()))
            cache_key = (user.id, routed.intent, params_key)

            cached = _cache_get(cache_key)
            if cached:
                # cache hit: still log via finally
                return ChatResponse(
                    answer=cached["answer"],
                    sql=cached["sql"],
                    source="canned",
                    table=cached["table"],
                )

        # 4) execute
        try:
            columns, rows = _execute_sql(db, sand.sql, exec_params)
        except Exception as e:
            error = f"sql_exec: {e}"
            raise HTTPException(status_code=400, detail=f"SQL execution error: {e}")

        # 5) summarizer
        try:
            target_currency = _detect_target_currency(question)
            answer = _llm_summarize(question, columns, rows, target_currency)
        except Exception as e:
            error = f"summarizer: {e}"
            raise HTTPException(status_code=502, detail="Failed to generate summary")

        table = {"columns": columns, "rows": rows[:50]}
        resp = ChatResponse(
            answer=answer,
            sql=sand.sql,
            source=routed.source,
            table=table,
        )

        # save to cache if canned
        if cache_key is not None:
            _cache_set(cache_key, {"answer": resp.answer, "sql": resp.sql, "table": resp.table})

        return resp

    except HTTPException as e:
        # ensure we log the error detail
        if error is None:
            error = str(e.detail)
        raise

    except Exception as e:
        # unexpected error -> 500, but log the original exception
        if error is None:
            error = f"unhandled: {e}"
        raise HTTPException(status_code=500, detail="Internal error")

    finally:
        latency_ms = int((time.perf_counter() - t0) * 1000)

        # best-effort logging; do not break endpoint if logging fails
        try:
            # important: session may be in failed state after an exception
            try:
                db.rollback()
            except Exception:
                pass

            db.add(
                ChatQuery(
                    user_id=user.id,
                    question=question,
                    chosen_source=chosen_source,
                    generated_sql=generated_sql,
                    sandbox_sql=sandbox_sql,
                    error=error,
                    latency_ms=latency_ms,
                )
            )
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass

