# Architecture

Module-by-module design rationale, the data flow per route, and the
caching / safety / failure-handling stories. Aimed at a reader who has
already read the [README](README.md) and now wants the engineering
specifics. Numbers below are from the eval run on 2026-05-09.

## At a glance

Single in-process pipeline. One `Pipeline` instance per process,
re-entrant on `run(question)`. All long-lived state -- the introspected
schema, the LLM clients, the retriever handles -- is built once at
`__init__`. Per-question work is one router call, zero or more tool
calls, one synthesizer call.

```
src/
├── config.py            # one Config dataclass; .env via python-dotenv
├── db/
│   ├── loader.py        # 9 TableSpecs -> CSV -> SQLite
│   └── schema.py        # PRAGMA-based introspection -> markdown for LLM
├── router/classifier.py # 1 LLM call, structured JSON output
├── sql_agent/
│   ├── generator.py     # NL question -> SQLite SELECT
│   ├── validator.py     # 3-layer safety check
│   ├── executor.py      # read-only conn + wall-clock timeout
│   └── __init__.py      # generate -> validate -> execute, with retry
├── retriever/
│   ├── embedder.py      # OpenAI batched + chunker + ingestion
│   ├── doc_retriever.py
│   └── review_retriever.py
├── synthesizer/
│   ├── answer_generator.py  # 1 LLM call, structured JSON output
│   ├── chart_selector.py    # rules-based, no LLM
│   └── __init__.py          # synthesize() orchestrator
├── charts/plotly_charts.py
└── pipeline.py          # the public API for the UI and the eval harness
```

## Components

### Router (`src/router/classifier.py`)

One chat-completion call. Structured outputs with a JSON schema that
constrains the response to four enumerated routes plus three optional
hint fields. The system prompt holds the full database schema (compact
markdown, ~2,500 tokens) and 12 few-shot examples (4 sql, 3 docs, 2
reviews, 3 hybrid). The user message is just the question; this maximises
the prompt-cache hit rate after the first call.

**Decision**: a single LLM call instead of a two-stage retrieval-augmented
classifier. At 30 questions the single-call model hit 93% accuracy
without retrieval; the cost is ~$0.0003/call after caching. A two-stage
system would add latency and tokens for marginal accuracy gain. Revisit
if scaling the eval to 300+ questions reveals a systematic miss class.

**Output fields** are advisory. `sql_tables_needed` is a hint the SQL
generator does not consume (it gets the full schema). `doc_query` and
`review_query` are passed verbatim to the retrievers. `reasoning` ends
up in the user-visible reasoning chain.

### SQL agent (`src/sql_agent/`)

Three stages with a single retry loop on validation failure.

`generator.py` produces structured JSON `{sql, explanation}`. The system
prompt includes the full schema with sample categorical values, hard
rules (SELECT-only, single statement, mandatory `LIMIT`), style rules
(table aliases, `julianday()` for date deltas, item-side revenue), and
four few-shot multi-table queries that demonstrate joins, aggregates,
`HAVING`, and `strftime` time-bucketing.

`validator.py` runs three layers in order:
1. **Static.** Strip strings and comments, then check for forbidden
   keywords (DDL/DML), multiple statements, leading SELECT/WITH, presence
   of LIMIT, and LIMIT value <= the configured cap.
2. **`EXPLAIN` against `mode=ro&immutable=1`.** Catches references to
   non-existent tables/columns and most syntax errors before the query
   runs.
3. **Read-only execution.** Even if the first two were bypassed, the
   executor opens the DB read-only -- a write would fail at SQLite.

`executor.py` opens `mode=ro&immutable=1` and installs a SQLite progress
handler that returns non-zero past the wall-clock deadline, causing
`OperationalError: interrupted` -- surfaced as `SqlResult.timed_out=True`.
Default cap is 10 seconds; in the eval run no query came near it
(p95 1.5s for actual execution).

The `__init__.py` orchestrator does one retry on validation failure: it
re-prompts the generator with the original question plus the validator's
error message and the rejected SQL. In the eval one question hit this
(`What was the total revenue in 2017?` -- the first attempt forgot to
include `LIMIT 1` on a single-row aggregate). The retry succeeded.

### Retriever (`src/retriever/`)

Two ChromaDB collections with `text-embedding-3-small`:

- **methodology_docs** -- 61 chunks from 6 markdown playbooks. Chunker
  is paragraph-aware: pack paragraphs greedily up to 500 chars, with a
  50-character overlap tail at chunk boundaries; never split a single
  paragraph mid-statement (matters for SQL code blocks). Chunks carry
  `filename`, `title`, and `chunk_index` metadata.
