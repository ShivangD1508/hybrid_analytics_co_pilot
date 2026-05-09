"""End-to-end evaluation harness.

Runs the test set in `eval/test_questions.json` through `Pipeline.run`,
scores each question against the expected fields, then writes:
- `eval/results/raw.json` -- per-question raw scores (for re-analysis)
- `eval/results/report.md` -- the human-readable summary

The eval intentionally measures the agent end-to-end. Per-stage failures
(routing miss, SQL exec error, retrieval miss, low keyword coverage) are
attributed individually so failure analysis can pinpoint the responsible
stage.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from src.config import load_config
from src.pipeline import Pipeline, PipelineResult


# Pricing (May 2026, gpt-4o-mini). Embedding cost is not tracked.
_PRICE_PER_1M = {"input": 0.15, "cached_input": 0.075, "output": 0.60}


def _estimate_cost(prompt: int, cached: int, completion: int) -> float:
    return (
        max(0, prompt - cached) * _PRICE_PER_1M["input"] / 1_000_000
        + cached * _PRICE_PER_1M["cached_input"] / 1_000_000
        + completion * _PRICE_PER_1M["output"] / 1_000_000
    )


@dataclass
class QuestionScore:
    id: str
    difficulty: str
    question: str
    expected_route: str
    actual_route: str
    routing_correct: bool

    expected_sql_tables: list[str] | None
    sql_executed: bool | None  # None when route did not fire SQL
    sql_returned_rows: bool | None
    sql_error: str | None

    expected_doc_titles: list[str] | None
    doc_titles_retrieved: list[str]
    doc_hit: bool | None  # None when no expected_doc_titles

    expected_keywords: list[str]
    keyword_matches: list[bool]
    keyword_coverage: float

    expected_chart_type: str | None
    actual_chart_type: str
    chart_correct: bool | None

    confidence: float
    answer: str

    timings: dict[str, int]
    total_ms: int
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int
    cost_usd: float

    error: str | None = None


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score_question(spec: dict, result: PipelineResult) -> QuestionScore:
    answer_lc = result.answer.lower()
    expected_keywords = list(spec.get("expected_answer_contains") or [])
    keyword_matches = [kw.lower() in answer_lc for kw in expected_keywords]
    keyword_coverage = (
        sum(keyword_matches) / len(keyword_matches) if keyword_matches else 1.0
    )

    sql_executed: bool | None = None
    sql_returned_rows: bool | None = None
    sql_error: str | None = None
    if result.sql_run is not None:
        if result.sql_run.result is None:
            sql_executed = False
            sql_error = result.sql_run.validation.error
        else:
            sql_executed = result.sql_run.result.error is None
            sql_returned_rows = result.sql_run.result.row_count > 0
            sql_error = result.sql_run.result.error

    expected_docs = spec.get("expected_doc_titles") or []
    doc_titles_retrieved = [h.filename for h in result.doc_hits]
    doc_hit: bool | None
    if expected_docs:
        retrieved_set = set(doc_titles_retrieved)
        doc_hit = any(d in retrieved_set for d in expected_docs)
    else:
        doc_hit = None

    expected_chart = spec.get("expected_chart_type")
    actual_chart = result.chart_spec.chart_type
    chart_correct: bool | None
    if expected_chart is None:
        chart_correct = None
    else:
        chart_correct = expected_chart == actual_chart

    timings_dict = {t.stage: t.latency_ms for t in result.timings}
    cost = _estimate_cost(
        result.total_prompt_tokens,
        result.total_cached_tokens,
        result.total_completion_tokens,
    )

    return QuestionScore(
        id=spec["id"],
        difficulty=spec.get("difficulty", "?"),
        question=spec["question"],
        expected_route=spec["expected_route"],
        actual_route=result.route,
        routing_correct=result.route == spec["expected_route"],
        expected_sql_tables=spec.get("expected_sql_tables"),
        sql_executed=sql_executed,
        sql_returned_rows=sql_returned_rows,
        sql_error=sql_error,
        expected_doc_titles=spec.get("expected_doc_titles"),
        doc_titles_retrieved=doc_titles_retrieved,
        doc_hit=doc_hit,
        expected_keywords=expected_keywords,
        keyword_matches=keyword_matches,
        keyword_coverage=keyword_coverage,
        expected_chart_type=expected_chart,
        actual_chart_type=actual_chart,
        chart_correct=chart_correct,
        confidence=result.confidence,
        answer=result.answer,
        timings=timings_dict,
        total_ms=result.total_ms,
        prompt_tokens=result.total_prompt_tokens,
        cached_tokens=result.total_cached_tokens,
        completion_tokens=result.total_completion_tokens,
        cost_usd=cost,
    )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((len(s) - 1) * p))))
    return s[k]


def aggregate(scores: list[QuestionScore]) -> dict:
    n = len(scores)
    routes = ["sql", "docs", "reviews", "hybrid"]

    # Confusion matrix.
    confusion: dict[str, dict[str, int]] = {a: {p: 0 for p in routes} for a in routes}
    for s in scores:
        if s.expected_route in confusion and s.actual_route in confusion[s.expected_route]:
            confusion[s.expected_route][s.actual_route] += 1

    routing_correct = [s for s in scores if s.routing_correct]
    by_route_acc = {}
    for r in routes:
        bucket = [s for s in scores if s.expected_route == r]
        if bucket:
            by_route_acc[r] = (
                sum(1 for s in bucket if s.routing_correct),
                len(bucket),
            )

    # SQL execution / row return.
    sql_fired = [s for s in scores if s.sql_executed is not None]
    sql_executed_ok = [s for s in sql_fired if s.sql_executed]
    sql_with_rows = [s for s in sql_fired if s.sql_returned_rows]

    # Doc retrieval hits.
    doc_evaluated = [s for s in scores if s.doc_hit is not None]
    doc_hits = [s for s in doc_evaluated if s.doc_hit]

    # Chart appropriateness.
    chart_evaluated = [s for s in scores if s.chart_correct is not None]
    chart_correct = [s for s in chart_evaluated if s.chart_correct]

    # Keyword coverage.
    coverages = [s.keyword_coverage for s in scores if s.expected_keywords]

    # Per-stage latency p50/p95.
    stage_latencies: dict[str, list[int]] = {}
    for s in scores:
        for stage, ms in s.timings.items():
            stage_latencies.setdefault(stage, []).append(ms)

    stage_stats = {
        stage: {
            "n": len(vals),
            "p50": _percentile(vals, 0.5),
            "p95": _percentile(vals, 0.95),
            "max": max(vals),
        }
        for stage, vals in stage_latencies.items()
    }
    total_latencies = [s.total_ms for s in scores]
    stage_stats["TOTAL"] = {
        "n": len(total_latencies),
        "p50": _percentile(total_latencies, 0.5),
        "p95": _percentile(total_latencies, 0.95),
        "max": max(total_latencies) if total_latencies else None,
    }

    total_prompt = sum(s.prompt_tokens for s in scores)
    total_completion = sum(s.completion_tokens for s in scores)
    total_cached = sum(s.cached_tokens for s in scores)
    total_cost = sum(s.cost_usd for s in scores)

    return {
        "n": n,
        "routing_accuracy": (len(routing_correct), n),
        "routing_accuracy_by_route": by_route_acc,
        "confusion": confusion,
        "sql_execution_rate": (len(sql_executed_ok), len(sql_fired)),
        "sql_returned_rows_rate": (len(sql_with_rows), len(sql_fired)),
        "doc_retrieval_hit_rate": (len(doc_hits), len(doc_evaluated)),
        "chart_appropriateness_rate": (len(chart_correct), len(chart_evaluated)),
        "avg_keyword_coverage": (
            statistics.mean(coverages) if coverages else 0.0
        ),
        "stage_stats": stage_stats,
        "totals": {
            "prompt_tokens": total_prompt,
            "completion_tokens": total_completion,
            "cached_tokens": total_cached,
            "cost_usd": total_cost,
            "wall_time_ms": sum(s.total_ms for s in scores),
        },
    }


# ---------------------------------------------------------------------------
# Failure analysis
# ---------------------------------------------------------------------------


@dataclass
class Failure:
    score: QuestionScore
    reasons: list[str] = field(default_factory=list)


def collect_failures(scores: list[QuestionScore]) -> list[Failure]:
    out: list[Failure] = []
    for s in scores:
        reasons: list[str] = []
        if not s.routing_correct:
            reasons.append(
                f"router predicted '{s.actual_route}', expected '{s.expected_route}'"
            )
        if s.sql_executed is False:
            reasons.append(
                f"SQL did not execute: {s.sql_error or 'unknown error'}"
            )
        if s.sql_returned_rows is False:
            reasons.append("SQL returned 0 rows")
        if s.doc_hit is False:
            expected = ", ".join(s.expected_doc_titles or [])
            got = ", ".join(s.doc_titles_retrieved) or "(none)"
            reasons.append(
                f"expected doc not in top-{len(s.doc_titles_retrieved)}: "
                f"expected one of [{expected}], got [{got}]"
            )
        if s.expected_keywords and s.keyword_coverage < 0.5:
            misses = [
                k for k, m in zip(s.expected_keywords, s.keyword_matches) if not m
            ]
            reasons.append(
                f"keyword coverage {s.keyword_coverage:.2f} -- missed: {misses}"
            )
        if s.chart_correct is False:
            reasons.append(
                f"chart predicted '{s.actual_chart_type}', expected '{s.expected_chart_type}'"
            )
        if reasons:
            out.append(Failure(score=s, reasons=reasons))
    return out


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------


def format_report(
    scores: list[QuestionScore], agg: dict, failures: list[Failure]
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines: list[str] = []
    lines.append("# Evaluation report")
    lines.append("")
    lines.append(f"Run at: {now}")
    lines.append(
        f"Pipeline: gpt-4o-mini (chat) + text-embedding-3-small (embeddings) + "
        "ChromaDB persistent local"
    )
    lines.append(f"Test set: `eval/test_questions.json` -- {agg['n']} questions")
    lines.append("")

    # Headline metrics.
    lines.append("## Headline metrics")
    lines.append("")
    rc_num, rc_den = agg["routing_accuracy"]
    sq_num, sq_den = agg["sql_execution_rate"]
    sr_num, sr_den = agg["sql_returned_rows_rate"]
    dr_num, dr_den = agg["doc_retrieval_hit_rate"]
    ch_num, ch_den = agg["chart_appropriateness_rate"]
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(
        f"| Routing accuracy | {rc_num}/{rc_den} ({100 * rc_num / rc_den:.0f}%) |"
    )
    if sq_den:
        lines.append(
            f"| SQL execution rate | {sq_num}/{sq_den} ({100 * sq_num / sq_den:.0f}%) |"
        )
        lines.append(
            f"| SQL returned >=1 row | {sr_num}/{sr_den} ({100 * sr_num / sr_den:.0f}%) |"
        )
    if dr_den:
        lines.append(
            f"| Doc retrieval hit rate (top-5) | {dr_num}/{dr_den} ({100 * dr_num / dr_den:.0f}%) |"
        )
    if ch_den:
        lines.append(
            f"| Chart appropriateness | {ch_num}/{ch_den} ({100 * ch_num / ch_den:.0f}%) |"
        )
    lines.append(
        f"| Average keyword coverage | {agg['avg_keyword_coverage']:.2f} |"
    )
    t = agg["totals"]
    lines.append(
        f"| Total tokens | {t['prompt_tokens'] + t['completion_tokens']:,} "
        f"(prompt {t['prompt_tokens']:,} of which cached {t['cached_tokens']:,}; "
        f"completion {t['completion_tokens']:,}) |"
    )
    lines.append(f"| Estimated chat-completion cost | ${t['cost_usd']:.4f} |")
    lines.append(f"| Total wall time | {t['wall_time_ms'] / 1000:.1f}s |")
    lines.append("")

    # Routing accuracy by expected route.
    lines.append("### Routing accuracy by expected route")
    lines.append("")
    lines.append("| Route | Correct | Total | % |")
    lines.append("|---|---|---|---|")
    for r, (num, den) in sorted(agg["routing_accuracy_by_route"].items()):
        lines.append(f"| {r} | {num} | {den} | {100 * num / den:.0f}% |")
    lines.append("")

    # Confusion matrix.
    lines.append("### Routing confusion matrix")
    lines.append("")
    lines.append("(Rows = expected, columns = predicted.)")
    lines.append("")
    lines.append("| expected \\ predicted | sql | docs | reviews | hybrid |")
    lines.append("|---|---|---|---|---|")
    for actual in ["sql", "docs", "reviews", "hybrid"]:
        row = agg["confusion"][actual]
        lines.append(
            f"| **{actual}** | {row['sql']} | {row['docs']} | {row['reviews']} | {row['hybrid']} |"
        )
    lines.append("")

    # Latency.
    lines.append("## Latency per stage (ms)")
    lines.append("")
    lines.append("| Stage | n | p50 | p95 | max |")
    lines.append("|---|---|---|---|---|")
    stage_order = ["router", "sql", "docs", "reviews", "synthesis", "TOTAL"]
    for stage in stage_order:
        if stage not in agg["stage_stats"]:
            continue
        s = agg["stage_stats"][stage]
        if not s["n"]:
            continue
        lines.append(
            f"| {stage} | {s['n']} | {s['p50']} | {s['p95']} | {s['max']} |"
        )
    lines.append("")

    # Per-question table.
    lines.append("## Per-question results")
    lines.append("")
    lines.append(
        "| ID | Diff | Route OK | SQL exec | Doc hit | Chart OK | Keyword cov. | Conf | Total ms |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for s in scores:
        def _b(v: bool | None) -> str:
            if v is None:
                return "-"
            return "yes" if v else "**NO**"

        lines.append(
            f"| `{s.id}` | {s.difficulty} | "
            f"{_b(s.routing_correct)} ({s.actual_route}) | "
            f"{_b(s.sql_executed)} | {_b(s.doc_hit)} | "
            f"{_b(s.chart_correct)} ({s.actual_chart_type}) | "
            f"{s.keyword_coverage:.2f} | {s.confidence:.2f} | "
            f"{s.total_ms} |"
        )
    lines.append("")

    # Failure analysis.
    lines.append("## Failure analysis")
    lines.append("")
    if not failures:
        lines.append(
            "No question failed any individual check. Compare to "
            "`avg_keyword_coverage` for soft-failure signal."
        )
    else:
        lines.append(
            f"Out of {agg['n']} questions, **{len(failures)} had at least one "
            "failed check**. Each is listed below with the failed check(s) and "
            "the agent's actual answer for comparison."
        )
        lines.append("")
        for f in failures:
            s = f.score
            lines.append(f"### `{s.id}` ({s.difficulty}) -- {s.question}")
            lines.append("")
            for r in f.reasons:
                lines.append(f"- {r}")
            lines.append("")
            lines.append(f"**Answer (confidence {s.confidence:.2f}):**")
            lines.append("")
            lines.append("> " + s.answer.replace("\n", "\n> "))
            lines.append("")

    # Notes.
    lines.append("## Notes and known limitations")
    lines.append("")
    lines.append(
        "- The eval test set covers `sql`, `docs`, and `hybrid` routes only "
        "(10 each). The router supports a `reviews` route but no reviews-only "
        "questions appear in this set; the pipeline smoke test in Step 8 "
        "exercises that route."
    )
    lines.append(
        "- Keyword-coverage is a substring check, not an LLM judge. It rewards "
        "answers that quote the exact expected token. Paraphrased answers can "
        "score lower than they deserve; that is a known limitation of this "
        "metric."
    )
    lines.append(
        "- SQL retry tokens are partly under-counted: when the SQL agent retries "
        "after a validation failure (typically a missing `LIMIT` on a single-row "
        "aggregate), only the second attempt's tokens land in the report. "
        "Latency is captured in full."
    )
    lines.append(
        "- Embedding-token cost for retrieval queries is not tracked. Each "
        "retrieval query embeds ~10-50 tokens, so the per-eval miss is a few "
        "thousand tokens at $0.02/M -- well under one cent."
    )
    lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    cfg = load_config()
    pipeline = Pipeline(config=cfg)

    questions_path = _REPO_ROOT / "eval" / "test_questions.json"
    out_dir = _REPO_ROOT / "eval" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)

    questions: list[dict] = json.loads(questions_path.read_text(encoding="utf-8"))
    print(f"Loaded {len(questions)} questions from {questions_path}")
    print(f"Pipeline ready (chat={cfg.chat_model}, embed={cfg.embed_model}).")
    print()

    scores: list[QuestionScore] = []
    t0 = time.perf_counter()
    for i, q in enumerate(questions, 1):
        try:
            result = pipeline.run(q["question"])
            score = score_question(q, result)
        except Exception as exc:  # noqa: BLE001 -- record and continue
            score = QuestionScore(
                id=q["id"],
                difficulty=q.get("difficulty", "?"),
                question=q["question"],
                expected_route=q["expected_route"],
                actual_route="ERROR",
                routing_correct=False,
                expected_sql_tables=q.get("expected_sql_tables"),
                sql_executed=None,
                sql_returned_rows=None,
                sql_error=None,
                expected_doc_titles=q.get("expected_doc_titles"),
                doc_titles_retrieved=[],
                doc_hit=None,
                expected_keywords=q.get("expected_answer_contains") or [],
                keyword_matches=[False] * len(q.get("expected_answer_contains") or []),
                keyword_coverage=0.0,
                expected_chart_type=q.get("expected_chart_type"),
                actual_chart_type="none",
                chart_correct=None,
                confidence=0.0,
                answer="",
                timings={},
                total_ms=0,
                prompt_tokens=0,
                cached_tokens=0,
                completion_tokens=0,
                cost_usd=0.0,
                error=f"{type(exc).__name__}: {exc}",
            )
        scores.append(score)
        ok = "OK " if score.routing_correct else "MISS"
        print(
            f"  [{i:>2}/{len(questions)}] {ok} {score.id:<10} "
            f"route={score.actual_route:<7} kw={score.keyword_coverage:.2f} "
            f"conf={score.confidence:.2f} t={score.total_ms}ms"
        )
    elapsed = time.perf_counter() - t0
    print(f"\nFinished in {elapsed:.1f}s.\n")

    agg = aggregate(scores)
    failures = collect_failures(scores)

    raw_path = out_dir / "raw.json"
    raw_path.write_text(
        json.dumps([asdict(s) for s in scores], indent=2, default=str),
        encoding="utf-8",
    )
    print(f"Wrote {raw_path}")

    report = format_report(scores, agg, failures)
    report_path = out_dir / "report.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"Wrote {report_path}")

    rc_num, rc_den = agg["routing_accuracy"]
    print(
        f"\nRouting accuracy {rc_num}/{rc_den}, "
        f"keyword coverage avg {agg['avg_keyword_coverage']:.2f}, "
        f"failures {len(failures)}, "
        f"cost ${agg['totals']['cost_usd']:.3f}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
