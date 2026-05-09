"""Central config. Loads .env once and exposes typed constants for the rest of the codebase."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_REPO_ROOT / ".env")


def _path(env_name: str, default: str) -> Path:
    raw = os.getenv(env_name, default)
    p = Path(raw)
    return p if p.is_absolute() else (_REPO_ROOT / p).resolve()


def _int(env_name: str, default: int) -> int:
    return int(os.getenv(env_name, default))


@dataclass(frozen=True)
class Config:
    repo_root: Path
    chat_model: str
    embed_model: str
    olist_csv_dir: Path
    sqlite_path: Path
    chroma_dir: Path
    sql_row_limit: int
    sql_timeout_seconds: int
    retriever_top_k: int
    _openai_api_key: str

    @property
    def openai_api_key(self) -> str:
        """Returns the API key. Raises if unset — call only from code paths that hit OpenAI."""
        if not self._openai_api_key:
            raise RuntimeError(
                "OPENAI_API_KEY not set. Copy .env.example to .env and fill it in."
            )
        return self._openai_api_key


def load_config() -> Config:
    """Build the Config from environment variables. Call once at startup."""
    return Config(
        repo_root=_REPO_ROOT,
        _openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        chat_model=os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini"),
        embed_model=os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small"),
        olist_csv_dir=_path("OLIST_CSV_DIR", "./dataset"),
        sqlite_path=_path("SQLITE_PATH", "./data/olist.db"),
        chroma_dir=_path("CHROMA_DIR", "./data/chroma"),
        sql_row_limit=_int("SQL_ROW_LIMIT", 1000),
        sql_timeout_seconds=_int("SQL_TIMEOUT_SECONDS", 10),
        retriever_top_k=_int("RETRIEVER_TOP_K", 5),
    )
