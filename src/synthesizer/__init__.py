"""Synthesis layer: combine SQL + docs + reviews into a single answer.

The pipeline (Step 8) calls `synthesize(...)` after the router, the SQL
agent, and the retrievers have run. This module owns:

- `Source` types (`SqlSource`, `DocSource`, `ReviewSource`) -- typed
  citations attached to the result.
- `SynthesisResult` -- the end-to-end output the UI renders.
- `synthesize(...)` -- orchestrator that runs the chart selector, builds
  source citations, calls the LLM answer generator, and stitches a
  reasoning chain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

from src.config import Config
from src.retriever import DocHit, ReviewHit
from src.router.classifier import RouterDecision
from src.sql_agent import SqlAgentRun
from src.synthesizer.answer_generator import (
    AnswerDraft,
    AnswerGenerator,
    render_user_message_for_inspection,
)
from src.synthesizer.chart_selector import (
    ChartSpec,
    ChartType,
    select_chart,
    to_chart_data,
)


# ---------------------------------------------------------------------------
# Source types (typed discriminated union)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SqlSource:
    query: str
    rows: int
    columns: tuple[str, ...]
    type: Literal["sql"] = "sql"


@dataclass(frozen=True)
class DocSource:
    filename: str
    title: str
    chunk_index: int
    text: str
    distance: float
    type: Literal["doc"] = "doc"


@dataclass(frozen=True)
class ReviewSource:
    review_id: str
    order_id: str
    score: int
    category: str
    text: str
    distance: float
    type: Literal["review"] = "review"


Source = SqlSource | DocSource | ReviewSource


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SynthesisResult:
    answer: str
    confidence: float
    reasoning_chain: list[str]
    chart_spec: ChartSpec
    chart_data: dict
    sources: list[Source]
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    latency_ms: int
    model: str


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def synthesize(
    question: str,
    router_decision: RouterDecision,
    sql_run: SqlAgentRun | None,
    doc_hits: list[DocHit] | None,
    review_hits: list[ReviewHit] | None,
    config: Config,
    generator: AnswerGenerator | None = None,
) -> SynthesisResult:
    """Combine SQL + docs + reviews into a single SynthesisResult."""
    gen = generator or AnswerGenerator(config=config)

    df = (
        sql_run.result.df
        if sql_run is not None and sql_run.result is not None
        else None
    )
    chart_spec = select_chart(df)
    chart_data = to_chart_data(df, chart_spec) if df is not None else {}

    draft = gen.generate(
        question=question,
        router_decision=router_decision,
        sql_run=sql_run,
        doc_hits=doc_hits,
        review_hits=review_hits,
    )

    sources = _build_sources(sql_run, doc_hits, review_hits)
    reasoning_chain = _build_reasoning_chain(
        router_decision, sql_run, doc_hits, review_hits, draft, chart_spec
    )

    return SynthesisResult(
        answer=draft.answer,
        confidence=draft.confidence,
        reasoning_chain=reasoning_chain,
        chart_spec=chart_spec,
        chart_data=chart_data,
        sources=sources,
        prompt_tokens=draft.prompt_tokens,
        completion_tokens=draft.completion_tokens,
        cached_tokens=draft.cached_tokens,
        latency_ms=draft.latency_ms,
        model=draft.model,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_sources(
    sql_run: SqlAgentRun | None,
    doc_hits: list[DocHit] | None,
    review_hits: list[ReviewHit] | None,
) -> list[Source]:
    sources: list[Source] = []
    if sql_run is not None and sql_run.validation.passed and sql_run.result is not None:
        r = sql_run.result
        sources.append(
            SqlSource(
                query=sql_run.validation.normalized_sql,
                rows=r.row_count,
                columns=r.columns,
            )
        )
    for h in doc_hits or []:
        sources.append(
            DocSource(
                filename=h.filename,
                title=h.title,
                chunk_index=h.chunk_index,
                text=h.text,
                distance=h.distance,
            )
        )
    for h in review_hits or []:
        sources.append(
            ReviewSource(
                review_id=h.review_id,
                order_id=h.order_id,
                score=h.review_score,
                category=h.product_category,
                text=h.text,
                distance=h.distance,
            )
        )
    return sources


def _build_reasoning_chain(
    router_decision: RouterDecision,
    sql_run: SqlAgentRun | None,
    doc_hits: list[DocHit] | None,
    review_hits: list[ReviewHit] | None,
    draft: AnswerDraft,
    chart_spec: ChartSpec,
) -> list[str]:
    chain: list[str] = []
    chain.append(
        f"Router routed to '{router_decision.route}': {router_decision.reasoning}"
    )

    if sql_run is not None:
        g = sql_run.generation
        v = sql_run.validation
        retried = " (retried after validation failure)" if sql_run.retried else ""
        if not v.passed:
            chain.append(f"SQL generation{retried}: validation FAILED -- {v.error}")
        elif sql_run.result is None:
            chain.append(f"SQL generation{retried}: validated but not executed")
        elif sql_run.result.error:
            chain.append(
                f"SQL generation{retried}: executed with error -- {sql_run.result.error}"
            )
        else:
            chain.append(
                f"SQL generation{retried}: {sql_run.result.row_count} rows, "
                f"{sql_run.result.execution_ms}ms -- {g.explanation}"
            )

    if doc_hits is not None:
        if doc_hits:
            files = sorted({h.filename for h in doc_hits})
            chain.append(
                f"Retrieved {len(doc_hits)} doc chunk(s) from {len(files)} file(s): "
                f"{', '.join(files)}"
            )
        else:
            chain.append("No doc chunks retrieved (query returned no hits)")

    if review_hits is not None:
        if review_hits:
            scores = sorted({h.review_score for h in review_hits})
            cats = sorted({h.product_category for h in review_hits if h.product_category})
            cat_part = f", categories: {', '.join(cats)}" if cats else ""
            chain.append(
                f"Retrieved {len(review_hits)} review(s), scores {scores}{cat_part}"
            )
        else:
            chain.append("No reviews retrieved (query returned no hits)")

    chain.append(
        f"Chart selector: {chart_spec.chart_type} ({chart_spec.rationale})"
    )
    chain.append(f"Synthesizer: {draft.reasoning_summary}")
    return chain


__all__ = [
    "AnswerDraft",
    "AnswerGenerator",
    "ChartSpec",
    "ChartType",
    "DocSource",
    "ReviewSource",
    "Source",
    "SqlSource",
    "SynthesisResult",
    "render_user_message_for_inspection",
    "select_chart",
    "synthesize",
    "to_chart_data",
]


# Quiet pyflakes for pandas import in type annotations.
_ = pd