- **customer_reviews** -- 40,968 reviews with non-empty
  `review_comment_message`. Each review is its own chunk. Metadata
  includes `review_id`, `order_id`, `review_score`, `product_category`
  (the modal English category for the order's items, computed via a
  window-function CTE), and `review_creation_date`. The review retriever
  exposes ChromaDB `where`-clause filters for score (eq/lte/gte) and
  category.

`Embedder` batches 100 inputs per OpenAI call with bounded retry on
`RateLimitError` and `APIError`. A full reviews build is ~410 batches,
~12 minutes wall time, ~$0.016 (818K input tokens at $0.02/M).

**Cross-language note.** The reviews are in Brazilian Portuguese; the
agent's `review_query` strings are in English (constructed by the router
prompt). `text-embedding-3-small` is multilingual enough that English
queries hit Portuguese reviews on similar topics. Verified in the
retriever smoke test: query `"late delivery never arrived"` returns
`"Entrega super atrasada"` and `"o produto não foi entregue"` at the
top.

### Synthesizer (`src/synthesizer/`)

`answer_generator.py` makes one chat-completion call. The user message
is built from whichever sources fired:
- the question and router decision,
- the SQL query, generator explanation, and a 25-row preview of the
  result table (formatted with `df.to_string(index=False)`),
- up to 5 doc chunks with filename/index/distance,
- up to 5 review excerpts (capped at 400 chars each) with
  short_id / score / category / date.

The system prompt enforces voice (dry, technical, no marketing tone),
citation format (`[sql]`, `[doc:filename]`, `[review:short_id]`), and
honesty rules (say so when SQL returned no rows; do not invent
citations). Output is structured JSON with `answer`, `confidence`, and
`reasoning_summary`.

`chart_selector.py` is pure rules. Decision order:
1. `none` if df is empty.
2. `kpi` if the result is a single numeric cell.
3. `line` if any column parses as a date and any column is numeric.
4. `bar` if a low-cardinality (≤30 distinct) text column is present
   alongside a numeric column, and rows ≤ 15.
5. `scatter` if there are ≥2 numeric columns and no categorical/date.
6. Fallback: `table`.

`__init__.py` orchestrator wires answer-generator + chart-selector,
builds typed `Source` objects (`SqlSource | DocSource | ReviewSource`),
and assembles the user-visible reasoning chain (router decision -> SQL
outcome -> retrieval summary -> chart pick -> synthesizer summary).

### Pipeline (`src/pipeline.py`)

Public entry point. `Pipeline(config).run(question)` returns one
`PipelineResult` carrying:
- the answer, confidence, reasoning chain,
- the chart spec and a JSON-able chart_data payload,
- the typed source list,
- the underlying `RouterDecision`, `SqlAgentRun`, `DocHit`s, `ReviewHit`s
  (for inspection in the UI),
- the SQL DataFrame,
- a list of `StageTiming` entries (per-stage latency + token usage) and
  aggregates.

Both the Streamlit app and the eval harness consume `PipelineResult`
identically -- the UI renders, the eval scores.

### UI (`app/streamlit_app.py`)

`@st.cache_resource` caches the `Pipeline` so SQLite + ChromaDB
connections persist across reruns. The reasoning chain is expanded by
default; sources and metadata are collapsed but a click away. The
sidebar surfaces row counts per table, vector store sizes, model info,
and a session-cumulative chat-completion cost (embedding tokens for
retrieval are not tracked -- their cost is sub-cent across a session).

## Data flow per route

### `sql`

```
Router -> SqlGenerator (structured) -> Validator (static + EXPLAIN)
       -> [retry once on failure]
       -> Executor (read-only, 10s timeout)
       -> Synthesizer (SQL preview only)
       -> ChartSelector (rules)
       -> PipelineResult
```

### `docs`

```
Router -> DocRetriever (1 query embed + Chroma kNN, top-5)
       -> Synthesizer (doc chunks only)
       -> ChartSelector (returns "none")
       -> PipelineResult
```

### `reviews`

Same shape as docs but against the `customer_reviews` collection. The
router can pass a `category` or `score` hint; the current pipeline does
not propagate filters into the retriever (router-issued filters would
be a follow-up).

### `hybrid`

```
Router -> SqlAgent || DocRetriever || ReviewRetriever  (sequentially in code,
                                                        independent in semantics)
       -> Synthesizer (sees all three)
       -> ChartSelector (often "table" or "none")
       -> PipelineResult
```

The three tools currently run sequentially (Python loop) but have no
data dependencies; running them with `asyncio.gather` would shave ~600
ms off hybrid p50. Not done -- the OpenAI client is sync in this
project for readability.

## Caching strategy

