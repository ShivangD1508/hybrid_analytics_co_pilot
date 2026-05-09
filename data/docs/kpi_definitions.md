# KPI definitions

The metrics on the analytics team's dashboards and the SQL we use to
compute them. When the agent answers a "what is X" question, this is the
source of truth. SQL is written for SQLite syntax; column names match the
live schema in `olist.db`.

## Average order value (AOV)

Mean revenue per order, item-side (excludes voucher-only legs in
`order_payments`).

```sql
SELECT AVG(order_total) AS aov
FROM (
  SELECT order_id, SUM(price + freight_value) AS order_total
  FROM order_items
  GROUP BY order_id
);
```

To split goods vs freight, sum `price` and `freight_value` separately.

## Customer lifetime value (CLV)

Total spend per **unique customer** to date. The dataset window is only
~25 months, so what we compute is observed historical value, not a
predicted lifetime. Treat trends with caution.

```sql
SELECT c.customer_unique_id, SUM(oi.price + oi.freight_value) AS clv
FROM customers c
JOIN orders o ON o.customer_id = c.customer_id
JOIN order_items oi ON oi.order_id = o.order_id
GROUP BY c.customer_unique_id;
```

## Delivery time

Days from purchase to customer delivery, on `delivered` orders only.

```sql
SELECT
  julianday(order_delivered_customer_date) - julianday(order_purchase_timestamp)
    AS delivery_days
FROM orders
WHERE order_status = 'delivered'
  AND order_delivered_customer_date IS NOT NULL;
```

Median is more meaningful than mean here — distribution has a long right
tail (some orders take 60+ days).

## On-time delivery rate

Share of delivered orders where actual delivery was on or before the
estimate.

```sql
SELECT
  AVG(CASE WHEN date(order_delivered_customer_date)
            <= date(order_estimated_delivery_date)
           THEN 1.0 ELSE 0.0 END) AS on_time_rate
FROM orders
WHERE order_status = 'delivered'
  AND order_delivered_customer_date IS NOT NULL;
```

Brazil-wide this sits around 93%. North/Northeast states drag this down.

## Average review score

Mean of `review_score` (1-5). Always pair with the score histogram, not
just the mean — the distribution is heavily right-skewed (~57% are 5s)
and the mean alone hides bimodality.

```sql
SELECT AVG(review_score) AS avg_score, COUNT(*) AS n_reviews
FROM order_reviews;
```

## Repeat purchase rate

Share of unique customers with at least 2 orders. This rate is low in
Olist data (~3%); see `segment_definitions.md` for the implications.

```sql
WITH customer_orders AS (
  SELECT c.customer_unique_id, COUNT(*) AS n_orders
  FROM customers c
  JOIN orders o ON o.customer_id = c.customer_id
  GROUP BY c.customer_unique_id
)
SELECT
  SUM(CASE WHEN n_orders >= 2 THEN 1 ELSE 0 END) * 1.0 / COUNT(*)
    AS repeat_rate
FROM customer_orders;
```

## Seller performance score

Composite metric we use for seller leaderboards. Three components, equally
weighted, each rescaled to 0-1:

1. **Avg review score** on items sold by the seller, normalized
   `(score - 1) / 4`.
2. **On-time delivery rate** on the seller's items
   (delivered_customer_date <= estimated_delivery_date), already 0-1.
3. **Order completion rate**: 1 - cancellation rate.

```sql
WITH seller_orders AS (
  SELECT oi.seller_id, o.order_id, o.order_status,
         o.order_estimated_delivery_date, o.order_delivered_customer_date
  FROM order_items oi
  JOIN orders o ON o.order_id = oi.order_id
  GROUP BY oi.seller_id, o.order_id
)
SELECT seller_id,
  (
    (AVG(r.review_score) - 1) / 4.0
    + AVG(CASE WHEN order_status = 'delivered'
                AND date(order_delivered_customer_date)
                  <= date(order_estimated_delivery_date)
               THEN 1.0 ELSE 0.0 END)
    + AVG(CASE WHEN order_status != 'canceled' THEN 1.0 ELSE 0.0 END)
  ) / 3.0 AS seller_score
FROM seller_orders so
LEFT JOIN order_reviews r ON r.order_id = so.order_id
GROUP BY seller_id
HAVING COUNT(DISTINCT so.order_id) >= 10;
```

The `>= 10` threshold filters sellers with too few orders for the score
to be meaningful.
