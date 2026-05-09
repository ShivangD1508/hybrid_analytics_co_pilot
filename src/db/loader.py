"""Build the Olist SQLite database from the 9 source CSVs.

Table specs are declared once here and consumed by both the loader (DDL +
inserts) and the schema introspector (LLM context). This keeps the SQL the
agent sees aligned with the SQL the database actually exposes.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Spec types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    sql_type: str  # "TEXT" | "INTEGER" | "REAL"
    nullable: bool = True
    pk: bool = False


@dataclass(frozen=True)
class ForeignKey:
    column: str
    ref_table: str
    ref_column: str


@dataclass(frozen=True)
class TableSpec:
    name: str
    csv_filename: str
    columns: tuple[ColumnSpec, ...]
    primary_key: tuple[str, ...] | None
    indexes: tuple[tuple[str, ...], ...]
    foreign_keys: tuple[ForeignKey, ...]

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)


# ---------------------------------------------------------------------------
# Schema definitions
# ---------------------------------------------------------------------------
#
# Notes:
# - Date/timestamp values are stored as TEXT in ISO 8601 form. SQLite has no
#   native datetime type and recommends ISO TEXT; its date functions
#   (date(), datetime(), strftime()) operate on it directly.
# - Foreign keys are declared for documentation and PRAGMA introspection.
#   PRAGMA foreign_keys remains OFF (SQLite default) at load time because
#   the Olist data has known orphan references (e.g. reviews pointing at
#   missing orders); enforcing FKs would fail the load.
# - The Olist CSVs include a real spelling typo, `product_name_lenght` and
#   `product_description_lenght`. Kept as-is so column names match the
#   original dataset and any documentation.
# ---------------------------------------------------------------------------


CUSTOMERS = TableSpec(
    name="customers",
    csv_filename="olist_customers_dataset.csv",
    columns=(
        ColumnSpec("customer_id", "TEXT", nullable=False, pk=True),
        ColumnSpec("customer_unique_id", "TEXT", nullable=False),
        ColumnSpec("customer_zip_code_prefix", "INTEGER", nullable=False),
        ColumnSpec("customer_city", "TEXT", nullable=False),
        ColumnSpec("customer_state", "TEXT", nullable=False),
    ),
    primary_key=("customer_id",),
    indexes=(("customer_unique_id",), ("customer_state",)),
    foreign_keys=(),
)

ORDERS = TableSpec(
    name="orders",
    csv_filename="olist_orders_dataset.csv",
    columns=(
        ColumnSpec("order_id", "TEXT", nullable=False, pk=True),
        ColumnSpec("customer_id", "TEXT", nullable=False),
        ColumnSpec("order_status", "TEXT", nullable=False),
        ColumnSpec("order_purchase_timestamp", "TEXT", nullable=False),
        ColumnSpec("order_approved_at", "TEXT", nullable=True),
        ColumnSpec("order_delivered_carrier_date", "TEXT", nullable=True),
        ColumnSpec("order_delivered_customer_date", "TEXT", nullable=True),
        ColumnSpec("order_estimated_delivery_date", "TEXT", nullable=False),
    ),
    primary_key=("order_id",),
    indexes=(
        ("customer_id",),
        ("order_status",),
        ("order_purchase_timestamp",),
    ),
    foreign_keys=(ForeignKey("customer_id", "customers", "customer_id"),),
)

ORDER_ITEMS = TableSpec(
    name="order_items",
    csv_filename="olist_order_items_dataset.csv",
    columns=(
        ColumnSpec("order_id", "TEXT", nullable=False),
        ColumnSpec("order_item_id", "INTEGER", nullable=False),
        ColumnSpec("product_id", "TEXT", nullable=False),
        ColumnSpec("seller_id", "TEXT", nullable=False),
        ColumnSpec("shipping_limit_date", "TEXT", nullable=False),
        ColumnSpec("price", "REAL", nullable=False),
        ColumnSpec("freight_value", "REAL", nullable=False),
    ),
    primary_key=("order_id", "order_item_id"),
    indexes=(("product_id",), ("seller_id",)),
    foreign_keys=(
        ForeignKey("order_id", "orders", "order_id"),
        ForeignKey("product_id", "products", "product_id"),
        ForeignKey("seller_id", "sellers", "seller_id"),
    ),
)

ORDER_PAYMENTS = TableSpec(
    name="order_payments",
    csv_filename="olist_order_payments_dataset.csv",
    columns=(
        ColumnSpec("order_id", "TEXT", nullable=False),
        ColumnSpec("payment_sequential", "INTEGER", nullable=False),
        ColumnSpec("payment_type", "TEXT", nullable=False),
        ColumnSpec("payment_installments", "INTEGER", nullable=False),
        ColumnSpec("payment_value", "REAL", nullable=False),
    ),
    primary_key=("order_id", "payment_sequential"),
    indexes=(("payment_type",),),
    foreign_keys=(ForeignKey("order_id", "orders", "order_id"),),
)

ORDER_REVIEWS = TableSpec(
    name="order_reviews",
    csv_filename="olist_order_reviews_dataset.csv",
    columns=(
        # review_id is NOT unique across the file (a known dataset quirk).
        ColumnSpec("review_id", "TEXT", nullable=False),
        ColumnSpec("order_id", "TEXT", nullable=False),
        ColumnSpec("review_score", "INTEGER", nullable=False),
        ColumnSpec("review_comment_title", "TEXT", nullable=True),
        ColumnSpec("review_comment_message", "TEXT", nullable=True),
        ColumnSpec("review_creation_date", "TEXT", nullable=False),
        ColumnSpec("review_answer_timestamp", "TEXT", nullable=False),
    ),
    primary_key=None,
    indexes=(("order_id",), ("review_score",), ("review_id",)),
    foreign_keys=(ForeignKey("order_id", "orders", "order_id"),),
)

PRODUCTS = TableSpec(
    name="products",
    csv_filename="olist_products_dataset.csv",
    columns=(
        ColumnSpec("product_id", "TEXT", nullable=False, pk=True),
        ColumnSpec("product_category_name", "TEXT", nullable=True),
        ColumnSpec("product_name_lenght", "INTEGER", nullable=True),
        ColumnSpec("product_description_lenght", "INTEGER", nullable=True),
        ColumnSpec("product_photos_qty", "INTEGER", nullable=True),
        ColumnSpec("product_weight_g", "REAL", nullable=True),
        ColumnSpec("product_length_cm", "REAL", nullable=True),
        ColumnSpec("product_height_cm", "REAL", nullable=True),
        ColumnSpec("product_width_cm", "REAL", nullable=True),
    ),
    primary_key=("product_id",),
    indexes=(("product_category_name",),),
    foreign_keys=(
        ForeignKey(
            "product_category_name",
            "product_category_translation",
            "product_category_name",
        ),
    ),
)

SELLERS = TableSpec(
    name="sellers",
    csv_filename="olist_sellers_dataset.csv",
    columns=(
        ColumnSpec("seller_id", "TEXT", nullable=False, pk=True),
        ColumnSpec("seller_zip_code_prefix", "INTEGER", nullable=False),
        ColumnSpec("seller_city", "TEXT", nullable=False),
        ColumnSpec("seller_state", "TEXT", nullable=False),
    ),
    primary_key=("seller_id",),
    indexes=(("seller_state",),),
    foreign_keys=(),
)

GEOLOCATION = TableSpec(
    name="geolocation",
    csv_filename="olist_geolocation_dataset.csv",
    columns=(
        ColumnSpec("geolocation_zip_code_prefix", "INTEGER", nullable=False),
        ColumnSpec("geolocation_lat", "REAL", nullable=False),
        ColumnSpec("geolocation_lng", "REAL", nullable=False),
        ColumnSpec("geolocation_city", "TEXT", nullable=False),
        ColumnSpec("geolocation_state", "TEXT", nullable=False),
    ),
    # Multiple rows per zip prefix; no natural PK.
    primary_key=None,
    indexes=(("geolocation_zip_code_prefix",), ("geolocation_state",)),
    foreign_keys=(),
)

PRODUCT_CATEGORY_TRANSLATION = TableSpec(
    name="product_category_translation",
    csv_filename="product_category_name_translation.csv",
    columns=(
        ColumnSpec("product_category_name", "TEXT", nullable=False, pk=True),
        ColumnSpec("product_category_name_english", "TEXT", nullable=False),
    ),
    primary_key=("product_category_name",),
    indexes=(),
    foreign_keys=(),
)


# Load order respects FK references for clean PRAGMA introspection.
TABLES: tuple[TableSpec, ...] = (
    PRODUCT_CATEGORY_TRANSLATION,
    CUSTOMERS,
    SELLERS,
    PRODUCTS,
    ORDERS,
    ORDER_ITEMS,
    ORDER_PAYMENTS,
    ORDER_REVIEWS,
    GEOLOCATION,
)


# ---------------------------------------------------------------------------
# DDL + load
# ---------------------------------------------------------------------------


def _create_table_sql(spec: TableSpec) -> str:
    """Render a CREATE TABLE statement from a TableSpec."""
    cols: list[str] = []
    for c in spec.columns:
        parts = [c.name, c.sql_type]
        if not c.nullable:
            parts.append("NOT NULL")
        cols.append(" ".join(parts))

    if spec.primary_key:
        pk_cols = ", ".join(spec.primary_key)
        cols.append(f"PRIMARY KEY ({pk_cols})")

    for fk in spec.foreign_keys:
        cols.append(
            f"FOREIGN KEY ({fk.column}) REFERENCES {fk.ref_table}({fk.ref_column})"
        )

    body = ",\n  ".join(cols)
    return f"CREATE TABLE IF NOT EXISTS {spec.name} (\n  {body}\n)"


def _index_sql(spec: TableSpec, cols: tuple[str, ...]) -> str:
    name = f"idx_{spec.name}_{'_'.join(cols)}"
    return f"CREATE INDEX IF NOT EXISTS {name} ON {spec.name} ({', '.join(cols)})"


def _read_csv(spec: TableSpec, csv_dir: Path) -> pd.DataFrame:
    """Read a CSV with column-typed casts and NaN-aware nullability."""
    path = csv_dir / spec.csv_filename
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {path}")

    # Read everything as string first, then cast per column. This avoids the
    # pandas inference quirk where nullable text columns get read as float64
    # when the first chunks of the file are all NaN.
    df = pd.read_csv(path, dtype=str, keep_default_na=True, na_values=[""])

    missing = [c.name for c in spec.columns if c.name not in df.columns]
    if missing:
        raise ValueError(f"{spec.name}: CSV missing columns {missing}")

    df = df[list(spec.column_names)].copy()

    for c in spec.columns:
        if c.sql_type == "INTEGER":
            df[c.name] = pd.to_numeric(df[c.name], errors="coerce").astype("Int64")
        elif c.sql_type == "REAL":
            df[c.name] = pd.to_numeric(df[c.name], errors="coerce").astype(float)
        # TEXT stays as-is.

    return df


def _df_to_rows(df: pd.DataFrame) -> list[tuple]:
    """Convert a DataFrame to row tuples with all NaN/NaT/<NA> replaced by None."""
    arr = df.to_numpy(dtype=object)
    # np.where on object arrays detects pd.NA, np.nan, NaT consistently when
    # we test with pd.isna; do it cell-wise.
    rows: list[tuple] = []
    for row in arr:
        rows.append(tuple(None if _is_null(v) else v for v in row))
    return rows


def _is_null(v: object) -> bool:
    if v is None:
        return True
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def _load_table(conn: sqlite3.Connection, spec: TableSpec, csv_dir: Path) -> int:
    df = _read_csv(spec, csv_dir)
    rows = _df_to_rows(df)
    placeholders = ", ".join(["?"] * len(spec.columns))
    cols_sql = ", ".join(spec.column_names)
    sql = f"INSERT INTO {spec.name} ({cols_sql}) VALUES ({placeholders})"

    with conn:
        conn.execute(f"DELETE FROM {spec.name}")
        conn.executemany(sql, rows)
    return len(rows)


def _open_connection(sqlite_path: Path) -> sqlite3.Connection:
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(sqlite_path))
    # Performance: WAL for concurrent reads; large cache for bulk loads.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-200000")  # ~200 MB page cache
    conn.execute("PRAGMA foreign_keys=OFF")  # see module docstring re: orphans
    return conn


def build_database(
    csv_dir: Path,
    sqlite_path: Path,
    replace: bool = False,
    on_table: callable | None = None,
) -> dict[str, dict]:
    """Compile the Olist CSVs into a SQLite database. Returns per-table stats.

    If `replace` is True, the existing .db file is deleted first. Otherwise,
    tables that already contain rows are left untouched.

    `on_table(stage, spec, info)` is an optional progress callback. Stages:
    "create", "load", "index", "skip". info is a dict with keys like
    `rows`, `seconds`.
    """
    if replace and sqlite_path.exists():
        sqlite_path.unlink()
        # WAL/SHM sidecars
        for sfx in ("-wal", "-shm", "-journal"):
            sib = sqlite_path.with_name(sqlite_path.name + sfx)
            if sib.exists():
                sib.unlink()

    conn = _open_connection(sqlite_path)
    stats: dict[str, dict] = {}

    try:
        for spec in TABLES:
            t0 = time.perf_counter()
            with conn:
                conn.execute(_create_table_sql(spec))
            if on_table:
                on_table("create", spec, {})

            existing = conn.execute(
                f"SELECT COUNT(*) FROM {spec.name}"
            ).fetchone()[0]
            if existing > 0 and not replace:
                stats[spec.name] = {"rows": existing, "seconds": 0.0, "skipped": True}
                if on_table:
                    on_table("skip", spec, {"rows": existing})
                continue

            rows = _load_table(conn, spec, csv_dir)
            elapsed = time.perf_counter() - t0
            stats[spec.name] = {"rows": rows, "seconds": elapsed, "skipped": False}
            if on_table:
                on_table("load", spec, {"rows": rows, "seconds": elapsed})

            for idx in spec.indexes:
                with conn:
                    conn.execute(_index_sql(spec, idx))
            if on_table:
                on_table("index", spec, {"count": len(spec.indexes)})

        with conn:
            conn.execute("ANALYZE")
    finally:
        conn.close()

    return stats
