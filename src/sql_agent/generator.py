"""Generate SQL from a natural-language question.

The schema and few-shot examples are baked into the system prompt at
construction time, so each `generate(...)` call sends only the user
question and OpenAI's prompt cache amortizes the static prefix.

Output is constrained by a JSON schema with two fields: `sql` and
`explanation`. The validator then enforces that the SQL is read-only and
includes a sane LIMIT.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from openai import OpenAI

from src.config import Config
from src.db.schema import DatabaseSchema, format_schema_for_llm


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SqlGeneration:
    sql: str
    explanation: str
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    latency_ms: int
    model: str


# ---------------------------------------------------------------------------
# Structured-output schema
# ---------------------------------------------------------------------------


_RESPONSE_SCHEMA: dict = {
    "name": "sql_generation",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["sql", "explanation"],
        "properties": {
            "sql": {"type": "string"},
            "explanation": {"type": "string"},
        },
    },
}


# ---------------------------------------------------------------------------
# Few-shot examples
# ---------------------------------------------------------------------------


_FEW_SHOTS: tuple[tuple[str, dict], ...] = (
    (
        "Top 10 product categories by order count",
        {
            "sql": (
                "SELECT t.product_category_name_english AS category, "
                "COUNT(DISTINCT oi.order_id) AS n_orders\n"
                "FROM order_items AS oi\n"
                "JOIN products AS p ON p.product_id = oi.product_id\n"
                "JOIN product_category_translation AS t "
                "ON t.product_category_name = p.product_category_name\n"
                "GROUP BY t.product_category_name_english\n"
                "ORDER BY n_orders DESC\n"
                "LIMIT 10;"
            ),
            "explanation": (
                "Counts distinct orders per category by joining items to "
                "products and the English-name translation; descending order, "
                "top 10."
            ),
        },
    ),
    (
        "Average delivery time in days, by customer state, only for delivered orders",
        {
            "sql": (
                "SELECT c.customer_state AS state,\n"
                "       AVG(julianday(o.order_delivered_customer_date) "
                "- julianday(o.order_purchase_timestamp)) AS avg_days,\n"
                "       COUNT(*) AS n_orders\n"
                "FROM orders AS o\n"
                "JOIN customers AS c ON c.customer_id = o.customer_id\n"
                "WHERE o.order_status = 'delivered'\n"
                "  AND o.order_delivered_customer_date IS NOT NULL\n"
                "GROUP BY c.customer_state\n"
                "ORDER BY avg_days DESC\n"
                "LIMIT 100;"
            ),
            "explanation": (
                "Joins orders to customers for state, computes per-state mean "
                "delivery time using julianday() arithmetic on the purchase "
                "and customer-delivery timestamps; filters to delivered orders "
                "with a non-null delivery date."
            ),
        },
    ),
    (
        "Monthly revenue trend in 2017, where revenue = price + freight on order_items.",
        {
            "sql": (
                "SELECT strftime('%Y-%m', o.order_purchase_timestamp) AS month,\n"
                "       SUM(oi.price + oi.freight_value) AS revenue,\n"
                "       COUNT(DISTINCT o.order_id) AS n_orders\n"
                "FROM orders AS o\n"
                "JOIN order_items AS oi ON oi.order_id = o.order_id\n"
                "WHERE o.order_purchase_timestamp >= '2017-01-01'\n"
                "  AND o.order_purchase_timestamp <  '2018-01-01'\n"
                "GROUP BY month\n"
                "ORDER BY month\n"
                "LIMIT 12;"
            ),
            "explanation": (
                "Monthly bucket via strftime, revenue summed from item-side "
                "(price + freight). Time filter on order_purchase_timestamp."
            ),
        },
    ),
    (
        "Sellers with the highest cancellation rate, considering only sellers with at least 50 orders.",
        {
            "sql": (
                "SELECT oi.seller_id,\n"
                "       AVG(CASE WHEN o.order_status = 'canceled' "
                "THEN 1.0 ELSE 0.0 END) AS cancel_rate,\n"
                "       COUNT(DISTINCT o.order_id) AS n_orders\n"
                "FROM order_items AS oi\n"
                "JOIN orders AS o ON o.order_id = oi.order_id\n"
                "GROUP BY oi.seller_id\n"
                "HAVING COUNT(DISTINCT o.order_id) >= 50\n"
                "ORDER BY cancel_rate DESC\n"
                "LIMIT 20;"
            ),
            "explanation": (
                "Per-seller cancellation rate computed as AVG over a CASE "
                "indicator. HAVING enforces the minimum order count after "
                "aggregation."
            ),
        },
    ),
)


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


class SqlGenerator:
    """Generates SQLite SELECT queries from natural-language questions."""

    def __init__(
        self,
        schema: DatabaseSchema,
        config: Config,
        client: OpenAI | None = None,
    ) -> None:
        self._config = config
        self._client = client or OpenAI(api_key=config.openai_api_key)
        self._system_prompt = self._build_system_prompt(schema, config.sql_row_limit)

    @staticmethod
    def _build_system_prompt(schema: DatabaseSchema, max_rows: int) -> str:
        schema_block = format_schema_for_llm(schema).rstrip()
        examples_block = "\n\n".join(
            f"Q: {q}\nA: {json.dumps(a, ensure_ascii=False)}" for q, a in _FEW_SHOTS
        )
        return _SYSTEM_PROMPT_TEMPLATE.format(
            schema_block=schema_block,
            examples_block=examples_block,
            max_rows=max_rows,
        )

    def generate(self, question: str) -> SqlGeneration:
        """Run one generation call. Raises only on transport errors."""
        if not question or not question.strip():
            raise ValueError("question must be non-empty")

        t0 = time.perf_counter()
        resp = self._client.chat.completions.create(
            model=self._config.chat_model,
            messages=[
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": question.strip()},
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

        return SqlGeneration(
            sql=parsed["sql"].strip(),
            explanation=parsed["explanation"].strip(),
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            cached_tokens=cached,
            latency_ms=latency_ms,
            model=resp.model,
        )

    @property
    def system_prompt(self) -> str:
        return self._system_prompt


_SYSTEM_PROMPT_TEMPLATE = """\
You translate natural-language questions about the Olist Brazilian
e-commerce dataset into SQLite SELECT queries. The SQL you produce is
executed verbatim against the database; correctness and safety are
required.

