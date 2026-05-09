"""Schema introspection for the SQL agent's system prompt.

Reads the live SQLite database via PRAGMAs (so the schema seen by the LLM is
always the schema actually deployed) and enriches it with row counts and
sampled categorical values. Emits a compact markdown rendering.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    sql_type: str
    nullable: bool
    pk: bool


@dataclass(frozen=True)
class FkInfo:
    column: str
    ref_table: str
    ref_column: str


@dataclass
class TableInfo:
    name: str
    row_count: int
    columns: list[ColumnInfo] = field(default_factory=list)
    foreign_keys: list[FkInfo] = field(default_factory=list)
    indexes: list[tuple[str, ...]] = field(default_factory=list)
    sample_values: dict[str, list[str]] = field(default_factory=dict)
    numeric_ranges: dict[str, tuple[float, float]] = field(default_factory=dict)
    date_ranges: dict[str, tuple[str, str]] = field(default_factory=dict)


@dataclass
class DatabaseSchema:
    tables: list[TableInfo]

    def by_name(self) -> dict[str, TableInfo]:
        return {t.name: t for t in self.tables}


# ---------------------------------------------------------------------------
# Heuristics for sampling
# ---------------------------------------------------------------------------

# TEXT columns with at most this many distinct values are sampled fully.
_CATEGORICAL_MAX_DISTINCT = 30

# Substrings that flag a column as a date/timestamp string for range queries.
_DATE_HINTS = ("_date", "_timestamp", "_at")


def _is_date_column(col: ColumnInfo) -> bool:
    return col.sql_type.upper() == "TEXT" and any(
        h in col.name for h in _DATE_HINTS
    )


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------


def introspect_schema(sqlite_path: Path) -> DatabaseSchema:
    """Read the live schema, row counts, and sample values from `sqlite_path`."""
    conn = sqlite3.connect(str(sqlite_path))
    try:
        table_names = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            ).fetchall()
        ]
        return DatabaseSchema(
            tables=[_introspect_table(conn, name) for name in table_names]
        )
    finally:
        conn.close()


def _introspect_table(conn: sqlite3.Connection, name: str) -> TableInfo:
    cols_raw = conn.execute(f"PRAGMA table_info({name})").fetchall()
    columns = [
        ColumnInfo(
            name=row[1],
            sql_type=row[2] or "TEXT",
            nullable=(row[3] == 0),
            pk=(row[5] > 0),
        )
        for row in cols_raw
    ]

    fks_raw = conn.execute(f"PRAGMA foreign_key_list({name})").fetchall()
    foreign_keys = [
        FkInfo(column=row[3], ref_table=row[2], ref_column=row[4])
        for row in fks_raw
    ]

    indexes: list[tuple[str, ...]] = []
    for idx_row in conn.execute(f"PRAGMA index_list({name})").fetchall():
        idx_name = idx_row[1]
        # Skip auto-indexes for primary keys / unique constraints.
        if idx_name.startswith("sqlite_autoindex_"):
            continue
        members = [r[2] for r in conn.execute(f"PRAGMA index_info({idx_name})").fetchall()]
        if members:
            indexes.append(tuple(members))

    row_count = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]

    info = TableInfo(
        name=name,
        row_count=row_count,
        columns=columns,
        foreign_keys=foreign_keys,
        indexes=indexes,
    )

    if row_count > 0:
        _enrich_with_samples(conn, info)

    return info


def _enrich_with_samples(conn: sqlite3.Connection, info: TableInfo) -> None:
    for col in info.columns:
        sql_type = col.sql_type.upper()

        if _is_date_column(col):
            row = conn.execute(
                f"SELECT MIN({col.name}), MAX({col.name}) "
                f"FROM {info.name} WHERE {col.name} IS NOT NULL"
            ).fetchone()
            if row and row[0] is not None:
                info.date_ranges[col.name] = (str(row[0]), str(row[1]))
            continue

        if sql_type == "TEXT":
            distinct = conn.execute(
                f"SELECT COUNT(DISTINCT {col.name}) FROM {info.name}"
            ).fetchone()[0]
            if 0 < distinct <= _CATEGORICAL_MAX_DISTINCT:
                vals = conn.execute(
                    f"SELECT DISTINCT {col.name} FROM {info.name} "
                    f"WHERE {col.name} IS NOT NULL "
                    f"ORDER BY {col.name} LIMIT {_CATEGORICAL_MAX_DISTINCT}"
                ).fetchall()
                info.sample_values[col.name] = [str(v[0]) for v in vals]
            continue

        if sql_type in ("INTEGER", "REAL"):
            row = conn.execute(
                f"SELECT MIN({col.name}), MAX({col.name}) "
                f"FROM {info.name} WHERE {col.name} IS NOT NULL"
            ).fetchone()
            if row and row[0] is not None:
                info.numeric_ranges[col.name] = (float(row[0]), float(row[1]))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def format_schema_for_llm(schema: DatabaseSchema) -> str:
    """Render the schema as markdown suitable for an LLM system prompt."""
    out: list[str] = []
    total_rows = sum(t.row_count for t in schema.tables)
    out.append(
        f"# DATABASE SCHEMA -- Olist Brazilian E-Commerce\n"
        f"{len(schema.tables)} tables, {total_rows:,} total rows. "
        f"Date columns are TEXT in ISO 8601 form; use SQLite `date()`, "
        f"`datetime()`, `strftime()` to filter.\n"
    )

    for t in schema.tables:
        out.append(_render_table(t))

    out.append(_render_join_paths(schema))
    return "\n".join(out).rstrip() + "\n"


def _render_table(t: TableInfo) -> str:
    lines: list[str] = [f"## {t.name} ({t.row_count:,} rows)"]

    pk_cols = [c.name for c in t.columns if c.pk]
    if pk_cols:
        lines.append(f"PRIMARY KEY: {', '.join(pk_cols)}")

    lines.append("COLUMNS:")
    name_w = max(len(c.name) for c in t.columns)
    type_w = max(len(c.sql_type) for c in t.columns)
    for c in t.columns:
        flags: list[str] = []
        if c.pk:
            flags.append("PK")
        if not c.nullable:
            flags.append("NOT NULL")
        flag_str = f" [{', '.join(flags)}]" if flags else ""

        annot = ""
        if c.name in t.sample_values:
            vals = t.sample_values[c.name]
            preview = ", ".join(vals[:8])
            if len(vals) > 8:
                preview += f", ... ({len(vals)} total)"
            annot = f"  -- values: {{{preview}}}"
        elif c.name in t.date_ranges:
            lo, hi = t.date_ranges[c.name]
            annot = f"  -- range: {lo} to {hi}"
        elif c.name in t.numeric_ranges:
            lo, hi = t.numeric_ranges[c.name]
            annot = f"  -- range: {_fmt_num(lo)} to {_fmt_num(hi)}"

        lines.append(f"  {c.name:<{name_w}}  {c.sql_type:<{type_w}}{flag_str}{annot}")

    if t.foreign_keys:
        lines.append("FOREIGN KEYS:")
        for fk in t.foreign_keys:
            lines.append(f"  {fk.column} -> {fk.ref_table}.{fk.ref_column}")

    if t.indexes:
        lines.append(
            "INDEXES: " + ", ".join("(" + ", ".join(ix) + ")" for ix in t.indexes)
        )

    return "\n".join(lines) + "\n"


def _render_join_paths(schema: DatabaseSchema) -> str:
    paths: list[str] = []
    for t in schema.tables:
        for fk in t.foreign_keys:
            paths.append(
                f"  {t.name}.{fk.column} = {fk.ref_table}.{fk.ref_column}"
            )
    if not paths:
        return ""
    return "## JOIN PATHS\n" + "\n".join(paths) + "\n"


def _fmt_num(v: float) -> str:
    if v == int(v):
        return f"{int(v):,}"
    return f"{v:,.2f}"
