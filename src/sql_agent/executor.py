"""Execute validated SQL against the SQLite database.

The connection is opened in `mode=ro&immutable=1` (read-only, no journaling
side-effects) regardless of what the validator allowed. A SQLite progress
handler enforces a wall-clock timeout: if the deadline passes, the handler
returns non-zero and SQLite raises `OperationalError: interrupted` from the
running statement.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd


@dataclass(frozen=True)
class SqlResult:
    df: pd.DataFrame | None
    columns: tuple[str, ...]
    row_count: int
    execution_ms: int
    error: str | None
    timed_out: bool


# Frequency at which SQLite invokes the progress handler. 100k VM
# instructions is a few milliseconds of work — small enough for prompt
# interruption, large enough that the handler is not a measurable
# overhead on small queries.
_PROGRESS_INTERVAL = 100_000


def execute(sql: str, sqlite_path: Path, timeout_seconds: int) -> SqlResult:
    """Run `sql` read-only against `sqlite_path`. Never raises; failures land in `.error`."""
    deadline = time.monotonic() + timeout_seconds

    def _watchdog() -> int:
        return 1 if time.monotonic() > deadline else 0

    try:
        conn = sqlite3.connect(
            f"file:{sqlite_path}?mode=ro&immutable=1", uri=True
        )
    except sqlite3.Error as e:
        return SqlResult(None, (), 0, 0, f"failed to open db: {e}", False)

    try:
        conn.set_progress_handler(_watchdog, _PROGRESS_INTERVAL)
        t0 = time.perf_counter()
        try:
            df = pd.read_sql_query(sql, conn)
        except sqlite3.OperationalError as e:
            elapsed = int((time.perf_counter() - t0) * 1000)
            timed_out = "interrupted" in str(e).lower()
            msg = (
                f"query exceeded {timeout_seconds}s timeout"
                if timed_out
                else f"sqlite error: {e}"
            )
            return SqlResult(None, (), 0, elapsed, msg, timed_out)
        except sqlite3.Error as e:
            elapsed = int((time.perf_counter() - t0) * 1000)
            return SqlResult(None, (), 0, elapsed, f"sqlite error: {e}", False)
        except pd.errors.DatabaseError as e:
            elapsed = int((time.perf_counter() - t0) * 1000)
            return SqlResult(None, (), 0, elapsed, f"pandas error: {e}", False)

        elapsed = int((time.perf_counter() - t0) * 1000)
        return SqlResult(
            df=df,
            columns=tuple(str(c) for c in df.columns),
            row_count=len(df),
            execution_ms=elapsed,
            error=None,
            timed_out=False,
        )
    finally:
        conn.set_progress_handler(None, 0)
        conn.close()