HARD RULES (enforced by a downstream validator)
- Output is read-only. Use SELECT or WITH only. Do not emit INSERT,
  UPDATE, DELETE, DROP, ALTER, CREATE, ATTACH, DETACH, REPLACE, PRAGMA,
  TRUNCATE, GRANT, REVOKE, VACUUM, or REINDEX.
- Single statement only. No semicolons inside the query.
- Always include a LIMIT clause. Maximum allowed is {max_rows}; pick a
  smaller LIMIT when the question implies a small result (e.g. "top 10",
  "monthly trend in 2017").

STYLE RULES
- Use table aliases on every FROM and JOIN (e.g. `orders AS o`).
- Qualify every column with its alias when more than one table is in
  scope.
- Date columns are TEXT in ISO 8601. Use `julianday()` for day-level
  arithmetic and `strftime('%Y-%m', ...)` or `date(...)` for grouping
  and filtering.
- Customer grain: `customer_unique_id` is the stable customer; the
  `customer_id` in `orders` and `customers` is per-order. Use
  `customer_unique_id` for any repeat-purchase, RFM, or CLV question.
- Filter `WHERE order_status = 'delivered'` for delivery-time analyses
  and exclude `IS NULL` on delivery dates.
- For revenue, prefer the item side: `SUM(price + freight_value)` from
  `order_items`. The `order_payments` side has voucher-leg quirks.
- Translate Portuguese product categories to English by joining
  `product_category_translation` (LEFT JOIN if you need to keep
  uncategorized products).

DATABASE SCHEMA
{schema_block}

OUTPUT
Return JSON with two fields:
- sql: the SQLite query, including a trailing semicolon.
- explanation: 1-2 sentences on why the query is shaped this way
  (joins chosen, filter rationale, aggregation grain).

FEW-SHOT EXAMPLES
{examples_block}
"""
