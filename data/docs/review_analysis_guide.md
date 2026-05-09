# Review analysis guide

How to read review scores and how to use the free-text comments
responsibly. Reviews are stored in `order_reviews`; one row per review,
joined to orders on `order_id`.

## Score distribution to expect

The `review_score` distribution is heavily right-skewed:

| Score | Approx share |
|---|---|
| 5 | ~57% |
| 4 | ~19% |
| 3 | ~8% |
| 2 | ~3% |
| 1 | ~12% |

The bimodal shape (a 5-star peak with a smaller 1-star peak, and a sparse
middle) is normal for Brazilian e-commerce surveys — customers tend to
respond when they are very satisfied or very angry. Reporting only the
mean smooths over this and hides the 1-star tail. Always pair the mean
with at minimum the **share of 1-2 star reviews** (a "detractor rate").

## Red flags

Treat any of these as escalations rather than monitoring metrics:

- A category, seller, or city with average score **below 4.0**. The
  global mean sits around 4.1; below 4.0 is a meaningful negative signal.
- A **detractor rate above 18%** (1-2 star share). Above 25% is severe.
- A **week-over-week drop in average score of more than 0.2** for any
  category with at least 200 reviews that week.
- Sellers whose detractor rate is in the top decile **and** whose review
  volume has grown — they are scaling a quality problem.

## Working with comment text

The text fields are in **Brazilian Portuguese**. The retrieval pipeline
embeds them with a multilingual-capable model (`text-embedding-3-small`
handles Portuguese well); plan English-language searches accordingly —
embedding-based retrieval works across languages but exact keyword
matches will not.

Comment availability:

- ~41% of reviews have a non-empty `review_comment_message`.
- ~12% have a non-empty `review_comment_title`.
- The rest are score-only.

Always filter empties before any text analysis:

```sql
SELECT review_id, order_id, review_score, review_comment_message
FROM order_reviews
WHERE review_comment_message IS NOT NULL
  AND TRIM(review_comment_message) != '';
```

## Root-cause analysis pattern

When the question is "why is X getting low reviews", run this two-step
pattern:

1. **Quantify** with SQL — confirm the issue is real and locate it.

   ```sql
   SELECT t.product_category_name_english,
          AVG(r.review_score) AS avg_score,
          COUNT(*)            AS n_reviews
   FROM order_reviews r
   JOIN order_items oi ON oi.order_id = r.order_id
   JOIN products p     ON p.product_id = oi.product_id
   JOIN product_category_translation t
     ON t.product_category_name = p.product_category_name
   GROUP BY 1
   HAVING n_reviews >= 100
   ORDER BY avg_score ASC LIMIT 10;
   ```

2. **Sample** with retrieval — pull 15-30 actual comments from the worst
   subgroup (`review_score <= 3` and a category filter) and read them.
   Patterns surface fast; don't try to do this with an LLM summarizer
   before reading the raw text yourself.

## What review text usually says

Across the dataset, the recurring complaint themes are:

- **Late delivery** ("ainda não recebi", "atrasou"). Most common; usually
  correlates with `delivered_customer_date > estimated_delivery_date`.
- **Wrong or missing item.** Often a seller-fulfillment issue.
- **Product quality / damage.** Concentrated in `furniture_decor` and
  fragile categories.
- **Defective at arrival.** Concentrated in `electronics` and `computers`.
- **Good product despite issues.** A non-trivial share of 1-2 star
  reviews are about delivery or packaging, not the product. Sellers
  legitimately bear blame for some but not all of these.

The takeaway: if a low-score cluster correlates with the delivery
columns, route the question through `delivery_performance.md` rather
than seller quality — the lever is logistics.
