"""Search the methodology_docs ChromaDB collection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import chromadb

from src.retriever.embedder import DOC_COLLECTION, Embedder


@dataclass(frozen=True)
class DocHit:
    chunk_id: str
    text: str
    filename: str
    title: str
    chunk_index: int
    distance: float


class DocRetriever:
    """Wraps the methodology_docs collection. Embeds the query, returns top-k chunks."""

    def __init__(self, chroma_dir: Path, embedder: Embedder) -> None:
        client = chromadb.PersistentClient(path=str(chroma_dir))
        self._collection = client.get_collection(name=DOC_COLLECTION)
        self._embedder = embedder

    def search(self, query: str, top_k: int = 5) -> list[DocHit]:
        if not query or not query.strip():
            return []
        embedding = self._embedder.embed_one(query.strip())
        result = self._collection.query(
            query_embeddings=[embedding],
            n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )
        return _parse_results(result)

    def count(self) -> int:
        return self._collection.count()


def _parse_results(result: dict) -> list[DocHit]:
    ids = result["ids"][0]
    docs = result["documents"][0]
    metas = result["metadatas"][0]
    dists = result["distances"][0]
    out: list[DocHit] = []
    for chunk_id, text, meta, dist in zip(ids, docs, metas, dists, strict=True):
        out.append(
            DocHit(
                chunk_id=chunk_id,
                text=text,
                filename=meta.get("filename", ""),
                title=meta.get("title", ""),
                chunk_index=int(meta.get("chunk_index", 0)),
                distance=float(dist),
            )
        )
    return out
