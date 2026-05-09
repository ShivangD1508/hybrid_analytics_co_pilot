"""End-to-end orchestrator: question -> answer.

A `Pipeline` instance holds long-lived components (schema, router,
SQL generator, retrievers, answer generator) so each `run(question)`
call only does the per-question work.

Per-stage latency and token usage are recorded on the way through; the
returned `PipelineResult` carries the answer plus everything needed to
reconstruct what happened, so the UI can show a transparent reasoning
chain and the eval harness can attribute failures to a specific stage.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

import pandas as pd

from src.config import Config
from src.db.schema import DatabaseSchema, introspect_schema
from src.retriever import (
    DocHit,
    DocRetriever,
    Embedder,
    ReviewHit,
    ReviewRetriever,
    collection_counts,
)
from src.router.classifier import (
    Route,
    Router,
    RouterDecision,
    load_doc_summaries,
)
from src.sql_agent import SqlAgentRun, SqlGenerator, run_sql_for_question
from src.synthesizer import (
    AnswerGenerator,
    ChartSpec,
    Source,
    SynthesisResult,
    synthesize,
)


Stage = Literal["router", "sql", "docs", "reviews", "synthesis"]


@dataclass(frozen=True)
class StageTiming:
    stage: Stage
    latency_ms: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0


@dataclass(frozen=True)
class PipelineResult:
    question: str
    route: Route
    answer: str
    confidence: float
    reasoning_chain: list[str]
    chart_spec: ChartSpec
    chart_data: dict
    sources: list[Source]

    # Inspection handles for the UI / eval harness.
    router_decision: RouterDecision
    sql_run: SqlAgentRun | None
    doc_hits: list[DocHit]
    review_hits: list[ReviewHit]
    df: pd.DataFrame | None

    # Aggregates.
    timings: list[StageTiming]
    total_ms: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_cached_tokens: int


class Pipeline:
    """One per process. Holds the long-lived components."""

    def __init__(self, config: Config) -> None:
        self._config = config

        if not config.sqlite_path.exists():
            raise RuntimeError(
                f"SQLite database missing at {config.sqlite_path}. "
                f"Run scripts/1_load_data.py first."
            )

        counts = collection_counts(config.chroma_dir)
        if counts["methodology_docs"] == 0 or counts["customer_reviews"] == 0:
            raise RuntimeError(
                f"ChromaDB collections empty under {config.chroma_dir}. "
                f"Run scripts/3_embed.py first. Current counts: {counts}"
            )

        schema: DatabaseSchema = introspect_schema(config.sqlite_path)
        docs_summaries = load_doc_summaries(config.repo_root / "data" / "docs")

        self._embedder = Embedder(config=config)
        self._router = Router(schema=schema, docs=docs_summaries, config=config)
        self._sql_gen = SqlGenerator(schema=schema, config=config)
        self._doc_retriever = DocRetriever(
            chroma_dir=config.chroma_dir, embedder=self._embedder
        )
        self._review_retriever = ReviewRetriever(
            chroma_dir=config.chroma_dir, embedder=self._embedder
        )
        self._answer_gen = AnswerGenerator(config=config)

    # ----- public API -----

    def run(self, question: str) -> PipelineResult:
        if not question or not question.strip():
            raise ValueError("question must be non-empty")

        t_total = time.perf_counter()
        timings: list[StageTiming] = []

        # 1. Router.
        decision = self._router.classify(question)
        timings.append(
            StageTiming(
                stage="router",
                latency_ms=decision.latency_ms,
                prompt_tokens=decision.prompt_tokens,
                completion_tokens=decision.completion_tokens,
                cached_tokens=decision.cached_tokens,
            )
        )

        sql_run: SqlAgentRun | None = None
        doc_hits: list[DocHit] = []
        review_hits: list[ReviewHit] = []

        # 2. Conditional dispatch.
        if decision.route in ("sql", "hybrid"):
            sql_run, sql_timing = self._run_sql(question)
            timings.append(sql_timing)

        if decision.route in ("docs", "hybrid") and decision.doc_query:
            doc_hits, doc_timing = self._run_docs(decision.doc_query)
            timings.append(doc_timing)

        if decision.route in ("reviews", "hybrid") and decision.review_query:
            review_hits, rev_timing = self._run_reviews(decision.review_query)
            timings.append(rev_timing)

        # 3. Synthesis.
        synthesis: SynthesisResult = synthesize(
            question=question,
            router_decision=decision,
            sql_run=sql_run,
            doc_hits=doc_hits or None,
            review_hits=review_hits or None,
            config=self._config,
            generator=self._answer_gen,
        )
        timings.append(
            StageTiming(
                stage="synthesis",
                latency_ms=synthesis.latency_ms,
                prompt_tokens=synthesis.prompt_tokens,
                completion_tokens=synthesis.completion_tokens,
                cached_tokens=synthesis.cached_tokens,
            )
        )

        df = (
            sql_run.result.df
            if sql_run is not None and sql_run.result is not None
            else None
        )

        total_ms = int((time.perf_counter() - t_total) * 1000)
        total_prompt = sum(t.prompt_tokens for t in timings)
        total_completion = sum(t.completion_tokens for t in timings)
        total_cached = sum(t.cached_tokens for t in timings)

        return PipelineResult(
            question=question,
            route=decision.route,
            answer=synthesis.answer,
            confidence=synthesis.confidence,
            reasoning_chain=synthesis.reasoning_chain,
            chart_spec=synthesis.chart_spec,
            chart_data=synthesis.chart_data,
            sources=synthesis.sources,
            router_decision=decision,
            sql_run=sql_run,
            doc_hits=doc_hits,
            review_hits=review_hits,
            df=df,
            timings=timings,
            total_ms=total_ms,
            total_prompt_tokens=total_prompt,
            total_completion_tokens=total_completion,
            total_cached_tokens=total_cached,
        )

    # ----- per-stage runners -----

    def _run_sql(self, question: str) -> tuple[SqlAgentRun, StageTiming]:
        t0 = time.perf_counter()
        run = run_sql_for_question(question, self._sql_gen, self._config)
        latency_ms = int((time.perf_counter() - t0) * 1000)

        prompt = run.generation.prompt_tokens
        completion = run.generation.completion_tokens
        cached = run.generation.cached_tokens
        # Note: if the agent retried, only the second generation is on
        # `run.generation`. The first attempt's tokens are lost in the orchestrator
        # but its latency is reflected in the wall clock.

        return run, StageTiming(
            stage="sql",
            latency_ms=latency_ms,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cached_tokens=cached,
        )

    def _run_docs(self, query: str) -> tuple[list[DocHit], StageTiming]:
        t0 = time.perf_counter()
        hits = self._doc_retriever.search(query, top_k=self._config.retriever_top_k)
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return hits, StageTiming(stage="docs", latency_ms=latency_ms)

    def _run_reviews(self, query: str) -> tuple[list[ReviewHit], StageTiming]:
        t0 = time.perf_counter()
        hits = self._review_retriever.search(
            query, top_k=self._config.retriever_top_k
        )
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return hits, StageTiming(stage="reviews", latency_ms=latency_ms)
