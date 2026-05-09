"""Smoke-test the routing classifier on a small spread of question types.

Reads OPENAI_API_KEY from .env. Uses the live SQLite schema and the live
markdown doc list, so this also doubles as a check that schema introspection
and doc-summary loading work.

Usage:
    python scripts/test_router.py             # default 8-question battery
    python scripts/test_router.py "your custom question here"
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from src.config import load_config
from src.db.schema import introspect_schema
from src.router.classifier import Router, load_doc_summaries


_BATTERY: list[tuple[str, str]] = [
    ("sql",     "What was the total revenue in 2017?"),
    ("sql",     "Top 5 customer states by order count."),
    ("docs",    "How is the seller performance score calculated?"),
    ("docs",    "What is RFM and how do we use it here?"),
    ("reviews", "What are customers complaining about in the bed_bath_table category?"),
    ("reviews", "Summarize feedback about packaging."),
    ("hybrid",  "Why is the on-time delivery rate worse for northern states?"),
    ("hybrid",  "Which segments drive the most revenue and what should we do about it?"),
]


def _truncate(s: str, n: int = 110) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 3] + "..."


def main() -> int:
    cfg = load_config()
    schema = introspect_schema(cfg.sqlite_path)
    docs = load_doc_summaries(cfg.repo_root / "data" / "docs")

    print(f"Model       : {cfg.chat_model}")
    print(f"Schema      : {len(schema.tables)} tables")
    print(f"Doc titles  : {len(docs)}")
    router = Router(schema=schema, docs=docs, config=cfg)
    sys_tok_est = len(router.system_prompt) // 4
    print(f"System prompt length: {len(router.system_prompt):,} chars (~{sys_tok_est:,} tokens)\n")

    if len(sys.argv) > 1:
        battery = [("(custom)", " ".join(sys.argv[1:]))]
    else:
        battery = _BATTERY

    correct = 0
    total_prompt = total_completion = total_cached = 0
    total_latency = 0
    for expected, q in battery:
        d = router.classify(q)
        ok = "OK " if d.route == expected else "MISS"
        if d.route == expected:
            correct += 1
        total_prompt += d.prompt_tokens
        total_completion += d.completion_tokens
        total_cached += d.cached_tokens
        total_latency += d.latency_ms

        print(f"[{ok}] expected={expected:<7} got={d.route:<7}  {_truncate(q)}")
        print(f"       reasoning : {_truncate(d.reasoning, 100)}")
        if d.sql_tables_needed:
            print(f"       sql_tables: {list(d.sql_tables_needed)}")
        if d.doc_query:
            print(f"       doc_query : {d.doc_query}")
        if d.review_query:
            print(f"       review_q  : {d.review_query}")
        print(f"       tokens    : prompt={d.prompt_tokens} (cached={d.cached_tokens}) completion={d.completion_tokens}  latency={d.latency_ms}ms")
        print()

    n = len(battery)
    print("=" * 78)
    print(f"Routing accuracy on battery: {correct}/{n} ({100*correct/n:.0f}%)")
    print(f"Tokens total: prompt={total_prompt:,} (cached={total_cached:,})  completion={total_completion:,}")
    avg_latency = total_latency / n
    print(f"Latency: avg {avg_latency:.0f}ms over {n} calls")
    return 0 if correct == n else 1


if __name__ == "__main__":
    sys.exit(main())
