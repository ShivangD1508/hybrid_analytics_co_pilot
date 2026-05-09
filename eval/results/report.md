# Evaluation report

Run at: 2026-05-09 15:39 UTC
Pipeline: gpt-4o-mini (chat) + text-embedding-3-small (embeddings) + ChromaDB persistent local
Test set: `eval/test_questions.json` -- 30 questions

## Headline metrics

| Metric | Value |
|---|---|
| Routing accuracy | 28/30 (93%) |
| SQL execution rate | 20/20 (100%) |
| SQL returned >=1 row | 20/20 (100%) |
| Doc retrieval hit rate (top-5) | 19/20 (95%) |
| Chart appropriateness | 24/26 (92%) |
| Average keyword coverage | 0.72 |
| Total tokens | 187,632 (prompt 178,180 of which cached 122,368; completion 9,452) |
| Estimated chat-completion cost | $0.0232 |
| Total wall time | 279.4s |

### Routing accuracy by expected route

| Route | Correct | Total | % |
|---|---|---|---|
| docs | 9 | 10 | 90% |
| hybrid | 9 | 10 | 90% |
| sql | 10 | 10 | 100% |

### Routing confusion matrix

(Rows = expected, columns = predicted.)

| expected \ predicted | sql | docs | reviews | hybrid |
|---|---|---|---|---|
| **sql** | 10 | 0 | 0 | 0 |
| **docs** | 0 | 9 | 0 | 1 |
| **reviews** | 0 | 0 | 0 | 0 |
| **hybrid** | 0 | 1 | 0 | 9 |

## Latency per stage (ms)

| Stage | n | p50 | p95 | max |
|---|---|---|---|---|
| router | 30 | 1101 | 1463 | 1652 |
| sql | 20 | 5672 | 10361 | 13083 |
| docs | 19 | 431 | 670 | 1259 |
| reviews | 5 | 623 | 1592 | 1592 |
| synthesis | 30 | 3999 | 6212 | 6447 |
| TOTAL | 30 | 7443 | 17811 | 21439 |

## Per-question results

