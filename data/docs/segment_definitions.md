# Customer segments

Operational segments used by the marketing and CRM teams. Definitions are
deterministic so the same customer lands in the same segment across
reports. Built on the RFM facts from `rfm_methodology.md` (Recency in
days, Frequency as distinct order count per `customer_unique_id`).

## Why we don't use the textbook 11-segment grid

Textbook RFM grids (Champions / Loyal Customers / Potential Loyalists /
At Risk / Hibernating / etc.) assume meaningful spread in Frequency.
**Olist's repeat-purchase rate is ~3%** — the F dimension is binary in
practice. Segments below collapse the grid down to seven cells that
actually have customers in them.

## Segment criteria

Reference date is `MAX(order_purchase_timestamp)` (2018-10-17 in the
current snapshot). All criteria filter `WHERE order_status != 'canceled'`.

| Segment | Frequency | Recency (days) | Population share |
|---|---|---|---|
| **Champions** | F >= 2 | R <= 90 | ~0.3% |
| **Loyal** | F >= 2 | 91 <= R <= 180 | ~0.7% |
| **At-Risk Repeat** | F >= 2 | 181 <= R <= 365 | ~1.3% |
| **Lost Repeat** | F >= 2 | R > 365 | ~0.8% |
| **New** | F = 1 | R <= 90 | ~9.7% |
| **One-Time Active** | F = 1 | 91 <= R <= 365 | ~58.6% |
| **One-Time Dormant** | F = 1 | R > 365 | ~28.7% |

Monetary value (M) is not part of the segment definition; it is reported
as a within-segment percentile so the team can prioritize high-spend
customers inside each segment without re-segmenting.

## SQL

```sql
WITH ref AS (SELECT MAX(order_purchase_timestamp) AS d FROM orders),
facts AS (
  SELECT
    c.customer_unique_id,
    COUNT(DISTINCT o.order_id) AS f,
    CAST(julianday((SELECT d FROM ref))
         - julianday(MAX(o.order_purchase_timestamp)) AS INT) AS r
  FROM customers c
  JOIN orders o ON o.customer_id = c.customer_id
  WHERE o.order_status != 'canceled'
  GROUP BY c.customer_unique_id
)
SELECT
  customer_unique_id,
  CASE
    WHEN f >= 2 AND r <= 90  THEN 'Champions'
    WHEN f >= 2 AND r <= 180 THEN 'Loyal'
    WHEN f >= 2 AND r <= 365 THEN 'At-Risk Repeat'
    WHEN f >= 2              THEN 'Lost Repeat'
    WHEN f = 1  AND r <= 90  THEN 'New'
    WHEN f = 1  AND r <= 365 THEN 'One-Time Active'
    ELSE                          'One-Time Dormant'
  END AS segment
FROM facts;
```

## Recommended actions per segment

**Champions.** Highest CRM priority despite the small population. Early
access to drops, loyalty perks, request reviews on new orders. Acquisition
cost is sunk; protect retention.

**Loyal.** Re-engagement window is short. Trigger a category-specific
recommendation email by week 6-8 from last purchase to compress recency
back under 90 days.

**At-Risk Repeat.** Reactivation campaigns with discount, ideally tied to
the category of their last order. Past 365 days the win-back rate
collapses.

**Lost Repeat.** Quarterly broad-reach reactivation only. Don't allocate
1:1 attention.

**New.** Most of the marketable population on any given month. Focus on
post-purchase experience: review-request flow, on-time delivery
follow-up, and a category-tailored second-purchase nudge after 30 days.

**One-Time Active.** Dominant cohort. The single highest-leverage
intervention is converting a portion of this segment to F=2 within 6
months — every percentage point shifted moves the company-wide repeat
rate visibly.

**One-Time Dormant.** Deprioritize for paid channels. Use organic and
cheap email touches only; the win-back economics do not work for paid.
