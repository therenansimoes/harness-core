---
name = "ecommerce-sales-methodology"
kinds = ["content"]
description = "ecommerce checkout cart PDP payment gateway fulfillment refunds sales funnel — not warehouse stock/BOM"
---
## Ecommerce / sales methodology

- Funnel order: discover → PDP → cart → checkout → pay → fulfill → support.
- Price and availability must come from one authoritative path; never hardcode sale price in copy alone.
- Cart: idempotent add/update; show tax/shipping assumptions explicitly or mark unknown.
- Checkout: validate required fields; fail closed on payment errors; never claim "paid" without gateway evidence.
- Fulfillment: status transitions are monotonic (paid → packed → shipped → delivered) unless a defined cancel/refund path.
- Refunds/returns: document the policy path; do not invent restocking fees.

## Done when
- Funnel path identified; price/availability from one authoritative source; payment failure is closed; status transitions are monotonic; refund policy documented.