| ID | Diff | Route OK | SQL exec | Doc hit | Chart OK | Keyword cov. | Conf | Total ms |
|---|---|---|---|---|---|---|---|---|
| `sql-01` | easy | yes (sql) | yes | - | yes (kpi) | 0.50 | 0.95 | 7443 |
| `sql-02` | easy | yes (sql) | yes | - | yes (kpi) | 0.33 | 0.85 | 11252 |
| `sql-03` | easy | yes (sql) | yes | - | yes (bar) | 1.00 | 0.85 | 7210 |
| `sql-04` | easy | yes (sql) | yes | - | yes (kpi) | 0.50 | 0.95 | 4473 |
| `sql-05` | medium | yes (sql) | yes | - | yes (kpi) | 0.00 | 0.85 | 6095 |
| `sql-06` | medium | yes (sql) | yes | - | yes (bar) | 0.00 | 0.85 | 12422 |
| `sql-07` | medium | yes (sql) | yes | - | yes (table) | 1.00 | 0.85 | 11124 |
| `sql-08` | easy | yes (sql) | yes | - | yes (kpi) | 0.50 | 0.95 | 4219 |
| `sql-09` | hard | yes (sql) | yes | - | yes (kpi) | 1.00 | 0.85 | 9020 |
| `sql-10` | medium | yes (sql) | yes | - | yes (line) | 1.00 | 0.95 | 10360 |
| `docs-01` | easy | yes (docs) | - | yes | yes (none) | 0.50 | 0.85 | 5349 |
| `docs-02` | medium | yes (docs) | - | yes | yes (none) | 1.00 | 0.85 | 5653 |
| `docs-03` | medium | yes (docs) | - | yes | yes (none) | 1.00 | 0.85 | 5955 |
| `docs-04` | easy | yes (docs) | - | yes | yes (none) | 1.00 | 0.85 | 5316 |
| `docs-05` | easy | yes (docs) | - | yes | yes (none) | 1.00 | 0.85 | 4738 |
| `docs-06` | easy | yes (docs) | - | yes | yes (none) | 0.50 | 0.85 | 5504 |
| `docs-07` | medium | **NO** (hybrid) | yes | yes | **NO** (table) | 1.00 | 0.85 | 14243 |
| `docs-08` | medium | yes (docs) | - | yes | yes (none) | 0.00 | 0.85 | 4331 |
| `docs-09` | hard | yes (docs) | - | yes | yes (none) | 1.00 | 0.85 | 5952 |
| `docs-10` | medium | yes (docs) | - | yes | yes (none) | 1.00 | 0.85 | 6058 |
| `hybrid-01` | medium | yes (hybrid) | yes | yes | yes (table) | 1.00 | 0.85 | 14260 |
| `hybrid-02` | hard | yes (hybrid) | yes | yes | - (line) | 0.67 | 0.85 | 12875 |
| `hybrid-03` | hard | yes (hybrid) | yes | yes | - (table) | 1.00 | 0.50 | 17811 |
| `hybrid-04` | hard | **NO** (docs) | - | yes | **NO** (none) | 0.50 | 0.85 | 5677 |
| `hybrid-05` | hard | yes (hybrid) | yes | yes | - (table) | 1.00 | 0.85 | 15406 |
| `hybrid-06` | medium | yes (hybrid) | yes | yes | yes (bar) | 0.50 | 0.85 | 13154 |
| `hybrid-07` | medium | yes (hybrid) | yes | yes | yes (table) | 0.67 | 0.85 | 21439 |
| `hybrid-08` | easy | yes (hybrid) | yes | yes | - (kpi) | 1.00 | 0.85 | 8936 |
| `hybrid-09` | medium | yes (hybrid) | yes | **NO** | yes (bar) | 0.50 | 0.85 | 12902 |
| `hybrid-10` | easy | yes (hybrid) | yes | yes | yes (kpi) | 1.00 | 0.85 | 10273 |

## Failure analysis

Out of 30 questions, **7 had at least one failed check**. Each is listed below with the failed check(s) and the agent's actual answer for comparison.

### `sql-02` (easy) -- What was the total revenue across all orders placed in 2017?

- keyword coverage 0.33 -- missed: ['7142672', '7.14']

**Answer (confidence 0.85):**

> The total revenue across all orders placed in 2017 was $7,142,672.43.[sql]

### `sql-05` (medium) -- What share of delivered orders arrive on or before the estimated delivery date?

- keyword coverage 0.00 -- missed: ['92', '93']

**Answer (confidence 0.85):**

> Approximately 91.88% of delivered orders arrive on or before the estimated delivery date, based on the analysis of the orders data.[sql]

### `sql-06` (medium) -- Top 10 product categories by total revenue in 2018, in English category names.

- keyword coverage 0.00 -- missed: ['health_beauty', 'watches_gifts', 'bed_bath_table']

**Answer (confidence 0.85):**

> The top 10 product categories by total revenue in 2018 are as follows: 1) Health & Beauty - $866,810.34, 2) Watches & Gifts - $749,738.44, 3) Bed, Bath & Table - $643,653.48, 4) Sports & Leisure - $604,393.87, 5) Computers & Accessories - $583,989.37, 6) Housewares - $481,661.00, 7) Furniture & Decor - $470,024.38, 8) Auto - $399,005.20, 9) Baby - $291,738.08, and 10) Cool Stuff - $260,159.81.[sql]

### `docs-07` (medium) -- What does it mean if a category has an average review score below 4.0?

- router predicted 'hybrid', expected 'docs'
- chart predicted 'table', expected 'none'

**Answer (confidence 0.85):**

