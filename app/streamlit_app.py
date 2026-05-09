"""Streamlit UI for the Olist Analytics Agent.

Run from the repo root:

    streamlit run app/streamlit_app.py

Layout:
- Sidebar: database stats, vector-store stats, model info, session cost.
- Main: title + subtitle, six example questions, free-text input,
  results panel (reasoning chain, answer, chart, sources, metadata).

The reasoning chain and the source cross-reference are the load-bearing
parts of this UI. Everything else is plumbing.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import streamlit as st

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from src.charts.plotly_charts import make_figure
from src.config import Config, load_config
from src.pipeline import Pipeline, PipelineResult
from src.retriever import collection_counts


# ---------------------------------------------------------------------------
# Pricing -- approximate, used to display session cost. Embedding tokens are
# not tracked by the pipeline (cost is sub-cent across a session) so this is
# a chat-completion-only estimate.
# ---------------------------------------------------------------------------

_PRICE_PER_1M = {
    "gpt-4o-mini": {"input": 0.15, "cached_input": 0.075, "output": 0.60},
}


def _estimate_cost_usd(model: str, prompt: int, cached: int, completion: int) -> float:
    rates = _PRICE_PER_1M.get(model.split("-2")[0])  # tolerate dated suffixes
    if rates is None:
        rates = _PRICE_PER_1M["gpt-4o-mini"]
    uncached = max(0, prompt - cached)
    return (
        uncached * rates["input"] / 1_000_000
        + cached * rates["cached_input"] / 1_000_000
        + completion * rates["output"] / 1_000_000
    )


# ---------------------------------------------------------------------------
# Cached pipeline + sidebar stats
# ---------------------------------------------------------------------------


@st.cache_resource(show_spinner="Loading the agent...")
def get_pipeline() -> Pipeline:
    return Pipeline(config=load_config())


@st.cache_data(show_spinner=False)
def db_row_counts(sqlite_path_str: str) -> dict[str, int]:
    conn = sqlite3.connect(f"file:{sqlite_path_str}?mode=ro&immutable=1", uri=True)
    try:
        names = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {n: conn.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0] for n in names}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Examples
# ---------------------------------------------------------------------------


_EXAMPLES: list[tuple[str, str]] = [
    ("sql", "What is the monthly revenue trend across 2017 and 2018?"),
    ("sql", "Top 10 product categories by order count."),
    ("docs", "How do we calculate customer lifetime value?"),
    ("docs", "What is the on-time delivery SLA definition?"),
    ("hybrid", "Why are electronics reviews lower than the global average?"),
    ("hybrid", "Which customer segments are most at risk of churning?"),
]


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------


def render_sidebar(cfg: Config) -> None:
    with st.sidebar:
        st.title("Olist Analytics Agent")
        st.caption("Routing agent over the Olist Brazilian e-commerce dataset.")

        st.subheader("Database")
        counts = db_row_counts(str(cfg.sqlite_path))
        total = sum(counts.values())
        st.write(f"{len(counts)} tables, {total:,} rows")
        with st.expander("Per-table row counts"):
            for name, n in counts.items():
                st.write(f"- `{name}`: {n:,}")

        st.subheader("Vector store")
        cc = collection_counts(cfg.chroma_dir)
        st.write(f"`methodology_docs`: {cc['methodology_docs']:,} chunks")
        st.write(f"`customer_reviews`: {cc['customer_reviews']:,} reviews")

        st.subheader("Models")
        st.write(f"chat: `{cfg.chat_model}`")
        st.write(f"embed: `{cfg.embed_model}`")

        st.subheader("Session")
        sess = st.session_state.setdefault("session_stats", _empty_stats())
        st.write(f"questions: {sess['n']}")
        st.write(f"prompt tokens: {sess['prompt']:,}")
        st.write(f"cached: {sess['cached']:,}")
        st.write(f"completion: {sess['completion']:,}")
        st.write(f"chat-completion cost: **${sess['cost']:.4f}**")
        st.caption("Embedding tokens not included; sub-cent across a session.")


def _empty_stats() -> dict:
    return {"n": 0, "prompt": 0, "cached": 0, "completion": 0, "cost": 0.0}


def update_session_stats(result: PipelineResult) -> None:
    sess = st.session_state.setdefault("session_stats", _empty_stats())
    sess["n"] += 1
    sess["prompt"] += result.total_prompt_tokens
    sess["cached"] += result.total_cached_tokens
    sess["completion"] += result.total_completion_tokens
    sess["cost"] += _estimate_cost_usd(
        result.router_decision.model,
        result.total_prompt_tokens,
        result.total_cached_tokens,
        result.total_completion_tokens,
    )


# ---------------------------------------------------------------------------
# Result rendering
# ---------------------------------------------------------------------------


def render_result(result: PipelineResult) -> None:
    cost = _estimate_cost_usd(
        result.router_decision.model,
        result.total_prompt_tokens,
        result.total_cached_tokens,
        result.total_completion_tokens,
    )

    # Header strip with route + confidence + key cost numbers.
    cols = st.columns([1, 1, 1, 1])
    cols[0].metric("Route", result.route)
    cols[1].metric("Confidence", f"{result.confidence:.2f}")
    cols[2].metric("Total time", f"{result.total_ms / 1000:.1f}s")
    cols[3].metric("Estimated cost", f"${cost:.4f}")

    # 1. Reasoning chain (expanded by default -- this is the differentiator).
    with st.expander("Reasoning chain", expanded=True):
        for i, step in enumerate(result.reasoning_chain, 1):
            st.markdown(f"**{i}.** {step}")

    # 2. Answer.
    st.subheader("Answer")
    st.markdown(result.answer)

    # 3. Chart.
    spec = result.chart_spec
    if spec.chart_type != "none":
        st.subheader("Chart")
        st.caption(f"`{spec.chart_type}` -- {spec.rationale}")
        _render_chart(result)

    # 4. Sources.
    with st.expander(f"Sources ({len(result.sources)})", expanded=False):
        _render_sources(result)

    # 5. Metadata.
    with st.expander("Metadata (per-stage timing, tokens)", expanded=False):
        _render_metadata(result, cost)


def _render_chart(result: PipelineResult) -> None:
    spec = result.chart_spec
    df = result.df

    if spec.chart_type == "kpi":
        col = spec.y_column or (df.columns[0] if df is not None and len(df.columns) else "")
        val = df.iloc[0][col] if df is not None and not df.empty else None
        st.metric(label=str(col), value=_fmt_kpi(val))
        return

    if spec.chart_type == "table":
        if df is None or df.empty:
            st.info("No rows to display.")
        else:
            st.dataframe(df, use_container_width=True, hide_index=True)
        return

    fig = make_figure(df, spec)
    if fig is None:
        if df is not None and not df.empty:
            st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.plotly_chart(fig, use_container_width=True)


def _fmt_kpi(v) -> str:
    if v is None:
        return "-"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(x) >= 1_000_000:
        return f"{x:,.0f}"
    if abs(x) >= 1000 or x == int(x):
        return f"{x:,.2f}" if x != int(x) else f"{int(x):,}"
    return f"{x:.4f}"


def _render_sources(result: PipelineResult) -> None:
    if not result.sources:
        st.write("(no sources)")
        return

    sql_sources = [s for s in result.sources if s.type == "sql"]
    doc_sources = [s for s in result.sources if s.type == "doc"]
    review_sources = [s for s in result.sources if s.type == "review"]

    if sql_sources:
        st.markdown("**SQL**")
        for s in sql_sources:
            st.code(s.query, language="sql")
            st.caption(f"{s.rows} rows, {len(s.columns)} columns")

    if doc_sources:
        st.markdown("**Methodology doc chunks**")
        for s in doc_sources:
            st.markdown(
                f"`[doc:{s.filename}]` chunk {s.chunk_index} -- distance {s.distance:.3f}"
            )
            st.markdown(
                f"<div style='border-left:3px solid #888;padding-left:10px;color:#444;"
                f"font-size:0.92em'>{s.text}</div>",
                unsafe_allow_html=True,
            )
            st.write("")

    if review_sources:
        st.markdown("**Customer reviews**")
        for s in review_sources:
            short = (s.order_id or "")[:8]
            cat = s.category or "(no category)"
            st.markdown(
                f"`[review:{short}]` score {s.score} -- {cat} -- distance {s.distance:.3f}"
            )
            st.markdown(
                f"<div style='border-left:3px solid #888;padding-left:10px;color:#444;"
                f"font-size:0.92em'>{s.text}</div>",
                unsafe_allow_html=True,
            )
            st.write("")


def _render_metadata(result: PipelineResult, cost: float) -> None:
    st.markdown("**Per-stage timing and tokens**")
    rows: list[dict] = []
    for t in result.timings:
        rows.append(
            {
                "stage": t.stage,
                "latency_ms": t.latency_ms,
                "prompt_tokens": t.prompt_tokens,
                "cached_tokens": t.cached_tokens,
                "completion_tokens": t.completion_tokens,
            }
        )
    rows.append(
        {
            "stage": "TOTAL",
            "latency_ms": result.total_ms,
            "prompt_tokens": result.total_prompt_tokens,
            "cached_tokens": result.total_cached_tokens,
            "completion_tokens": result.total_completion_tokens,
        }
    )
    st.dataframe(rows, use_container_width=True, hide_index=True)

    st.markdown("**Costs**")
    st.write(
        f"- Estimated chat-completion cost for this question: **${cost:.4f}**"
    )
    st.caption(
        "Pricing assumes gpt-4o-mini at $0.15 / $0.075 / $0.60 per 1M (input / "
        "cached input / output). Embedding tokens for retrieval queries are "
        "not tracked; their cost is sub-cent."
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(
        page_title="Olist Analytics Agent",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    cfg = load_config()
    render_sidebar(cfg)

    st.title("Olist Analytics Agent")
    st.markdown(
        "Ask business questions. The agent routes to SQL, methodology docs, "
        "review search, or a hybrid; runs the relevant tools; and synthesizes "
        "an answer with cited sources and a transparent reasoning chain."
    )

    st.subheader("Try an example")
    example_cols = st.columns(3)
    for i, (route, q) in enumerate(_EXAMPLES):
        col = example_cols[i % 3]
        if col.button(q, key=f"ex_{i}", use_container_width=True):
            st.session_state["question_input"] = q
            st.session_state["auto_submit"] = True

    st.subheader("Or ask anything")
    initial = st.session_state.get("question_input", "")
    question = st.text_area(
        "Question",
        key="question_input",
        value=initial,
        height=80,
        label_visibility="collapsed",
        placeholder="e.g. Why are northern Brazilian states slower to receive orders?",
    )

    submit_cols = st.columns([1, 1, 6])
    submitted = submit_cols[0].button("Run", type="primary")
    if submit_cols[1].button("Clear"):
        st.session_state["question_input"] = ""
        st.session_state.pop("last_result", None)
        st.rerun()

    auto = st.session_state.pop("auto_submit", False)
    if submitted or auto:
        if not question or not question.strip():
            st.warning("Type a question or pick an example first.")
        else:
            try:
                pipeline = get_pipeline()
            except RuntimeError as e:
                st.error(str(e))
                return
            with st.spinner("Routing, retrieving, generating, synthesizing..."):
                try:
                    result = pipeline.run(question.strip())
                except Exception as e:  # noqa: BLE001 -- show in UI rather than crash
                    st.error(f"Pipeline error: {type(e).__name__}: {e}")
                    return
            update_session_stats(result)
            st.session_state["last_result"] = result

    last = st.session_state.get("last_result")
    if last is not None:
        st.divider()
        render_result(last)


if __name__ == "__main__":
    main()
