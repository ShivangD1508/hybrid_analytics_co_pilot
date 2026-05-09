"""Smoke-test the doc and review retrievers.

Reads OPENAI_API_KEY from .env. Assumes both ChromaDB collections have
already been built by `scripts/3_embed.py`.

Default battery covers methodology lookups and three flavors of review
search: open query, score-filtered, and category-filtered.

Usage:
    python scripts/test_retriever.py
    python scripts/test_retriever.py --doc-query "RFM scoring formula"
    python scripts/test_retriever.py --review-query "broken on arrival" --score 1
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from src.config import load_config
from src.retriever import (
    DocRetriever,
    Embedder,
    ReviewRetriever,
    collection_counts,
)


def _trunc(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 3] + "..."


def _print_doc_hits(query: str, hits) -> None:
    print(f"\n# DOC QUERY: {query}")
    if not hits:
        print("  (no hits)")
        return
    for i, h in enumerate(hits, 1):
        print(f"  [{i}] dist={h.distance:.3f}  {h.filename}#chunk-{h.chunk_index}")
        print(f"      {_trunc(h.text, 200)}")


def _print_review_hits(query: str, hits) -> None:
    print(f"\n# REVIEW QUERY: {query}")
    if not hits:
        print("  (no hits)")
        return
    for i, h in enumerate(hits, 1):
        print(
            f"  [{i}] dist={h.distance:.3f}  score={h.review_score}  "
            f"category={h.product_category or '(none)'}  date={h.review_creation_date[:10]}"
        )
        print(f"      {_trunc(h.text, 220)}")


_DOC_BATTERY: list[str] = [
    "How do we calculate customer lifetime value?",
    "What is the seller performance score formula?",
    "What is the on-time delivery SLA definition?",
    "Customer segments Champions Loyal definition",
]


_REVIEW_BATTERY: list[tuple[str, dict]] = [
    ("late delivery never arrived", {}),
    ("broken damaged product", {"score": 2, "score_op": "lte"}),
    ("very satisfied recommend", {"score": 5}),
    ("packaging quality", {}),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--doc-query", help="Run a single ad-hoc doc query.")
    parser.add_argument("--review-query", help="Run a single ad-hoc review query.")
    parser.add_argument("--score", type=int, default=None, help="Filter reviews by score.")
    parser.add_argument(
        "--score-op",
        choices=["eq", "lte", "gte"],
        default="eq",
        help="Comparison operator for --score.",
    )
    parser.add_argument("--category", help="Filter reviews by English category.")
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    cfg = load_config()
    counts = collection_counts(cfg.chroma_dir)
    print(f"Collection sizes: {counts}")
    if counts.get("methodology_docs", 0) == 0 or counts.get("customer_reviews", 0) == 0:
        print("ERROR: collections are empty. Run scripts/3_embed.py first.", file=sys.stderr)
        return 2

    embedder = Embedder(config=cfg)
    docs = DocRetriever(chroma_dir=cfg.chroma_dir, embedder=embedder)
    reviews = ReviewRetriever(chroma_dir=cfg.chroma_dir, embedder=embedder)

    if args.doc_query:
        _print_doc_hits(args.doc_query, docs.search(args.doc_query, top_k=args.top_k))
        return 0
    if args.review_query:
        kw = {}
        if args.score is not None:
            kw["score"] = args.score
            kw["score_op"] = args.score_op
        if args.category:
            kw["category"] = args.category
        _print_review_hits(
            args.review_query,
            reviews.search(args.review_query, top_k=args.top_k, **kw),
        )
        return 0

    print("\n== Doc retriever battery ==")
    for q in _DOC_BATTERY:
        _print_doc_hits(q, docs.search(q, top_k=args.top_k))

    print("\n\n== Review retriever battery ==")
    for q, kw in _REVIEW_BATTERY:
        label = q + (f" [filters={kw}]" if kw else "")
        _print_review_hits(label, reviews.search(q, top_k=args.top_k, **kw))

    return 0


if __name__ == "__main__":
    sys.exit(main())