> An average review score below 4.0 indicates potential customer dissatisfaction, as it is considered a meaningful negative signal in the context of review analysis. The global mean review score is around 4.1, and scores below this threshold suggest that a category may be experiencing quality issues or customer complaints. Specifically, categories with lower scores may have a higher proportion of detractor reviews (1-2 star ratings), which can further highlight dissatisfaction among customers. For instance, categories like "security_and_services" and "diapers_and_hygiene" have average scores of 2.5 and 3.38, respectively, indicating significant concerns from customers in these areas, as reflected in the review excerpts that mention poor quality and unmet expectations [doc:review_analysis_guide.md]. Overall, a score below 4.0 warrants further investigation into customer feedback and product quality to address underlying issues [doc:review_analysis_guide.md].

### `docs-08` (medium) -- How is the repeat purchase rate defined in our metrics?

- keyword coverage 0.00 -- missed: ['customer_unique_id', 'share']

**Answer (confidence 0.85):**

> The repeat purchase rate is defined as the proportion of unique customers who have made two or more purchases. It is calculated by dividing the number of customers with at least two orders by the total number of unique customers. This metric is typically low, around 3% of unique customers, indicating a strong left tail when analyzed by customer segments. The repeat purchase rate is crucial for understanding customer retention and the effectiveness of marketing strategies aimed at converting one-time buyers into repeat customers.[doc:kpi_definitions.md][doc:segment_definitions.md]

### `hybrid-04` (hard) -- Compute the repeat purchase rate. Reference our defined formula in the answer.

- router predicted 'docs', expected 'hybrid'
- chart predicted 'none', expected 'kpi'

**Answer (confidence 0.85):**

> The repeat purchase rate is calculated using the formula: \( \text{Repeat Purchase Rate} = \frac{\text{Number of Customers with 2 or More Orders}}{\text{Total Number of Unique Customers}} \). This is implemented in SQL by counting the number of customers who have made two or more purchases and dividing that by the total number of unique customers in the dataset, as shown in the provided SQL snippet from the methodology documentation `[doc:kpi_definitions.md]`. The repeat purchase rate is noted to be low, approximately 3% of unique customers, indicating a strong left tail in the distribution of repeat purchases `[doc:data_dictionary.md]`.

### `hybrid-09` (medium) -- Show me the breakdown of payment types and explain why boleto matters for delivery.

- expected doc not in top-0: expected one of [delivery_performance.md], got [(none)]

**Answer (confidence 0.85):**

> The breakdown of payment types for delivered orders is as follows: credit card accounts for 74,304 orders with a total revenue of $12,101,094.88, followed by boleto with 19,191 orders generating $2,769,932.58. Other payment types include vouchers with 3,679 orders totaling $343,013.19, and debit cards with 1,485 orders amounting to $208,421.12. Boleto is significant for delivery as it represents a substantial portion of the revenue and order volume, indicating its popularity among customers. However, customer reviews suggest that there are issues with delivery times associated with boleto payments, as multiple reviews mention delays in delivery, which could impact customer satisfaction and repeat business. This highlights the need for efficient processing of boleto transactions to improve delivery performance and customer experience overall.[sql][review:6a8068cb][review:38b6f495][review:563374a9][review:5510c1ad]

## Notes and known limitations

- The eval test set covers `sql`, `docs`, and `hybrid` routes only (10 each). The router supports a `reviews` route but no reviews-only questions appear in this set; the pipeline smoke test in Step 8 exercises that route.
- Keyword-coverage is a substring check, not an LLM judge. It rewards answers that quote the exact expected token. Paraphrased answers can score lower than they deserve; that is a known limitation of this metric.
- SQL retry tokens are partly under-counted: when the SQL agent retries after a validation failure (typically a missing `LIMIT` on a single-row aggregate), only the second attempt's tokens land in the report. Latency is captured in full.
- Embedding-token cost for retrieval queries is not tracked. Each retrieval query embeds ~10-50 tokens, so the per-eval miss is a few thousand tokens at $0.02/M -- well under one cent.

