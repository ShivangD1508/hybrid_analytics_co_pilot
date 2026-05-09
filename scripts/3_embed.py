"""Build the two ChromaDB collections: methodology_docs and customer_reviews.

Usage:
    python scripts/3_embed.py                  # build only what's missing
    python scripts/3_embed.py --rebuild        # drop and rebuild both
    python scripts/3_embed.py --docs-only      # docs collection only
    python scripts/3_embed.py --reviews-only   # reviews collection only
    python scripts/3_embed.py --review-limit 500   # dev: small reviews subset

Cost estimate at full size (~41K reviews, ~60 doc chunks):
- text-embedding-3-small at $0.02 / 1M tokens
- ~2 - 3M total input tokens, so ~$0.04 - $0.06 one-time.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from src.config import load_config
from src.retriever import (
    Embedder,
    build_doc_index,
    build_review_index,
    collection_counts,
)


def _make_progress(label: str):
    last_pct = -1

    def _cb(done: int, total: int) -> None:
        nonlocal last_pct
        pct = int(100 * done / total) if total else 100
        if pct != last_pct and (pct % 5 == 0 or done == total):
            print(f"  {label}: {done:>6,}/{total:>6,}  ({pct:>3}%)")
            last_pct = pct

    return _cb


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--rebuild", action="store_true", help="Drop existing collections and rebuild.")
    parser.add_argument("--docs-only", action="store_true")
    parser.add_argument("--reviews-only", action="store_true")
    parser.add_argument(
        "--review-limit",
        type=int,
        default=None,
        help="Cap reviews ingestion (development only).",
    )
    args = parser.parse_args()

    if args.docs_only and args.reviews_only:
        print("--docs-only and --reviews-only are mutually exclusive", file=sys.stderr)
        return 2

    cfg = load_config()
    print(f"Chroma dir : {cfg.chroma_dir}")
    print(f"Embed model: {cfg.embed_model}")
    print(f"Existing   : {collection_counts(cfg.chroma_dir)}")

    embedder = Embedder(config=cfg)
    t0 = time.perf_counter()

    if not args.reviews_only:
        print("\nBuilding methodology_docs ...")
        stats = build_doc_index(
            chroma_dir=cfg.chroma_dir,
            docs_dir=cfg.repo_root / "data" / "docs",
            embedder=embedder,
            replace=args.rebuild,
            on_progress=_make_progress("docs"),
        )
        print(
            f"  -> chunks={stats['chunks']}  skipped={stats['skipped']}  "
            f"tokens={stats['tokens']:,}  {stats['seconds']:.1f}s"
        )

    if not args.docs_only:
        print("\nBuilding customer_reviews ...")
        stats = build_review_index(
            chroma_dir=cfg.chroma_dir,
            sqlite_path=cfg.sqlite_path,
            embedder=embedder,
            replace=args.rebuild,
            on_progress=_make_progress("reviews"),
            limit=args.review_limit,
        )
        print(
            f"  -> reviews={stats['reviews']}  skipped={stats['skipped']}  "
            f"tokens={stats['tokens']:,}  {stats['seconds']:.1f}s"
        )

    elapsed = time.perf_counter() - t0
    print(f"\nFinal counts: {collection_counts(cfg.chroma_dir)}")
    print(f"Total time  : {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
