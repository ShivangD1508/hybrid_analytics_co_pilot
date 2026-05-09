"""OpenAI embedding wrapper + ingestion pipelines for both ChromaDB collections.

`Embedder` batches API calls and retries on transient rate-limit / network
errors. `build_doc_index` and `build_review_index` use it to populate two
persistent collections under `<chroma_dir>/`. Re-running with `replace=True`
drops the existing collection first; otherwise the build is a no-op when
the collection's row count already matches the input.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import chromadb
from openai import APIError, OpenAI, RateLimitError

from src.config import Config
from src.router.classifier import load_doc_summaries  # title-from-H1 helper


DOC_COLLECTION = "methodology_docs"
REVIEW_COLLECTION = "customer_reviews"


# ---------------------------------------------------------------------------
# Embedder
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmbedStats:
    n_inputs: int
    n_batches: int
    total_tokens: int
    elapsed_seconds: float


class Embedder:
    """Thin OpenAI embedding client with batching and bounded retries."""

    def __init__(
        self,
        config: Config,
        client: OpenAI | None = None,
        batch_size: int = 100,
        max_retries: int = 5,
    ) -> None:
        self._client = client or OpenAI(api_key=config.openai_api_key)
        self._model = config.embed_model
        self._batch_size = batch_size
        self._max_retries = max_retries

    @property
    def model(self) -> str:
        return self._model

    def embed_one(self, text: str) -> list[float]:
        """Embed a single string. Used by the retrievers for query embedding."""
        return self._embed_with_retry([text])[0][0]

    def embed_many(
        self,
        texts: Sequence[str],
        on_progress: Callable[[int, int], None] | None = None,
    ) -> tuple[list[list[float]], EmbedStats]:
        """Embed many strings, batched. Returns (embeddings, stats)."""
        if not texts:
            return [], EmbedStats(0, 0, 0, 0.0)

        out: list[list[float]] = []
        total_tokens = 0
        n_batches = 0
        t0 = time.perf_counter()

        for start in range(0, len(texts), self._batch_size):
            batch = list(texts[start : start + self._batch_size])
            embeds, used = self._embed_with_retry(batch)
            out.extend(embeds)
            total_tokens += used
            n_batches += 1
            if on_progress:
                on_progress(start + len(batch), len(texts))

        elapsed = time.perf_counter() - t0
        return out, EmbedStats(
            n_inputs=len(texts),
            n_batches=n_batches,
            total_tokens=total_tokens,
            elapsed_seconds=elapsed,
        )

    # ---- internal --------------------------------------------------------

    def _embed_with_retry(self, batch: list[str]) -> tuple[list[list[float]], int]:
        delay = 1.0
        last_err: Exception | None = None
        for _ in range(self._max_retries):
            try:
                resp = self._client.embeddings.create(model=self._model, input=batch)
                vecs = [d.embedding for d in resp.data]
                used = resp.usage.total_tokens if resp.usage else 0
                return vecs, used
            except RateLimitError as e:
                last_err = e
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
            except APIError as e:
                last_err = e
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
        raise RuntimeError(f"embedding failed after retries: {last_err}")


# ---------------------------------------------------------------------------
# Chunker (markdown, paragraph-aware)
# ---------------------------------------------------------------------------


def chunk_markdown(
    text: str, max_chars: int = 500, overlap: int = 50
) -> list[str]:
    """Greedy paragraph-pack chunker with a small character-level overlap.

    Paragraphs are joined into a chunk until adding the next paragraph would
    exceed `max_chars`. A single paragraph longer than `max_chars` (e.g. a
    long SQL block) is kept whole rather than split mid-statement. Chunks
    after the first carry an `overlap`-character tail of the previous chunk
    so that retrieval can hit context near boundaries.
    """
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        return []

    chunks: list[str] = []
    current = paragraphs[0]
    for para in paragraphs[1:]:
        candidate = current + "\n\n" + para
        if len(candidate) <= max_chars:
            current = candidate
            continue
        chunks.append(current)
        tail = _overlap_tail(current, overlap)
        current = (tail + "\n\n" + para) if tail else para
    chunks.append(current)
    return chunks


def _overlap_tail(text: str, overlap: int) -> str:
    if overlap <= 0 or len(text) <= overlap:
        return ""
    tail = text[-overlap:]
    sp = tail.find(" ")  # avoid splitting a word
    return tail[sp + 1 :] if sp > 0 else tail


# ---------------------------------------------------------------------------
# Doc index
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DocChunk:
    chunk_id: str
    text: str
    metadata: dict


def _doc_chunks(docs_dir: Path) -> list[DocChunk]:
    summaries = {s.filename: s.title for s in load_doc_summaries(docs_dir)}
    out: list[DocChunk] = []
    for path in sorted(docs_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        title = summaries.get(path.name, path.stem)
        chunks = chunk_markdown(text, max_chars=500, overlap=50)
        for i, chunk in enumerate(chunks):
            out.append(
                DocChunk(
                    chunk_id=f"{path.stem}-{i:03d}",
                    text=chunk,
                    metadata={
                        "filename": path.name,
                        "title": title,
                        "chunk_index": i,
                    },
                )
            )
    return out


def build_doc_index(
    chroma_dir: Path,
    docs_dir: Path,
    embedder: Embedder,
    *,
    replace: bool = False,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict:
    """(Re)build the methodology_docs collection from `data/docs/*.md`."""
    chunks = _doc_chunks(docs_dir)
    client = _open_client(chroma_dir)
    collection = _get_collection(
        client, DOC_COLLECTION, expected=len(chunks), replace=replace
    )
    if collection is None:  # already populated
        return {"chunks": len(chunks), "skipped": True, "tokens": 0, "seconds": 0.0}

    vectors, stats = embedder.embed_many(
        [c.text for c in chunks], on_progress=on_progress
    )
    collection.add(
        ids=[c.chunk_id for c in chunks],
        embeddings=vectors,
        documents=[c.text for c in chunks],
        metadatas=[c.metadata for c in chunks],
    )
    return {
        "chunks": len(chunks),
        "skipped": False,
        "tokens": stats.total_tokens,
        "seconds": stats.elapsed_seconds,
    }


# ---------------------------------------------------------------------------
# Review index
# ---------------------------------------------------------------------------


def _review_rows(sqlite_path: Path) -> list[tuple[str, str, str, int, str | None, str]]:
    """Return rows for embedding: (review_id, order_id, message, score, category, date).

    Filters to non-empty `review_comment_message`. Joins to a per-order modal
    English category (deterministic tie-break by category name).
    """
    sql = """
    WITH item_cats AS (
      SELECT oi.order_id,
             t.product_category_name_english AS category,
             COUNT(*) AS n
      FROM order_items oi
      LEFT JOIN products p ON p.product_id = oi.product_id
      LEFT JOIN product_category_translation t
        ON t.product_category_name = p.product_category_name
      GROUP BY oi.order_id, t.product_category_name_english
    ),
    ranked AS (
      SELECT order_id, category,
             ROW_NUMBER() OVER (
               PARTITION BY order_id
               ORDER BY n DESC, category IS NULL, category
             ) AS rn
      FROM item_cats
    ),
    order_cat AS (
      SELECT order_id, category FROM ranked WHERE rn = 1
    )
    SELECT r.review_id,
           r.order_id,
           r.review_comment_message,
           r.review_score,
           oc.category AS product_category,
           r.review_creation_date
    FROM order_reviews r
    LEFT JOIN order_cat oc ON oc.order_id = r.order_id
    WHERE r.review_comment_message IS NOT NULL
      AND TRIM(r.review_comment_message) != '';
    """
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro&immutable=1", uri=True)
    try:
        return list(conn.execute(sql))
    finally:
        conn.close()


def build_review_index(
    chroma_dir: Path,
    sqlite_path: Path,
    embedder: Embedder,
    *,
    replace: bool = False,
    on_progress: Callable[[int, int], None] | None = None,
    limit: int | None = None,
) -> dict:
    """(Re)build the customer_reviews collection from order_reviews + categories.

    `limit` is supported only as a development knob; production builds use the
    full 41K-row set.
    """
    rows = _review_rows(sqlite_path)
    if limit is not None:
        rows = rows[:limit]

    client = _open_client(chroma_dir)
    collection = _get_collection(
        client, REVIEW_COLLECTION, expected=len(rows), replace=replace
    )
    if collection is None:
        return {"reviews": len(rows), "skipped": True, "tokens": 0, "seconds": 0.0}

    # Trim review text to keep a single embedding call well under the model's
    # 8191-token limit. ~4000 chars maps to ~1000 tokens.
    texts = [_clip(row[2], 4000) for row in rows]
    vectors, stats = embedder.embed_many(texts, on_progress=on_progress)

    ids: list[str] = []
    metadatas: list[dict] = []
    seen: set[str] = set()
    for review_id, order_id, _msg, score, category, created in rows:
        # review_id is non-unique in the source data; disambiguate.
        cid = review_id
        if cid in seen:
            cid = f"{review_id}-{order_id}"
        seen.add(cid)
        ids.append(cid)
        metadatas.append(
            {
                "review_id": review_id,
                "order_id": order_id,
                "review_score": int(score),
                "product_category": category or "",
                "review_creation_date": created or "",
            }
        )

    # ChromaDB caps a single .add() call. Insert in batches.
    insert_batch = 1000
    for start in range(0, len(rows), insert_batch):
        end = start + insert_batch
        collection.add(
            ids=ids[start:end],
            embeddings=vectors[start:end],
            documents=texts[start:end],
            metadatas=metadatas[start:end],
        )

    return {
        "reviews": len(rows),
        "skipped": False,
        "tokens": stats.total_tokens,
        "seconds": stats.elapsed_seconds,
    }


def _clip(s: str, max_chars: int) -> str:
    return s if len(s) <= max_chars else s[:max_chars]


# ---------------------------------------------------------------------------
# ChromaDB helpers
# ---------------------------------------------------------------------------


def _open_client(chroma_dir: Path) -> chromadb.api.ClientAPI:
    chroma_dir.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(chroma_dir))


def _get_collection(
    client: chromadb.api.ClientAPI,
    name: str,
    expected: int,
    replace: bool,
) -> chromadb.Collection | None:
    """Return a collection ready for insertion, or None if it's already populated."""
    if replace:
        try:
            client.delete_collection(name=name)
        except Exception:  # noqa: BLE001
            pass  # didn't exist
    coll = client.get_or_create_collection(name=name)
    if not replace and coll.count() == expected and expected > 0:
        return None
    if not replace and coll.count() != 0 and coll.count() != expected:
        # Stale partial state; rebuild rather than appending.
        client.delete_collection(name=name)
        coll = client.get_or_create_collection(name=name)
    return coll


def collection_counts(chroma_dir: Path) -> dict[str, int]:
    """Return current sizes of both collections, or 0 for missing ones."""
    client = _open_client(chroma_dir)
    out: dict[str, int] = {}
    for name in (DOC_COLLECTION, REVIEW_COLLECTION):
        try:
            out[name] = client.get_collection(name=name).count()
        except Exception:  # noqa: BLE001
            out[name] = 0
    return out


def _iter_in_chunks(seq: Iterable, n: int):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i : i + n]
