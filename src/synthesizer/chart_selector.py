"""Rules-based chart selector. No LLM call.

Decisions are made from the DataFrame's shape and inferred column kinds:
- a column is "datetime-like" if its values parse as ISO dates,
- "numeric" if its pandas dtype is integer or floating,
- "categorical" if it is object/string with low cardinality,
- otherwise "text" (treated as opaque).

The output `ChartSpec` is consumed by `src/charts/plotly_charts.py` (Step 9)
and surfaced in the synthesizer's reasoning chain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd


ChartType = Literal["line", "bar", "scatter", "table", "kpi", "none"]


@dataclass(frozen=True)
class ChartSpec:
    chart_type: ChartType
    x_column: str | None
    y_column: str | None
    rationale: str


# Cardinality cap for treating an object column as categorical (vs free text).
_CATEGORICAL_MAX = 30
# Row-count cap for using a bar chart instead of a table.
_BAR_MAX_ROWS = 15


def select_chart(df: pd.DataFrame | None) -> ChartSpec:
    """Pick a chart type from the DataFrame's shape. Pure function."""
    if df is None or df.empty:
        return ChartSpec("none", None, None, "no data returned")

    n_rows, n_cols = df.shape

    if n_rows == 1 and n_cols == 1 and _is_numeric(df.dtypes.iloc[0]):
        return ChartSpec(
            "kpi",
            x_column=None,
            y_column=str(df.columns[0]),
            rationale="single numeric value",
        )

    date_cols = [c for c in df.columns if _is_date_like(df[c])]
    numeric_cols = [c for c in df.columns if _is_numeric(df[c].dtype)]
    cat_cols = [
        c for c in df.columns
        if c not in date_cols
        and c not in numeric_cols
        and _is_low_cardinality(df[c])
    ]

    if date_cols and numeric_cols:
        return ChartSpec(
            "line",
            x_column=date_cols[0],
            y_column=numeric_cols[0],
            rationale=f"time-series on {date_cols[0]} vs {numeric_cols[0]}",
        )

    if cat_cols and numeric_cols and n_rows <= _BAR_MAX_ROWS:
        return ChartSpec(
            "bar",
            x_column=cat_cols[0],
            y_column=numeric_cols[0],
            rationale=f"categorical {cat_cols[0]} vs numeric {numeric_cols[0]}, {n_rows} rows",
        )

    if (
        len(numeric_cols) >= 2
        and not cat_cols
        and not date_cols
    ):
        return ChartSpec(
            "scatter",
            x_column=numeric_cols[0],
            y_column=numeric_cols[1],
            rationale=f"two numeric columns: {numeric_cols[0]}, {numeric_cols[1]}",
        )

    return ChartSpec(
        "table",
        x_column=None,
        y_column=None,
        rationale=f"{n_rows} rows x {n_cols} cols, mixed types",
    )


# ---------------------------------------------------------------------------
# Type sniffing
# ---------------------------------------------------------------------------


def _is_numeric(dtype) -> bool:
    return pd.api.types.is_numeric_dtype(dtype) and not pd.api.types.is_bool_dtype(dtype)


def _is_date_like(series: pd.Series) -> bool:
    """True if this column parses as a date in >=80% of non-null rows.

    We don't store native datetimes (everything is TEXT in SQLite), so columns
    arrive as object/string. Try parsing a sample.
    """
    if pd.api.types.is_datetime64_any_dtype(series):
        return True
    if not pd.api.types.is_object_dtype(series):
        return False
    sample = series.dropna().head(50)
    if len(sample) == 0:
        return False
    try:
        parsed = pd.to_datetime(sample, errors="coerce", format="mixed")
    except (TypeError, ValueError):
        return False
    return parsed.notna().mean() >= 0.8


def _is_low_cardinality(series: pd.Series) -> bool:
    if not pd.api.types.is_object_dtype(series):
        return False
    n = series.nunique(dropna=True)
    return 0 < n <= _CATEGORICAL_MAX


def to_chart_data(df: pd.DataFrame, spec: ChartSpec) -> dict:
    """Serialize the data for the chosen chart into a JSON-able dict.

    For `table` we cap at 100 rows so the payload stays small; the executor's
    LIMIT (default 1000) bounds the upper end already.
    """
    if spec.chart_type == "none" or df is None or df.empty:
        return {}
    if spec.chart_type == "kpi":
        col = spec.y_column or df.columns[0]
        val = df.iloc[0][col]
        return {"value": _coerce_scalar(val), "label": str(col)}
    if spec.chart_type == "table":
        return {"columns": [str(c) for c in df.columns], "rows": df.head(100).values.tolist()}
    # line / bar / scatter share an x/y payload
    x = spec.x_column
    y = spec.y_column
    return {
        "x": df[x].tolist() if x else None,
        "y": df[y].tolist() if y else None,
        "x_column": x,
        "y_column": y,
    }


def _coerce_scalar(v):
    if pd.isna(v):
        return None
    if hasattr(v, "item"):
        try:
            return v.item()
        except (ValueError, TypeError):
            pass
    return v
