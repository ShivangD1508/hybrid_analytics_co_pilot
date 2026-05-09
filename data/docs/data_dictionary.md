# Data dictionary

Authoritative reference for the 9 tables in `olist.db`. Use this when a
question asks what a column means or how to join two tables. Schema-level
facts (types, indexes) come from `PRAGMA` introspection; this document adds
the business meaning and quality caveats that the schema cannot express.

The data covers **2016-09-04 to 2018-10-17** (~25 months). All timestamps
are stored as TEXT in ISO 8601 form (`YYYY-MM-DD HH:MM:SS`). Use
`datetime()`, `date()`, `strftime()`, and `julianday()` for filtering and
arithmetic.

## customers

One row per **order-side customer reference**, not per physical person.
- `customer_id`: the per-order pseudonym used to link `orders.customer_id`.
- `customer_unique_id`: the stable identifier for a real customer across
  orders. Use this for repeat-purchase, RFM, and CLV calculations.
- `customer_zip_code_prefix`: 5-digit Brazilian zip prefix (1,003 to 99,990).
- `customer_state`: 27 Brazilian state codes (AC, AL, ..., SP, ...).

Repeat-purchase rate is low (~3% of unique customers): expect a strong
left tail when grouping by `customer_unique_id`.

## orders

One row per order. PK `order_id`.
- `order_status`: one of `approved`, `canceled`, `created`, `delivered`,
  `invoiced`, `processing`, `shipped`, `unavailable`. Filter to `delivered`
  for delivery-performance analyses.
- `order_purchase_timestamp`: when the customer placed the order. Always set.
- `order_approved_at`: payment approval. Nullable for canceled orders.
- `order_delivered_carrier_date`: handoff to carrier. Nullable.
- `order_delivered_customer_date`: actual customer delivery. Nullable.
- `order_estimated_delivery_date`: promised date set at order time.

## order_items

One row per **line item** in an order. Composite PK `(order_id, order_item_id)`.
The same `order_id` can have up to ~21 items.
- `price` and `freight_value` are per item. Order total =
  `SUM(price + freight_value)` grouped by `order_id`.
- `seller_id` and `product_id` are item-level: a single order can span
  multiple sellers and categories.

## order_payments

One row per payment leg of an order; an order can have multiple if split
across methods or vouchers. PK `(order_id, payment_sequential)`.
- `payment_type`: `credit_card`, `boleto` (Brazilian bank slip),
  `debit_card`, `voucher`, `not_defined`.
- `payment_value`: amount on this leg. Sum across legs to get order total
  from the payments side; this can differ slightly from
  `SUM(price + freight_value)` due to vouchers and rounding.

## order_reviews

One row per review. `review_id` is **not unique** in the source data
(quirk of the dataset; ~800 dupes); `(review_id, order_id)` is effectively
the natural key.
- `review_score`: 1 to 5. Skewed right (most are 5).
- `review_comment_message`: free-text Portuguese; **NULL or empty in ~59%
  of rows**. Filter `WHERE review_comment_message IS NOT NULL AND
  TRIM(review_comment_message) != ''` before any text analysis.

## products

PK `product_id`. Product attributes are dimensions, not events.
- `product_category_name`: Portuguese category. Translate via
  `product_category_translation`. **Nullable** (~610 rows have NULL category).
- Original Olist column-name typos kept: `product_name_lenght`,
  `product_description_lenght`.

## sellers

PK `seller_id`. 3,095 sellers across 23 states. Heavily concentrated in SP.

## geolocation

Multiple rows per `geolocation_zip_code_prefix` (different lat/lng
samples). No PK. For "city of zip X" lookups, aggregate
(`AVG(lat), AVG(lng)`) or pick any single row.

## product_category_translation

71 rows. Maps Portuguese `product_category_name` to English. Some products
have categories that do not appear here; LEFT JOIN, do not INNER JOIN, if
you need to keep all products.

## Common join paths

- Order grain (most analyses):
  `orders` → `order_items` → `products` → `product_category_translation`
- Customer grain: `orders` → `customers` (then group by
  `customer_unique_id`).
- Payment grain: `orders` → `order_payments`.
- Review grain: `orders` → `order_reviews`.
- Seller grain: `order_items` → `sellers`.
