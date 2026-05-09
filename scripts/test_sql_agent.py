"""Smoke-test the SQL agent: generate, validate, execute.

Reads OPENAI_API_KEY from .env. Runs a small battery of analytical
questions plus 3 negative validator tests against handcrafted SQL.

Usage:
    python scripts/test_sql_agent.py                  # default battery
    python scripts/test_sql_agent.py "your question"  # one custom question
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from src.config import load_config
from src.db.schema import introspect_schema
from src.sql_agent import SqlGenerator, run_sql_for_question, validate


_BATTERY: list[str] = [
    "What was the total revenue across all orders in 2017?",
    "Top 5 customer states by number of orders.",
    "Average delivery time in days for delivered orders, by customer state, descending.",
    "Repeat purchase rate: share of unique customers with at least 2 orders.",
    "Top 10 product categories (English names) by total revenue, in 2018.",
]


_NEGATIVE_TESTS: list[tuple[str, str]] = [
    (
        "DDL should be rejected",
        "DROP TABLE orders;",
    ),
    (
        "Multiple statements should be rejected",
        "SELECT * FROM orders LIMIT 10; SELECT * FROM customers LIMIT 10;",
    ),
    (
        "Missing LIMIT should be rejected",
        "SELECT order_id FROM orders WHERE order_status = 'delivered';",
    ),
]


def _print_run(label: str, q: str, run) -> None:
    g = run.generation
    v = run.validation
    print(f"--- {label} ---")
    print(f"Q: {q}")
    print()
    print("Generated SQL:")
    for line in g.sql.splitlines():
        print(f"  {line}")
    print()
    print(f"Explanation: {g.explanation}")
    retry_str = "  (retried)" if run.retried else ""
    print(f"Validation : passed={v.passed}{retry_str}" + (f"  error={v.error}" if v.error else ""))
    if run.result is not None:
        r = run.result
        if r.error:
            print(f"Execution  : ERROR  {r.error}  ({r.execution_ms}ms, timed_out={r.timed_out})")
        else:
            print(f"Execution  : {r.row_count} rows, {len(r.columns)} cols, {r.execution_ms}ms")
            if r.df is not None and len(r.df) > 0:
                preview = r.df.head(5).to_string(index=False)
                for line in preview.splitlines():
                    print(f"  {line}")
    print(
        f"Tokens     : prompt={g.prompt_tokens} (cached={g.cached_tokens})  "
        f"completion={g.completion_tokens}  gen_latency={g.latency_ms}ms  total={run.total_ms}ms"
    )
    print()


def main() -> int:
    cfg = load_config()
    schema = introspect_schema(cfg.sqlite_path)
    gen = SqlGenerator(schema=schema, config=cfg)

    print(f"Model       : {cfg.chat_model}")
    print(f"Schema      : {len(schema.tables)} tables")
    print(f"Row limit   : {cfg.sql_row_limit}    Timeout: {cfg.sql_timeout_seconds}s")
    print(f"System prompt length: {len(gen.system_prompt):,} chars\n")
    print("=" * 78)

    questions = sys.argv[1:] if len(sys.argv) > 1 else None
    battery = [" ".join(questions)] if questions else _BATTERY

    n_ok = n_total = 0
    total_prompt_tokens = total_completion = total_cached = 0
    for q in battery:
        run = run_sql_for_question(q, gen, cfg)
        n_total += 1
        if (
            run.validation.passed
            and run.result is not None
            and run.result.error is None
            and run.result.row_count > 0
        ):
            n_ok += 1
        total_prompt_tokens += run.generation.prompt_tokens
        total_completion += run.generation.completion_tokens
        total_cached += run.generation.cached_tokens
        _print_run("LIVE", q, run)

    if not questions:
        print("=" * 78)
        print("Negative validator tests (no LLM call):")
        for label, sql in _NEGATIVE_TESTS:
            v = validate(sql, cfg.sqlite_path, cfg.sql_row_limit)
            ok = "OK " if not v.passed else "FAIL"
            print(f"  [{ok}] {label}")
            print(f"        sql: {sql}")
            print(f"        verdict: passed={v.passed}  error={v.error}")

    print()
    print("=" * 78)
    print(f"Live battery: {n_ok}/{n_total} ran with non-empty results.")
    print(
        f"Tokens total: prompt={total_prompt_tokens:,} (cached={total_cached:,})  "
        f"completion={total_completion:,}"
    )
    return 0 if n_ok == n_total else 1


if __name__ == "__main__":
    sys.exit(main())
