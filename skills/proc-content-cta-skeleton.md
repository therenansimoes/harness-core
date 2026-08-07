---
name = "proc-content-cta-skeleton"
kinds = ["content"]
description = "CTA marketing inventory content task: headline hook body CTA four-section skeleton"
paths = ["*.md", "*.html", "*.txt", "*.json", "**/*.md", "**/*.html"]
---
## Content CTA Skeleton

Situation: task asks for marketing copy, CTA block, product description, inventory summary, or any content output — no code involved.

Steps:
1. Identify the four required sections: **Headline** (1 line, benefit-first), **Hook** (1–2 sentences, problem or desire), **Body** (2–4 sentences, evidence or features), **CTA** (1 imperative sentence + next step).
2. Write all four sections in one `write_file` call. Do not write a partial draft and ask for feedback.
3. Keep the copy specific to the spec's audience, product, and channel — no generic filler.
4. Do NOT load a domain methodology skill just for structure. Use this skeleton for structure; load `marketing-methodology` or `inventory-methodology` only if you need domain-specific rules (tone, compliance, SKU format).
5. Run `read_file` to confirm the output file was written correctly.

## Done when
Output file written with all four sections; content is specific to the spec, not generic.
