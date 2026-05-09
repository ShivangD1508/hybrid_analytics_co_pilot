"""Synthesize a single natural-language answer from the available sources.

One LLM call per question. Inputs are the user question, the router's
decision, and any of: a SQL run, retrieved doc chunks, retrieved review
excerpts. The output is a structured `AnswerDraft` carrying the answer
text, a self-reported confidence, and a short reasoning summary. The
caller wraps this with the chart selection and the source list to form
the full `SynthesisResult`.

The answer is asked to use inline citations (`[sql]`, `[doc:filename]`,
`[review:short_id]`) that downstream UI can link back to the sources.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import pandas as pd
from openai import OpenAI

from src.config import Config
from src.retriever import DocHit, ReviewHit
from src.router.classifier import RouterDecision
from src.sql_agent import SqlAgentRun


@dataclass(frozen=True)
class AnswerDraft:
    answer: str
    confidence: float
    reasoning_summary: str
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    latency_ms: int
    model: str


_RESPONSE_SCHEMA: dict = {
    "name": "answer_draft",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["answer", "confidence", "reasoning_summary"],
        "properties": {
            "answer": {"type": "string"},
            "confidence": {"type": "number"},
            "reasoning_summary": {"type": "string"},
        },
    },
}


_SYSTEM_PROMPT = """\
You synthesize a final answer for an analytics question, using whichever of
these sources are present: a SQL result table, retrieved methodology-doc
chunks, and retrieved customer-review excerpts.

VOICE
- Dry, technical, honest. Write like a staff data analyst, not a chatbot.
- No exclamation points, no marketing tone, no filler.
- 3-6 sentences for simple questions; up to ~12 for hybrid questions
  needing both data and context.

CITATIONS
- Use inline tags inside the answer:
  - `[sql]` after any number you cite from the SQL result table.
  - `[doc:<filename>]` after a definition or methodology claim, e.g.
    `[doc:kpi_definitions.md]`.
  - `[review:<short_id>]` when quoting or paraphrasing a customer review.
    `<short_id>` is the first 8 characters of the review's order_id, as
    shown in the source list.
- Do not invent citations. Cite only what is given to you.

HONESTY
- If the SQL returned no rows, say so. Do not fabricate numbers.
- If the docs do not contain the answer, say "no methodology doc covers
  this directly" rather than guessing.
- If you are stitching together sources whose conclusions disagree,
  surface the disagreement.

CONFIDENCE
- Self-report a confidence in 0.0-1.0.
- 0.85+ : sources directly answer the question, numbers are unambiguous.
- 0.50-0.85: partial coverage, some inference, or noisy retrieval.
- Below 0.50: the question is barely supported by the sources.

OUTPUT SCHEMA
- answer: the final natural-language answer with inline citations.
- confidence: float in 0.0-1.0.
- reasoning_summary: one sentence describing how the sources were combined,
  to append to the visible reasoning chain.
