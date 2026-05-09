"""Routing classifier — single LLM call that picks one of sql/docs/reviews/hybrid.

The system prompt is built once at `Router(...)` construction from the live
DB schema and the methodology doc titles. Each `classify(question)` call
sends only the user question, so OpenAI's automatic prompt caching can
amortize the static prefix across calls.

Output is constrained by a JSON schema (OpenAI structured outputs). The
classifier never sees free-form model text — the response is always a
parsed object that matches `RouterDecision`.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence

from openai import OpenAI

from src.config import Config
from src.db.schema import DatabaseSchema, format_schema_for_llm


Route = Literal["sql", "docs", "reviews", "hybrid"]


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DocSummary:
    filename: str
    title: str


@dataclass(frozen=True)
class RouterDecision:
    route: Route
    reasoning: str
    sql_tables_needed: tuple[str, ...] | None
    doc_query: str | None
    review_query: str | None
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    latency_ms: int
    model: str


# ---------------------------------------------------------------------------
# Doc-summary loader
# ---------------------------------------------------------------------------


def load_doc_summaries(docs_dir: Path) -> list[DocSummary]:
    """Read each `*.md` in `docs_dir` and return its filename + first H1 title."""
    out: list[DocSummary] = []
    for p in sorted(docs_dir.glob("*.md")):
        title = p.stem
        with open(p, encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith("# "):
                    title = stripped[2:].strip()
                    break
        out.append(DocSummary(filename=p.name, title=title))
    return out


# ---------------------------------------------------------------------------
# JSON schema for structured outputs
# ---------------------------------------------------------------------------


_RESPONSE_SCHEMA: dict = {
    "name": "router_decision",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "route",
            "reasoning",
            "sql_tables_needed",
            "doc_query",
            "review_query",
        ],
        "properties": {
            "route": {
                "type": "string",
                "enum": ["sql", "docs", "reviews", "hybrid"],
            },
            "reasoning": {"type": "string"},
            "sql_tables_needed": {
                "type": ["array", "null"],
                "items": {"type": "string"},
            },
            "doc_query": {"type": ["string", "null"]},
            "review_query": {"type": ["string", "null"]},
        },
    },
}


# ---------------------------------------------------------------------------
# Few-shot examples
# ---------------------------------------------------------------------------


_FEW_SHOTS: tuple[tuple[str, dict], ...] = (
    # --- sql ---
    (
        "What is the average delivery time for delivered orders?",
        {
            "route": "sql",
            "reasoning": "Pure aggregate over the orders table; no methodology or sentiment needed.",
            "sql_tables_needed": ["orders"],
            "doc_query": None,
            "review_query": None,
        },
    ),
    (
        "What were the top 10 product categories by order count in 2017?",
        {
            "route": "sql",
            "reasoning": "Group-by aggregate joining items to products and the category translation. Time filter on orders.",
            "sql_tables_needed": [
                "orders",
                "order_items",
                "products",
                "product_category_translation",
            ],
            "doc_query": None,
            "review_query": None,
        },
    ),
    (
        "Plot monthly revenue trend over 2017 and 2018.",
        {
            "route": "sql",
            "reasoning": "Time series aggregate; revenue from order_items grouped by month of order_purchase_timestamp.",
            "sql_tables_needed": ["orders", "order_items"],
            "doc_query": None,
            "review_query": None,
        },
    ),
    (
        "How many sellers are based in São Paulo state?",
        {
            "route": "sql",
            "reasoning": "Filtered count on the sellers table.",
            "sql_tables_needed": ["sellers"],
            "doc_query": None,
            "review_query": None,
        },
    ),
    # --- docs ---
    (
        "How do we calculate customer lifetime value?",
        {
            "route": "docs",
            "reasoning": "Asks for a formula/methodology, not data.",
            "sql_tables_needed": None,
            "doc_query": "customer lifetime value CLV formula calculation",
            "review_query": None,
        },
    ),
    (
        "What is the difference between a Champion and a Loyal customer in our segmentation?",
        {
            "route": "docs",
            "reasoning": "Asks for segment definitions; answer lives in segment_definitions playbook.",
            "sql_tables_needed": None,
            "doc_query": "customer segments Champions Loyal definition criteria",
            "review_query": None,
        },
    ),
    (
        "How is the on-time delivery SLA defined?",
        {
            "route": "docs",
            "reasoning": "Definition question; no data needed.",
            "sql_tables_needed": None,
            "doc_query": "on-time delivery SLA definition estimated date",
            "review_query": None,
        },
    ),
    # --- reviews ---
    (
        "What are customers saying about furniture quality?",
        {
            "route": "reviews",
            "reasoning": "Asks for qualitative sentiment from review text; no aggregation requested.",
            "sql_tables_needed": None,
            "doc_query": None,
            "review_query": "furniture quality complaints damage",
        },
    ),
    (
        "Show me complaints about late deliveries.",
        {
            "route": "reviews",
            "reasoning": "Sentiment lookup against review text.",
            "sql_tables_needed": None,
            "doc_query": None,
            "review_query": "late delivery delay shipping",
        },
    ),
    # --- hybrid ---
    (
        "Why are electronics reviews lower than the global average?",
        {
            "route": "hybrid",
            "reasoning": "Need SQL for avg review_score by category to confirm the gap, plus review samples to explain it.",
            "sql_tables_needed": [
                "order_reviews",
                "order_items",
                "products",
                "product_category_translation",
            ],
            "doc_query": "review analysis red flags interpretation",
            "review_query": "electronics defective broken quality",
        },
    ),
    (
        "Which customer segments are most at risk of churning?",
        {
            "route": "hybrid",
            "reasoning": "Need segment definitions from the docs and segment population counts from the data.",
            "sql_tables_needed": ["customers", "orders"],
            "doc_query": "customer segments At-Risk Lost churn definition",
            "review_query": None,
        },
    ),
    (
        "Are northern-state customers more dissatisfied than southeastern ones, and if so why?",
        {
            "route": "hybrid",
            "reasoning": "Need SQL for avg review_score by customer_state plus review samples for context. Methodology context on review interpretation also helps.",
            "sql_tables_needed": ["order_reviews", "orders", "customers"],
            "doc_query": "review analysis geographic delivery interpretation",
            "review_query": "delivery experience late state",
        },
    ),
)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class Router:
    """Classifies user questions into one of four routes via a single LLM call."""

    def __init__(
        self,
        schema: DatabaseSchema,
        docs: Sequence[DocSummary],
        config: Config,
        client: OpenAI | None = None,
    ) -> None:
        self._config = config
        self._client = client or OpenAI(api_key=config.openai_api_key)
        self._system_prompt = self._build_system_prompt(schema, docs)

    # ----- prompt construction -----

    @staticmethod
    def _build_system_prompt(
        schema: DatabaseSchema, docs: Sequence[DocSummary]
    ) -> str:
        schema_block = format_schema_for_llm(schema).rstrip()
        doc_block = "\n".join(f"- {d.filename}: {d.title}" for d in docs)
        examples_block = "\n\n".join(
            f"Q: {q}\nA: {json.dumps(a, ensure_ascii=False)}"
            for q, a in _FEW_SHOTS
        )

        return _SYSTEM_PROMPT_TEMPLATE.format(
            schema_block=schema_block,
            doc_block=doc_block,
            examples_block=examples_block,
        )

    # ----- public API -----

    def classify(self, question: str) -> RouterDecision:
        """Run one classification call. Raises on unrecoverable model error."""
        if not question or not question.strip():
            raise ValueError("question must be non-empty")

        t0 = time.perf_counter()
        resp = self._client.chat.completions.create(
            model=self._config.chat_model,
            messages=[
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": question.strip()},
            ],
            response_format={"type": "json_schema", "json_schema": _RESPONSE_SCHEMA},
            temperature=0,
        )
        latency_ms = int((time.perf_counter() - t0) * 1000)

        content = resp.choices[0].message.content or "{}"
        parsed = json.loads(content)
        usage = resp.usage
        cached = 0
        if usage and getattr(usage, "prompt_tokens_details", None):
            cached = getattr(usage.prompt_tokens_details, "cached_tokens", 0) or 0

        sql_tables = parsed.get("sql_tables_needed")
        return RouterDecision(
            route=parsed["route"],
            reasoning=parsed["reasoning"],
            sql_tables_needed=tuple(sql_tables) if sql_tables else None,
            doc_query=parsed.get("doc_query"),
            review_query=parsed.get("review_query"),
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            cached_tokens=cached,
            latency_ms=latency_ms,
            model=resp.model,
        )

    # ----- introspection helpers -----

    @property
    def system_prompt(self) -> str:
        """Exposed for debugging — the full system prompt sent on every call."""
        return self._system_prompt


_SYSTEM_PROMPT_TEMPLATE = """\
You are the routing layer of an analytics agent over the Olist Brazilian
e-commerce dataset. Your only job is to classify each user question into
exactly one route.

