"""Search the customer_reviews ChromaDB collection.

Supports optional metadata filters on `review_score` (exact, lte, or gte)
and `product_category` (exact match against the English category name).
The agent uses these to ask things like "complaints about electronics",
"low-score reviews about packaging", etc.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import chromadb

from src.retriever.embedder import REVIEW_COLLECTION, Embedder


@dataclass(frozen=True)
class ReviewHit:
    review_id: str
    order_id: str
    text: str
    review_score: int
    product_category: str
    review_creation_date: str
    distance: float


ScoreOp = Literal["eq", "lte", "gte"]


class ReviewRetriever:
    """Wraps the customer_reviews collection. Embeds the query, returns top-k reviews."""

    def __init__(self, chroma_dir: Path, embedder: Embedder) -> None:
        client = chromadb.PersistentClient(path=str(chroma_dir))
        self._collection = client.get_collection(name=REVIEW_COLLECTION)
        self._embedder = embedder

    def search(
        self,
        query: str,
        top_k: int = 5,
        *,
        score: int | None = None,
        score_op: ScoreOp = "eq",
        category: str | None = None,
    ) -> list[ReviewHit]:
        if not query or not query.strip():
            return []
        where = self._build_filter(score=score, score_op=score_op, category=category)
        embedding = self._embedder.embed_one(query.strip())
        result = self._collection.query(
            query_embeddings=[embedding],
            n_results=top_k,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        return _parse_results(result)

    def count(self) -> int:
        return self._collection.count()

    @staticmethod
    def _build_filter(
        score: int | None, score_op: ScoreOp, category: str | None
    ) -> dict | None:
        clauses: list[dict] = []
        if score is not None:
            op_map = {"eq": "$eq", "lte": "$lte", "gte": "$gte"}
            clauses.append({"review_score": {op_map[score_op]: int(score)}})
        if category:
            clauses.append({"product_category": {"$eq": category}})
        if not clauses:
            return None
        if len(clauses) == 1:
            return clauses[0]
        return {"$and": clauses}


def _parse_results(result: dict) -> list[ReviewHit]:
    ids = result["ids"][0]
    docs = result["documents"][0]
    metas = result["metadatas"][0]
    dists = result["distances"][0]
    out: list[ReviewHit] = []
    for _id, text, meta, dist in zip(ids, docs, metas, dists, strict=True):
        out.append(
            ReviewHit(
                review_id=meta.get("review_id", ""),
                order_id=meta.get("order_id", ""),
                text=text,
                review_score=int(meta.get("review_score", 0)),
                product_category=meta.get("product_category", "") or "",
                review_creation_date=meta.get("review_creation_date", "") or "",
                distance=float(dist),
            )
        )
    return out