"""


class AnswerGenerator:
    """Wraps a single chat completion call with the synthesis system prompt."""

    def __init__(self, config: Config, client: OpenAI | None = None) -> None:
        self._config = config
        self._client = client or OpenAI(api_key=config.openai_api_key)

    def generate(
        self,
        question: str,
        router_decision: RouterDecision,
        sql_run: SqlAgentRun | None,
        doc_hits: list[DocHit] | None,
        review_hits: list[ReviewHit] | None,
    ) -> AnswerDraft:
        user_msg = _build_user_message(
            question=question,
            router_decision=router_decision,
            sql_run=sql_run,
            doc_hits=doc_hits,
            review_hits=review_hits,
        )

        t0 = time.perf_counter()
        resp = self._client.chat.completions.create(
            model=self._config.chat_model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_schema", "json_schema": _RESPONSE_SCHEMA},
            temperature=0,
        )
        latency_ms = int((time.perf_counter() - t0) * 1000)

        parsed = json.loads(resp.choices[0].message.content or "{}")
        usage = resp.usage
        cached = 0
        if usage and getattr(usage, "prompt_tokens_details", None):
            cached = getattr(usage.prompt_tokens_details, "cached_tokens", 0) or 0

        return AnswerDraft(
            answer=parsed["answer"].strip(),
            confidence=_clip01(float(parsed["confidence"])),
            reasoning_summary=parsed["reasoning_summary"].strip(),
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            cached_tokens=cached,
            latency_ms=latency_ms,
            model=resp.model,
        )


# ---------------------------------------------------------------------------
# User-message construction
# ---------------------------------------------------------------------------


# How many SQL result rows to show the synthesizer. Enough for it to ground
# its answer; trimmed so we don't blow up the prompt on large result sets.
_SQL_PREVIEW_ROWS = 25
# How much review text to keep per excerpt. Most reviews are short.
_REVIEW_TEXT_CAP = 400


def _build_user_message(
    *,
    question: str,
    router_decision: RouterDecision,
    sql_run: SqlAgentRun | None,
    doc_hits: list[DocHit] | None,
    review_hits: list[ReviewHit] | None,
) -> str:
    parts: list[str] = [
        f"QUESTION: {question.strip()}",
        "",
        f"ROUTER DECISION: {router_decision.route}",
        f"ROUTER REASONING: {router_decision.reasoning}",
        "",
    ]

    if sql_run is not None:
        parts.append(_format_sql_section(sql_run))
        parts.append("")

    if doc_hits:
        parts.append(_format_doc_section(doc_hits))
        parts.append("")

    if review_hits:
        parts.append(_format_review_section(review_hits))
        parts.append("")

    parts.append(
        "Write the answer now. Use the citation format described in the system message."
    )
    return "\n".join(parts)


def _format_sql_section(run: SqlAgentRun) -> str:
    g = run.generation
    v = run.validation
    r = run.result

    lines = ["SQL"]
    lines.append("Query:")
    lines.append("```sql")
    lines.append(g.sql.strip())
    lines.append("```")
    lines.append(f"Generator explanation: {g.explanation}")

    if not v.passed:
        lines.append(f"Validation FAILED: {v.error}")
        lines.append("No SQL results available.")
        return "\n".join(lines)

    if r is None:
        lines.append("No SQL result returned.")
        return "\n".join(lines)

    if r.error:
        lines.append(
            f"Execution ERROR: {r.error} (timed_out={r.timed_out}, {r.execution_ms}ms)"
        )
        return "\n".join(lines)

    df = r.df
    lines.append(f"Result: {r.row_count} rows, {len(r.columns)} cols, {r.execution_ms}ms")
    if df is not None and r.row_count > 0:
        preview = df.head(_SQL_PREVIEW_ROWS).to_string(index=False)
        lines.append("Preview (first {}):".format(min(_SQL_PREVIEW_ROWS, r.row_count)))
        lines.append("```")
        lines.append(preview)
        lines.append("```")
        if r.row_count > _SQL_PREVIEW_ROWS:
            lines.append(f"... ({r.row_count - _SQL_PREVIEW_ROWS} more rows omitted)")
    else:
        lines.append("Result was empty.")
    return "\n".join(lines)


def _format_doc_section(hits: list[DocHit]) -> str:
    lines = [f"DOC CHUNKS ({len(hits)} hits)"]
    for i, h in enumerate(hits, 1):
        lines.append(
            f"[{i}] file={h.filename}  chunk={h.chunk_index}  distance={h.distance:.3f}"
        )
        lines.append(h.text.strip())
        lines.append("")
    return "\n".join(lines).rstrip()


def _format_review_section(hits: list[ReviewHit]) -> str:
    lines = [f"REVIEW EXCERPTS ({len(hits)} hits)"]
    for h in hits:
        short = (h.order_id or "")[:8]
        cat = h.product_category or "(uncategorized)"
        text = h.text.strip()
        if len(text) > _REVIEW_TEXT_CAP:
            text = text[:_REVIEW_TEXT_CAP] + " ..."
        lines.append(
            f"[review:{short}] score={h.review_score} category={cat} date={h.review_creation_date[:10]}"
        )
        lines.append(text)
        lines.append("")
    return "\n".join(lines).rstrip()


def _clip01(v: float) -> float:
    if v < 0:
        return 0.0
    if v > 1:
        return 1.0
    return v


# Re-export for callers that want to inspect the prompt body.
def render_user_message_for_inspection(
    question: str,
    router_decision: RouterDecision,
    sql_run: SqlAgentRun | None,
    doc_hits: list[DocHit] | None,
    review_hits: list[ReviewHit] | None,
) -> str:
    return _build_user_message(
        question=question,
        router_decision=router_decision,
        sql_run=sql_run,
        doc_hits=doc_hits,
        review_hits=review_hits,
    )


# Quiet pyflakes for the imported pd (used only in type checking).
_ = pd