ROUTES
- sql: answerable from the database alone. Counts, aggregates, trends,
  group-bys, time filters. No methodology or sentiment needed.
- docs: asks for a definition, formula, methodology, or how-to. Answer is
  in the internal analytics playbooks listed below.
- reviews: asks about customer sentiment, opinions, complaints, or themes
  in customer review text.
- hybrid: requires two or more of the above. Use this when the question
  asks WHY something looks the way it does in the data, when it pairs a
  metric with sentiment, or when it needs both methodology context and
  data to answer.

Heuristics:
- Prefer sql over hybrid if the question is purely quantitative.
- Prefer hybrid over sql if the question asks "why" or "explain".
- If a question can be answered by sampling review text without any
  aggregation, route to reviews. If it needs both an aggregate AND
  sentiment color, route to hybrid.

DATABASE SCHEMA
{schema_block}

AVAILABLE PLAYBOOK DOCS
{doc_block}

OUTPUT
Return JSON matching the response schema. Set fields to null when their
tool is not needed:
- sql_tables_needed: list every table you would touch (including join
  tables) for sql / hybrid, otherwise null.
- doc_query: a 3-8 word search string for the docs vector store, or null.
- review_query: a 2-6 word search string for the reviews vector store,
  or null. Use English even though reviews are Portuguese — embeddings
  bridge the two.

FEW-SHOT EXAMPLES
{examples_block}
"""
