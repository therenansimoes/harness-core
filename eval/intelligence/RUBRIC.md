# Intelligence process rubric (outside genome / sealed)

Score each axis **0–5**:

| Score | Meaning |
|-------|---------|
| 0 | Nothing useful / hallucinated |
| 1 | Attempted, wrong path |
| 2 | Right direction, wrong outcome |
| 3 | Outcome OK, messy execution |
| 4 | Outcome OK, reasonable efficiency |
| 5 | Outcome OK, minimal steps, clean |

## Axes and weights

| axis | weight | Look for in the trace |
|------|--------|------------------------|
| planning | 0.15 | Decompose before bulk edits |
| study_before_act | 0.15 | Read/research before write |
| tool_selection | 0.15 | Right tools, real paths (no invented `/p/`) |
| efficiency | 0.10 | Steps ≈ minimum needed |
| error_recovery | 0.15 | After failure: change approach, don't blind-retry |
| verify_before_claim | 0.10 | Real verify evidence before claiming done |
| task_completion | 0.20 | Outcome + deterministic verify when present |

**overall** = weighted average of the seven scores (0–5).

## Judge output (JSON only)

```json
{
  "scores": {
    "planning": 0,
    "study_before_act": 0,
    "tool_selection": 0,
    "efficiency": 0,
    "error_recovery": 0,
    "verify_before_claim": 0,
    "task_completion": 0
  },
  "overall": 0.0,
  "rationale": "one short paragraph"
}
```
