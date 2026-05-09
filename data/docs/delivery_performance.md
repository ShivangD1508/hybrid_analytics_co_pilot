# Delivery performance

Definitions for delivery KPIs, the SLA we report against, and the
geographic patterns that drive most of the variance. All figures sourced
from `orders` (filtered to `order_status = 'delivered'`); enrich with
`order_items` for seller/category cuts and with `customers` for
geography.

## Date columns and what they mean

| Column | Meaning |
|---|---|
| `order_purchase_timestamp` | Customer placed the order. T0 for everything. |
| `order_approved_at` | Payment cleared. NULL for canceled. |
| `order_delivered_carrier_date` | Seller handed off to carrier. NULL until shipped. |
| `order_delivered_customer_date` | Actual delivery. NULL until delivered. |
| `order_estimated_delivery_date` | Promise made to the customer at order time. |

Always store as TEXT in ISO 8601, compute deltas with
`julianday(b) - julianday(a)`.

## Stage-by-stage metrics

Splitting end-to-end delivery into stages localizes problems to either
the seller or the carrier.

```sql
SELECT
  julianday(order_approved_at) - julianday(order_purchase_timestamp)
    AS payment_lag_days,
  julianday(order_delivered_carrier_date) - julianday(order_approved_at)
    AS seller_handling_days,
  julianday(order_delivered_customer_date) - julianday(order_delivered_carrier_date)
    AS carrier_transit_days,
  julianday(order_delivered_customer_date) - julianday(order_purchase_timestamp)
    AS total_days
FROM orders
WHERE order_status = 'delivered'
  AND order_delivered_customer_date IS NOT NULL
  AND order_delivered_carrier_date IS NOT NULL
  AND order_approved_at IS NOT NULL;
```

Typical national medians:

- Payment lag: ~0.5 day (boleto pulls this up; credit_card is near zero).
- Seller handling: ~2-3 days.
- Carrier transit: ~7-9 days.
- Total: ~10-12 days, with a long right tail.

## On-time SLA

The published SLA is **delivered on or before the estimated date**:

```sql
SELECT
  AVG(CASE WHEN date(order_delivered_customer_date)
            <= date(order_estimated_delivery_date)
           THEN 1.0 ELSE 0.0 END) AS on_time_rate
FROM orders
WHERE order_status = 'delivered'
  AND order_delivered_customer_date IS NOT NULL;
```

National rate hovers around **93%**. The estimate is set generously by
Olist's pricing/logistics layer, so beating it should be the norm; missing
it is the alert condition.

A useful companion metric is **delivery delta in days** (negative = early):

```sql
SELECT
  julianday(order_delivered_customer_date)
    - julianday(order_estimated_delivery_date) AS delta_days
FROM orders
WHERE order_status = 'delivered'
  AND order_delivered_customer_date IS NOT NULL;
```

Median delta is around **-12 days** (orders arrive ~12 days before the
promise date). When the median delta moves toward 0, the SLA is at risk
even before the on-time rate moves.

## Geographic variance

Sellers are concentrated in the Southeast (SP, MG, RJ, PR account for
the bulk). Customers are spread across all 27 states, so distance from
SP is a strong predictor of delivery time.

Order states roughly by total delivery time, fastest to slowest:

1. SP, RJ, MG, ES (Southeast)
2. PR, SC, RS (South)
3. GO, MT, MS, DF (Center-West)
4. BA, PE, CE, RN, PB, AL, SE, PI, MA (Northeast)
5. AM, PA, RO, AC, AP, RR, TO (North)

The bottom two groups can show medians 2-3x the SP-internal median. When
analyzing on-time rate, **always cut by `customers.customer_state`** —
national averages are dominated by SP volume and hide the tails.

## Cancellations and exclusions

- Filter `order_status = 'delivered'` for any time-based metric. Other
  statuses (`canceled`, `unavailable`, `processing`) have NULL delivery
  dates and will produce zeros or NaNs.
- Cancellation rate is its own KPI — track it on the full population
  (`order_status = 'canceled' / total`) and join to `order_items` if you
  need to attribute cancellations to a seller or category.
- Late and early deliveries both fall under `delivered`; there is no
  separate "late" status. The on-time SLA above is the only flag.
