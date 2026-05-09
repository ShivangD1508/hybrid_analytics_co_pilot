"""End-to-end pipeline smoke test.

Runs a small battery (one question per route) through `Pipeline.run` and
prints the answer, confidence, reasoning chain, chart spec, sources, and
per-stage latency / token breakdown.

Usage:
    python scripts/test_pipeline.py
    python scripts/test_pipeline.py "your question"
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from src.config import load_config
from src.pipeline import Pipeline


_BATTERY: list[str] = [
    "What was the total revenue across all orders in 2017?",         # sql
    "How is the seller performance score calculated?",               # docs
    "What are customers complaining about in the toys category?",    # reviews
    "Why are northern Brazilian states slower to receive orders?",   # hybrid
]


def _wrap(s: str, width: int = 92, indent: str = "  ") -> str:
    return textwrap.fill(
        s,
        width=width,
        initial_indent=indent,
        subsequent_indent=indent,
        break_long_words=False,
        break_on_hyphens=False,
    )


def _hr() -> None:
    print("=" * 92)


def _print_one(idx: int, total: int, q: str, result) -> None:
    _hr()
    print(f"[{idx}/{total}]  route={result.route:<7}  conf={result.confidence:.2f}  "
          f"total={result.total_ms:>5}ms  tokens={result.total_prompt_tokens + result.total_completion_tokens:,}")
    print(f"Q: {q}")
    print()
    print("-- ANSWER --")
    for line in textwrap.wrap(
        result.answer, width=92, replace_whitespace=False, drop_whitespace=False
    ):
        print(f"  {line}")

    print()
    print("-- REASONING CHAIN --")
    for i, step in enumerate(result.reasoning_chain, 1):
        print(f"  {i}. {_wrap(step).lstrip()}")

    print()
    print("-- CHART --")
    print(f"  type      : {result.chart_spec.chart_type}")
    print(f"  rationale : {result.chart_spec.rationale}")
    if result.chart_spec.x_column or result.chart_spec.y_column:
        print(f"  columns   : x={result.chart_spec.x_column}  y={result.chart_spec.y_column}")

    print()
    print("-- SOURCES --")
    counts: dict[str, int] = {}
    for s in result.sources:
        counts[s.type] = counts.get(s.type, 0) + 1
    print(f"  count: {sum(counts.values())} ({', '.join(f'{k}={v}' for k, v in sorted(counts.items()))})")
    for s in result.sources[:6]:
        if s.type == "sql":
            preview = s.query.split('\n')[0][:80]
            print(f"  [sql]    rows={s.rows}  cols={len(s.columns)}  {preview}...")
        elif s.type == "doc":
            print(f"  [doc]    {s.filename}#chunk-{s.chunk_index}  dist={s.distance:.3f}")
        elif s.type == "review":
            short = (s.order_id or "")[:8]
            print(f"  [review] [review:{short}]  score={s.score}  cat={s.category or '(none)'}  dist={s.distance:.3f}")
    if len(result.sources) > 6:
        print(f"  ... ({len(result.sources) - 6} more)")

    print()
    print("-- TIMINGS --")
    for t in result.timings:
        line = f"  {t.stage:<10} {t.latency_ms:>5}ms"
        if t.prompt_tokens or t.completion_tokens:
            line += (f"   prompt={t.prompt_tokens:>5} (cached={t.cached_tokens:>5}) "
                     f"completion={t.completion_tokens:>4}")
        print(line)
    print()


def main() -> int:
    cfg = load_config()
    pipeline = Pipeline(config=cfg)

    questions = [" ".join(sys.argv[1:])] if len(sys.argv) > 1 else _BATTERY

    print(f"Model       : {cfg.chat_model}")
    print(f"Embed model : {cfg.embed_model}")
    print(f"Battery     : {len(questions)} question(s)")

    grand_ms = 0
    grand_tokens = 0
    grand_cached = 0
    routes: dict[str, int] = {}
    for i, q in enumerate(questions, 1):
        r = pipeline.run(q)
        _print_one(i, len(questions), q, r)
        grand_ms += r.total_ms
        grand_tokens += r.total_prompt_tokens + r.total_completion_tokens
        grand_cached += r.total_cached_tokens
        routes[r.route] = routes.get(r.route, 0) + 1

    _hr()
    print(f"Aggregate over {len(questions)} questions:")
    print(f"  routes used     : {dict(sorted(routes.items()))}")
    print(f"  total wall time : {grand_ms:,}ms  (avg {grand_ms // len(questions):,}ms)")
    print(f"  total tokens    : {grand_tokens:,} (cached={grand_cached:,})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
