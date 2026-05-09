"""Validate generated SQL before it touches the database.

Three layers of defense:

1. **Static checks on the raw SQL string.** Strip comments and string
   literals, then look for forbidden keywords (DDL/DML), multiple
   statements, missing LIMIT, or a LIMIT that exceeds the configured cap.
2. **EXPLAIN against a read-only SQLite connection.** Catches syntax
   errors and references to non-existent tables/columns without running
   the query.
3. **Read-only DB connection at execute time.** Defense in depth — the
   executor opens the database in `mode=ro&immutable=1`, so any DDL/DML
   that slipped through layer 1 still cannot mutate state.

The validator does not normalize or rewrite SQL; if a query is missing
LIMIT, it fails. The generator's system prompt is responsible for
producing compliant SQL.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path


_FORBIDDEN_KEYWORDS: frozenset[str] = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "DROP",
        "ALTER",
        "CREATE",
        "ATTACH",
        "DETACH",
        "REPLACE",
        "PRAGMA",
        "TRUNCATE",
        "GRANT",
        "REVOKE",
        "VACUUM",
        "REINDEX",
    }
)

_ALLOWED_LEAD_KEYWORDS: frozenset[str] = frozenset({"SELECT", "WITH"})


@dataclass(frozen=True)
class ValidationResult:
    passed: bool
    error: str | None
    normalized_sql: str  # trimmed, trailing `;` removed


def validate(sql: str, sqlite_path: Path, max_rows: int) -> ValidationResult:
    """Run all validation layers. Returns a single result; first failure short-circuits."""
    if not sql or not sql.strip():
        return _fail("empty SQL", sql)

    # Layer 1a: strip strings and comments before any keyword checks.
    naked = _strip_strings_and_comments(sql)

    # Layer 1b: must be a single statement. Allow at most a trailing `;`.
    body = naked.rstrip().rstrip(";")
    if ";" in body:
        return _fail("multiple statements not allowed", sql)

    tokens = re.findall(r"[A-Za-z_]+", naked)
    if not tokens:
        return _fail("no SQL tokens found", sql)

    # Layer 1c: must start with SELECT or WITH.
    first_kw = tokens[0].upper()
    if first_kw not in _ALLOWED_LEAD_KEYWORDS:
        return _fail(f"queries must start with SELECT or WITH; got '{tokens[0]}'", sql)

    # Layer 1d: no forbidden keywords anywhere.
    for tok in tokens:
        if tok.upper() in _FORBIDDEN_KEYWORDS:
            return _fail(f"forbidden keyword: {tok}", sql)

    # Layer 1e: must include LIMIT, and LIMIT <= max_rows.
    limit_match = re.search(r"\bLIMIT\s+(\d+)", naked, flags=re.IGNORECASE)
    if not limit_match:
        return _fail(
            f"query must include an explicit LIMIT clause (max {max_rows})",
            sql,
        )
    limit_value = int(limit_match.group(1))
    if limit_value > max_rows:
        return _fail(
            f"LIMIT {limit_value} exceeds the maximum of {max_rows}",
            sql,
        )

    # Layer 2: EXPLAIN against a read-only connection.
    normalized = sql.strip().rstrip(";")
    try:
        conn = sqlite3.connect(
            f"file:{sqlite_path}?mode=ro&immutable=1", uri=True
        )
        try:
            conn.execute(f"EXPLAIN {normalized}")
        finally:
            conn.close()
    except sqlite3.Error as e:
        return _fail(f"sqlite rejected query: {e}", sql)

    return ValidationResult(passed=True, error=None, normalized_sql=normalized)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fail(msg: str, sql: str) -> ValidationResult:
    return ValidationResult(
        passed=False,
        error=msg,
        normalized_sql=sql.strip().rstrip(";"),
    )


def _strip_strings_and_comments(sql: str) -> str:
    """Replace string literals and comments with spaces so keyword scans skip them.

    Handles SQLite's quoting rules: '...' for string literals (with '' as
    escape), "..." for identifiers (with "" as escape), `--` line comments,
    and `/* ... */` block comments. Lengths are preserved so character
    offsets remain useful for later regex matches.
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        # Block comment.
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            if j == -1:
                out.append(" " * (n - i))
                break
            out.append(" " * (j + 2 - i))
            i = j + 2
            continue
        # Line comment.
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i + 2)
            if j == -1:
                out.append(" " * (n - i))
                break
            out.append(" " * (j - i))
            i = j  # keep the newline
            continue
        # Single-quoted string.
        if ch == "'":
            out.append("'")
            i += 1
            while i < n:
                if sql[i] == "'" and i + 1 < n and sql[i + 1] == "'":
                    out.append("  ")
                    i += 2
                    continue
                if sql[i] == "'":
                    out.append("'")
                    i += 1
                    break
                out.append(" ")
                i += 1
            continue
        # Double-quoted identifier.
        if ch == '"':
            out.append('"')
            i += 1
            while i < n:
                if sql[i] == '"' and i + 1 < n and sql[i + 1] == '"':
                    out.append("  ")
                    i += 2
                    continue
                if sql[i] == '"':
                    out.append('"')
                    i += 1
                    break
                out.append(" ")
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)
