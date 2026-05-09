# RFM methodology

How we score customers on Recency, Frequency, and Monetary value, the
thresholds we use, and the caveats specific to the Olist dataset. RFM
feeds the segmentation rules in `segment_definitions.md`.

## Inputs

RFM is computed at the **`customer_unique_id`** grain. The per-order
`customer_id` in `customers` is a pseudonym; `customer_unique_id` is the
stable identifier. Doing RFM on `customer_id` will misclassify every
repeat customer as several "new" ones.

The reference date is the latest `order_purchase_timestamp` in the
dataset, currently **2018-10-17**. In production we would use today's
date; for this static snapshot we anchor on the data's max date so
recency is well-defined.

## Definitions

- **Recency (R)**: days from the customer's most recent
  `order_purchase_timestamp` to the reference date. Lower is better.
- **Frequency (F)**: count of distinct `order_id` per
  `customer_unique_id`.
- **Monetary (M)**: sum of `price + freight_value` from `order_items`
  across all of the customer's orders. We use the item side rather than
  `order_payments` to keep the metric consistent with AOV and to avoid
  voucher legs distorting the total.

```sql
WITH last_purchase AS (
  SELECT MAX(order_purchase_timestamp) AS ref FROM orders
),
customer_facts AS (
  SELECT
    c.customer_unique_id,
    CAST(julianday((SELECT ref FROM last_purchase))
         - julianday(MAX(o.order_purchase_timestamp)) AS INT) AS recency_days,
    COUNT(DISTINCT o.order_id)                              AS frequency,
    SUM(oi.price + oi.freight_value)                        AS monetary
  FROM customers c
  JOIN orders o      ON o.customer_id = c.customer_id
  JOIN order_items oi ON oi.order_id  = o.order_id
  GROUP BY c.customer_unique_id
)
SELECT * FROM customer_facts;
```

## Scoring

Each dimension is bucketed 1-5 by quintile across all customers, with 5
being the most desirable.

- R5 = most recent quintile (smallest `recency_days`)
- F5 = top quintile by `frequency`
- M5 = top quintile by `monetary`

```sql
SELECT
  customer_unique_id,
  NTILE(5) OVER (ORDER BY recency_days ASC)  AS r_score,  -- low days → high score
  NTILE(5) OVER (ORDER BY frequency DESC)    AS f_score,
  NTILE(5) OVER (ORDER BY monetary DESC)     AS m_score
FROM customer_facts;
```

## Olist-specific caveat: F is degenerate

About **97% of `customer_unique_id` values have exactly one order** in
this dataset. `NTILE(5) ORDER BY frequency` therefore puts ~97% of
customers into a single bucket and the F score loses most of its
discriminating power.

Two pragmatic adjustments we use:

1. **Binary F**: replace the 1-5 score with `f_score = 1 if frequency = 1
   else 5`. This is what most reports default to.
2. **Drop F from the composite when answering business questions** about
   one-time vs repeat behavior — segment on F separately rather than
   blending into a single RFM score.

`segment_definitions.md` uses approach (2): segments are defined on
**Recency × Frequency only**, with M used as a tiebreaker rather than as
an axis.

## Recency bucket thresholds

The quintile boundaries above are data-driven. For reporting we also
report fixed-cutoff buckets so segments are stable across reruns:

| Bucket | Condition | Approx share |
|---|---|---|
| Active | recency_days <= 90 | ~10% |
| Slipping | 91-180 | ~19% |
| At risk | 181-365 | ~41% |
| Lost | > 365 | ~30% |

These cutoffs assume the ~25-month dataset window; widen them if the
window grows.

## Common mistakes

- Computing F by `customer_id` instead of `customer_unique_id`: yields
  F=1 for every customer (since `customer_id` is per-order). The actual
  unique-customer overcount is small (~3%), but Frequency collapses
  entirely.
- Including canceled orders in F or M (they shouldn't drive segmentation).
  Filter `WHERE order_status != 'canceled'`.
- Using `order_payments.payment_value` for M without deduplicating
  multi-leg payments — sum carefully or use the item side.
