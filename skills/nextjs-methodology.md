---
name = "nextjs-methodology"
kinds = ["code"]
description = "Next.js App Router page.tsx RSC server component routing app/ directory — not inventory/ecommerce"
paths = ["**/app/**", "**/*.tsx", "**/*.jsx", "**/next.config.*", "**/package.json"]
---
## Next.js (App Router) methodology

- Default to Server Components; add `"use client"` only for state, effects, or browser APIs.
- New page: `write_file` to `app/page.tsx` (parents are created). Minimal:

```tsx
export default function Page() {
  return <main>…</main>;
}
```

- Data: fetch on the server near the consumer; avoid waterfall client fetches for initial HTML.
- Routing: use `app/` segments, `loading.tsx` / `error.tsx` only when UX needs them — do not scaffold empty shells.
- Mutations: Server Actions or route handlers with explicit revalidation (`revalidatePath` / tags).
- Styling: follow the repo's existing system (CSS modules / Tailwind / tokens). Do not introduce a second UI kit.
- Verify: `npm`/`pnpm` build or the unit verify_cmd; fix type errors before claiming done.

## Done when
- Build or unit verify_cmd green; type errors fixed; no second UI kit introduced; mutations use Server Actions or route handlers.
