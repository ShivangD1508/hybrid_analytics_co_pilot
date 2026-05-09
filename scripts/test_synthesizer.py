"""Smoke-test the synthesizer end-to-end.

Runs the router, dispatches to SQL agent / doc retriever / review retriever
based on the route, then synthesizes a final answer. This previews what
Step 8 (pipeline.py) will formalize.

Usage:
    python scripts/test_synthesizer.py
    python scripts/test_synthesizer.py "your question here"
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from src.config import load_config
from src.db.schema import introspect_schema
from src.retriever import DocRetriever, Embedder, ReviewRetriever
from src.router.classifier import Router, load_doc_summaries
from src.sql_agent import SqlGenerator, run_sql_for_question
from src.synthesizer import synthesize


_BATTERY: list[str] = [
    "What was the total revenue across all orders in 2017?",                # sql
    "How is the seller performance score calculated?",                      # docs
    "Why are northern Brazilian states slower to receive their orders?",    # hybrid
]


def _print_header(label: str, q: str) -> None:
    print()
    print("=" * 78)
    print(f"[{label}]  {q}")
    print("=" * 78)


def _wrap(s: str, width: int = 90, indent: str = "  ") -> str:
    return textwrap.fill(
        s,
        width=width,
        initial_indent=indent,
        subsequent_indent=indent,
        break_long_words=False,
        break_on_hyphens=False,
    )


def main() -> int:
    cfg = load_config()
    schema = introspect_schema(cfg.sqlite_path)
    docs_summaries = load_doc_summaries(cfg.repo_root / "data" / "docs")

    embedder = Embedder(config=cfg)
    router = Router(schema=schema, docs=docs_summaries, config=cfg)
    sql_gen = SqlGenerator(schema=schema, config=cfg)
    doc_ret = DocRetriever(chroma_dir=cfg.chroma_dir, embedder=embedder)
    rev_ret = ReviewRetriever(chroma_dir=cfg.chroma_dir, embedder=embedder)

    questions = [" ".join(sys.argv[1:])] if len(sys.argv) > 1 else _BATTERY

    grand_total_tokens = 0
    grand_total_ms = 0
    for q in questions:
        _print_header("Q", q)

        decision = router.classify(q)
        print(f"\n-- ROUTER --")
        print(f"  route   : {decision.route}")
        print(f"  reason  : {_wrap(decision.reasoning).lstrip()}")
        if decision.sql_tables_needed:
            print(f"  tables  : {list(decision.sql_tables_needed)}")
        if decision.doc_query:
            print(f"  doc q   : {decision.doc_query}")
        if decision.review_query:
            print(f"  rev q   : {decision.review_query}")

        sql_run = None
        doc_hits = None
        review_hits = None

        if decision.route in ("sql", "hybrid"):
            sql_run = run_sql_for_question(q, sql_gen, cfg)
            print(f"\n-- SQL --")
            print(f"  validation: passed={sql_run.validation.passed}"
                  f" retried={sql_run.retried}")
            if sql_run.result is not None and sql_run.result.error is None:
                print(f"  result    : {sql_run.result.row_count} rows, "
                      f"{sql_run.result.execution_ms}ms")
                if sql_run.result.df is not None and sql_run.result.row_count > 0:
                    preview = sql_run.result.df.head(5).to_string(index=False)
                    for line in preview.splitlines():
                        print(f"    {line}")
            elif sql_run.result is not None:
                print(f"  result    : ERROR {sql_run.result.error}")

        if decision.route in ("docs", "hybrid") and decision.doc_query:
            doc_hits = doc_ret.search(decision.doc_query, top_k=cfg.retriever_top_k)
            print(f"\n-- DOC HITS ({len(doc_hits)}) --")
            for i, h in enumerate(doc_hits, 1):
                print(f"  [{i}] {h.filename}#chunk-{h.chunk_index}  dist={h.distance:.3f}")

        if decision.route in ("reviews", "hybrid") and decision.review_query:
            review_hits = rev_ret.search(decision.review_query, top_k=cfg.retriever_top_k)
            print(f"\n-- REVIEW HITS ({len(review_hits)}) --")
            for i, h in enumerate(review_hits, 1):
                short = (h.order_id or "")[:8]
                print(
                    f"  [{i}] [review:{short}]  score={h.review_score}  "
                    f"cat={h.product_category or '(none)'}  dist={h.distance:.3f}"
                )

        result = synthesize(
            question=q,
            router_decision=decision,
            sql_run=sql_run,
            doc_hits=doc_hits,
            review_hits=review_hits,
            config=cfg,
        )

        print(f"\n-- ANSWER (confidence {result.confidence:.2f}) --")
        for line in textwrap.wrap(
            result.answer, width=90, replace_whitespace=False, drop_whitespace=False
        ):
            print(f"  {line}")

        print(f"\n-- REASONING CHAIN --")
        for i, step in enumerate(result.reasoning_chain, 1):
            print(f"  {i}. {_wrap(step).lstrip()}")

        print(f"\n-- CHART --")
        print(f"  type      : {result.chart_spec.chart_type}")
        print(f"  rationale : {result.chart_spec.rationale}")
        if result.chart_spec.x_column or result.chart_spec.y_column:
            print(f"  columns   : x={result.chart_spec.x_column}  y={result.chart_spec.y_column}")

        print(f"\n-- COSTS --")
        synth_total = result.prompt_tokens + result.completion_tokens
        print(
            f"  synthesizer: prompt={result.prompt_tokens} (cached={result.cached_tokens}) "
            f"completion={result.completion_tokens}  latency={result.latency_ms}ms"
        )
        print(f"  sources    : {len(result.sources)} ({_summarize_sources(result.sources)})")
        grand_total_tokens += synth_total
        grand_total_ms += result.latency_ms

    print()
    print("=" * 78)
    print(f"Aggregate: {grand_total_tokens:,} synthesis tokens, "
          f"{grand_total_ms:,}ms total synth latency over {len(questions)} questions")
    return 0


def _summarize_sources(sources) -> str:
    counts: dict[str, int] = {}
    for s in sources:
        counts[s.type] = counts.get(s.type, 0) + 1
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))


if __name__ == "__main__":
    sys.exit(main())
