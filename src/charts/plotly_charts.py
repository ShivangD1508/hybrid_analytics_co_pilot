"""Plotly figure factory for the Streamlit UI.

`make_figure(df, spec)` returns a Plotly Figure for `line`, `bar`, and
`scatter` chart types. It returns `None` for `kpi`, `table`, and `none` --
those are rendered with native Streamlit primitives (`st.metric`,
`st.dataframe`, or skipped) rather than Plotly. Keeping that split here
means this module is UI-agnostic: the eval harness can serialize figures
to PNG without touching Streamlit.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from src.synthesizer import ChartSpec


_LAYOUT_DEFAULTS = dict(
    margin=dict(l=10, r=10, t=40, b=10),
    height=380,
    template="plotly_white",
    legend=dict(orientation="h", y=-0.18),
)


def make_figure(df: pd.DataFrame | None, spec: ChartSpec) -> go.Figure | None:
    """Build a Plotly figure for line/bar/scatter. Returns None for other types."""
    if df is None or df.empty:
        return None
    if spec.chart_type == "line":
        return _line(df, spec)
    if spec.chart_type == "bar":
        return _bar(df, spec)
    if spec.chart_type == "scatter":
        return _scatter(df, spec)
    return None


def _line(df: pd.DataFrame, spec: ChartSpec) -> go.Figure:
    x = spec.x_column
    y = spec.y_column
    plot_df = df.copy()
    if x:
        try:
            plot_df[x] = pd.to_datetime(plot_df[x], errors="coerce", format="mixed")
        except (TypeError, ValueError):
            pass
    fig = px.line(plot_df, x=x, y=y, markers=True)
    fig.update_layout(
        title=f"{y} over {x}" if (x and y) else None,
        xaxis_title=x,
        yaxis_title=y,
        **_LAYOUT_DEFAULTS,
    )
    return fig


def _bar(df: pd.DataFrame, spec: ChartSpec) -> go.Figure:
    x = spec.x_column
    y = spec.y_column
    fig = px.bar(df, x=x, y=y)
    fig.update_layout(
        title=f"{y} by {x}" if (x and y) else None,
        xaxis_title=x,
        yaxis_title=y,
        **_LAYOUT_DEFAULTS,
    )
    return fig


def _scatter(df: pd.DataFrame, spec: ChartSpec) -> go.Figure:
    x = spec.x_column
    y = spec.y_column
    fig = px.scatter(df, x=x, y=y)
    fig.update_layout(
        title=f"{y} vs {x}" if (x and y) else None,
        xaxis_title=x,
        yaxis_title=y,
        **_LAYOUT_DEFAULTS,
    )
    return fig
