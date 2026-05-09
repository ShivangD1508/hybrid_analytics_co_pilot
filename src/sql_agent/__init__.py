"""Top-level SQL agent: generate a query, validate it, then execute it.

This is a thin orchestrator over `generator.py`, `validator.py`, and
`executor.py`. The pipeline (Step 8) calls this when the router routes a
question to `sql` or `hybrid`. Direct callers (the smoke-test script,
the eval harness) use `run_sql_for_question` to get a single result
object back.

If the first generation fails validation, the agent retries once with
the validator's error appended to the user message. The retry is bounded
to one attempt — repeated validation failures are reported as-is rather
than burning tokens in a loop.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.config import Config
from src.sql_agent.executor import SqlResult, execute
from src.sql_agent.generator import SqlGeneration, SqlGenerator
from src.sql_agent.validator import ValidationResult, validate


@dataclass(frozen=True)
class SqlAgentRun:
    generation: SqlGeneration
    validation: ValidationResult
    result: SqlResult | None  # None when validation failed
    retried: bool
    total_ms: int


def run_sql_for_question(
    question: str,
    generator: SqlGenerator,
    config: Config,
) -> SqlAgentRun:
    """Generate -> validate -> execute, with a single retry on validation failure."""
    gen = generator.generate(question)
    val = validate(
        sql=gen.sql,
        sqlite_path=config.sqlite_path,
        max_rows=config.sql_row_limit,
    )

    total_ms = gen.latency_ms
    retried = False

    if not val.passed:
        retry_question = (
            f"{question}\n\n"
            f"The previous SQL was rejected by the validator with this error: "
            f"{val.error}. Generate a corrected query. The previous attempt was:\n"
            f"{gen.sql}"
        )
        gen2 = generator.generate(retry_question)
        val2 = validate(
            sql=gen2.sql,
            sqlite_path=config.sqlite_path,
            max_rows=config.sql_row_limit,
        )
        total_ms += gen2.latency_ms
        retried = True
        gen, val = gen2, val2

    if not val.passed:
        return SqlAgentRun(
            generation=gen, validation=val, result=None, retried=retried, total_ms=total_ms
        )

    res = execute(
        sql=val.normalized_sql,
        sqlite_path=config.sqlite_path,
        timeout_seconds=config.sql_timeout_seconds,
    )
    total_ms += res.execution_ms
    return SqlAgentRun(
        generation=gen, validation=val, result=res, retried=retried, total_ms=total_ms
    )


__all__ = [
    "SqlAgentRun",
    "SqlGeneration",
    "SqlGenerator",
    "SqlResult",
    "ValidationResult",
    "execute",
    "run_sql_for_question",
    "validate",
]
