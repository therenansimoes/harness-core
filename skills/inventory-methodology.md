---
name = "inventory-methodology"
kinds = ["content"]
description = "warehouse inventory SKU on-hand reserved BOM reorder_point stock audit part — not checkout/payment/cart"
---
## Inventory methodology

- Identify the unit of stock (SKU / part / lot) and the source of truth store before editing.
- Every quantity change needs: who/what/why and a reversible audit entry when the system supports it.
- Reorder: track on-hand, reserved, incoming; never treat reserved as free.
- BOM / kits: explode components before promising availability; partial kits are not sellable unless policy says so.
- Prefer idempotent updates (natural key) over blind increments.
- When suggesting alternatives, match form-fit-function constraints — do not substitute silently.

## Done when
- Unit of stock and source-of-truth identified; quantity change has audit entry; BOM components exploded before availability check; updates are idempotent.