OpenAI's automatic prompt-prefix cache is the load-bearing optimisation.
The router and SQL generator both put the heavy static content (full
schema, 12 / 4 few-shots, system rules) at the top of the system prompt
and send only the question as the user message. After the first call,
subsequent prompts hit the cache for ~80-95% of input tokens.

Eval-run measurements:
- Router system prompt: ~2,500 tokens. Cache hit ~96% from call #2
  onward.
- SQL generator system prompt: ~2,200 tokens. Cache hit ~80% (lower
  because ChatGPT input streams vary slightly with question length).
- Synthesizer: cache rarely hits because the user message changes every
  call (different SQL results, different doc/review hits). System
  prompt is small (~600 tokens), so this barely matters.
- Aggregate over the 30-question eval: 122,368 of 178,180 input tokens
  cached (**65% prompt-token cache rate**), driving the eval cost down
  from a ~$0.06 estimate to **$0.0232** actual.

## Safety, in layers

The SQL agent uses the principle that defense-in-depth is cheap when the
layers are independent. A non-`SELECT` query has to defeat all three:

1. **Generator prompt** says read-only, so the LLM rarely tries.
2. **Static validator** rejects forbidden keywords, multiple statements,
   missing LIMIT, LIMIT > cap.
3. **`EXPLAIN`** against the read-only DB rejects bad references.
4. **Execution connection** uses `mode=ro&immutable=1`. SQLite
   physically refuses writes.

LIMIT enforcement is intentionally strict: the validator rejects rather
than silently injecting one. This surfaced a real issue in the eval (a
single-row aggregate without LIMIT) where the retry mechanism corrected
it cleanly. Silent injection would have hidden the generator quality
signal.

## Failure modes and what catches them

| Failure | Caught by | Surfaced as |
|---|---|---|
| Model emits non-`SELECT` SQL | Static validator | Validation error; retry; if still bad, return validation failure to UI |
| Model hallucinates table or column | `EXPLAIN` | sqlite error string in `ValidationResult.error` |
| Query exceeds row limit | Static `LIMIT > cap` check | Validation error |
| Query runs > 10s | Progress-handler watchdog | `SqlResult.timed_out=True`, surfaced in metadata |
| ChromaDB collection missing | Pipeline `__init__` raises `RuntimeError` | App shows "run scripts/3_embed.py first" |
| Question is off-topic | None at routing | Synthesizer asked to be honest; tends to say "no methodology doc covers this directly" |
| LLM transient API error | Embedder retries with backoff; chat calls do not | Chat-side errors propagate; UI shows them |
| Generator writes semantically-wrong SQL (passes validation) | **Not caught.** Exposed in eval (`sql-05`, `hybrid-04`) | Eval failure analysis |

The last row is the one to take seriously. Validators can enforce
syntactic + read-only safety; semantic correctness is a generator-quality
question that the eval harness measures. The README's "What I'd change
with more time" section lists two cheap mitigations (a static SQL linter
for ratio consistency; auto-routing well-known formulas to `hybrid`).

## Performance budget

Per-question budget at p50 / p95, from the eval:
- router: 1.1 / 1.5 s
- sql (when fired): 5.7 / 10.4 s
- doc retrieval (when fired): 0.4 / 0.7 s
- review retrieval (when fired): 0.6 / 1.6 s
- synthesis: 4.0 / 6.2 s
- end-to-end: 7.4 / 17.8 s

The two LLM calls (router + synthesis) are the floor. Adding more tools
adds linearly to wall time as long as they fire serially. Switching the
hybrid path to async parallel calls would compress hybrid p50 from ~14s
to ~10s. Not implemented.

## Tradeoffs and what was on the table

- **No LangChain / LlamaIndex.** The routing classifier, prompt
  construction, and orchestration are the project. Hiding them in a
  framework would defeat the purpose for a portfolio audience.
- **No async.** All OpenAI and ChromaDB calls are sync. The hybrid path
  could go async with `asyncio.gather`; cost is added complexity for ~4s
  of p50 saved. Skipped.
- **No SQL retry on execution error.** Currently retry only fires on
  validation failure. If a query passes validation but fails at exec
  time (e.g. a long timeout from a weirdly-shaped JOIN), there is no
  recovery loop. Eval did not produce any such cases at this question
  difficulty.
- **No structured output for SQL itself.** The generator returns the
  query as a JSON string; we then parse SQL out. We could push the
  output schema deeper (table list, column list, structured WHERE) but
  that would require a JSON-to-SQL renderer and limits expressive power.
- **Sample-values metadata for categorical columns is fixed at 30
  distinct.** Larger categorical sets (city names, product IDs) get no
  values shown. Acceptable: the sample-values block is meant for
  enum-like dimensions (`order_status`, `payment_type`).
