# CLAUDE.md — hybrid-analytics-agent

Project instructions for Claude Code sessions in this repo. Keep this file
authoritative; if behavior here conflicts with the conversation, ask before
deviating.

## What this project is

An AI agent that answers business questions over the Olist Brazilian
e-commerce dataset by routing between three data sources:

- **SQL** — structured queries against a SQLite database built from 9 Olist CSVs
- **Docs** — semantic search over methodology/playbook markdown we author
- **Reviews** — semantic search over ~40K real customer review comments

The agent classifies the question, runs the relevant tool(s), and synthesizes
a single answer with charts and a visible reasoning chain. Portfolio-grade,
audience is the technical DS/ML community (not recruiters).

## Hard rules

1. **No frameworks that hide the interesting parts.** No LangChain, no
   LlamaIndex, no Haystack. Use the raw `openai` SDK and the `chromadb`
   client directly. Routing logic, prompt assembly, and orchestration are
   written by hand — that's the point.
2. **Build step-by-step.** The execution plan has 11 numbered steps. After
   each step, stop and show the user. Wait for "approved" or "next" before
   continuing. Do not combine steps.
3. **Honest evaluation.** The eval harness must report failures as
   prominently as successes. The README's failure-analysis section is
   non-negotiable.
4. **Voice for written output (READMEs, docs, comments).** Dry, technical,
   honest. No exclamation points, no emojis, no "Welcome!", no marketing
   tone. Write like a staff engineer documenting an internal tool.
5. **SQL safety.** SELECT only. The validator must reject INSERT, UPDATE,
   DELETE, DROP, ALTER, CREATE. Every executed query has `LIMIT 1000` and a
   10-second timeout.

## Tech stack

| Layer | Choice |
|---|---|
| Python | 3.14.2 (Windows) |
| Database | SQLite (single `data/olist.db` file) |
| Embeddings | OpenAI `text-embedding-3-small` |
| Vector store | ChromaDB (persistent, local at `data/chroma/`) |
| LLM | OpenAI `gpt-4o-mini` for routing, SQL gen, synthesis |
| Charts | Plotly (rendered inside Streamlit) |
| UI | Streamlit |
| Secrets | `python-dotenv`, never commit `.env` |

**Python 3.14 caveat:** `chromadb` and its native deps (e.g. `onnxruntime`,
`hnswlib`) historically lag new CPython releases. If install fails on 3.14,
surface the error to the user before swapping packages — do not silently
substitute.

## Data location

Olist CSVs live at `c:/dev/hybrid-analytics-agent/dataset/` (note: the
original brief referenced `c:/dev/Analytics Co-pilot/dataset/`, which does
not exist on this machine — the dataset folder is local to the repo).

The 9 CSVs:
- `olist_customers_dataset.csv`
- `olist_orders_dataset.csv`
- `olist_order_items_dataset.csv`
- `olist_order_payments_dataset.csv`
- `olist_order_reviews_dataset.csv`
- `olist_products_dataset.csv`
- `olist_sellers_dataset.csv`
- `olist_geolocation_dataset.csv`
- `product_category_name_translation.csv`

`data/raw/` will hold copies (or symlinks) at load time. The compiled
SQLite DB lands at `data/olist.db`. Both are gitignored.

## Code standards

- Type hints on every function signature.
- One-line docstring per public function; longer only if the WHY is
  non-obvious. No multi-paragraph docstrings.
- No comments that restate what the code does.
- One module = one responsibility. The directory layout (`router/`,
  `sql_agent/`, `retriever/`, `synthesizer/`) is load-bearing.
- The pipeline must log a structured `reasoning_chain` — each stage
  appends a step. This is what the UI surfaces to the user.

## Environment

- Shell: PowerShell 5.1 (Windows). Use PowerShell syntax in any scripts
  intended to run from a terminal. Bash is also available via the harness's
  Bash tool.
- Not a git repo yet — `git init` will happen later, on the user's call.
- Author: Shivaang Dayavarshetty <shivaang.dayavarshetty@gmail.com>,
  GitHub `ShivangD1508`.

## Execution plan

1. Scaffold + this file + requirements + `.env` setup ← current
2. Data loader (CSVs → SQLite with indexes + schema introspection)
3. Methodology docs (6 markdown playbooks under `data/docs/`)
4. Router (LLM classifier with few-shot examples)
5. SQL agent (generator + validator + executor)
6. Retriever (embed docs + reviews into ChromaDB)
7. Synthesizer + chart selector
8. Pipeline (wire everything, log reasoning chain)
9. Streamlit UI
10. Evaluation harness + run eval + generate report
11. README + architecture.md (with real numbers from the eval)

README and architecture docs are written **last**, after eval, so the
numbers in them are real.
