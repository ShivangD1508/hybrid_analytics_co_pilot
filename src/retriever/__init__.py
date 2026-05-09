"""Top-level retriever exports."""

from src.retriever.doc_retriever import DocHit, DocRetriever
from src.retriever.embedder import (
    DOC_COLLECTION,
    REVIEW_COLLECTION,
    DocChunk,
    EmbedStats,
    Embedder,
    build_doc_index,
    build_review_index,
    chunk_markdown,
    collection_counts,
)
from src.retriever.review_retriever import ReviewHit, ReviewRetriever

__all__ = [
    "DOC_COLLECTION",
    "REVIEW_COLLECTION",
    "DocChunk",
    "DocHit",
    "DocRetriever",
    "EmbedStats",
    "Embedder",
    "ReviewHit",
    "ReviewRetriever",
    "build_doc_index",
    "build_review_index",
    "chunk_markdown",
    "collection_counts",
]
