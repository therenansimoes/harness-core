# Sources for harness process lifts (2026-08-07)

Primary: **subject/harness before judgment**. Judge sources kept secondary.

## Harness / scaffolding (subject)

- https://hugobowne.substack.com/p/how-to-build-an-effective-agent-harness — harness as product; context vs action complexity; retest scaffolds when model changes
- https://sarahpayneai.substack.com/p/procedural-intelligence-how-agents — Prediction→Reasoning→Action→Verify→Recovery as architecture; model↔logic seam
- https://www.sarahpayne.ai/procedural-intelligence-framework — procedural blocks, gates, fallbacks
- https://arxiv.org/html/2607.17598 — progressive disclosure: one skill level helps; deeper routing often hurts; buys context not intelligence
- https://aipatternbook.com/progressive-disclosure — tiered index → body → references
- https://wowhow.cloud/blogs/claude-code-skills-architecture-3-layer-agent-harness-pattern-2026 — 3-layer agent harness; short always-on index
- https://notchrisgroves.com/hierarchical-context-framework/ — hierarchical context loading
- https://zylos.ai/research/2026-04-08-ai-agent-skill-acquisition-self-improvement-architectures/ — Agent Skills progressive load; avoid >2k always-on instruction tokens
- https://pypi.org/project/langgraph-kit/ — EmptyTurn, CompletionGuard, ToolError, context pressure middleware peers
- https://docs.langchain.com/oss/python/langgraph/fault-tolerance — RetryPolicy / TimeoutPolicy / error_handler
- https://runlocal.cc/blog/canonical-local-llm-skill-library — LocalLLaMA short restrictive skills
- https://github.com/ahwurm/localharness — local SLM agent harness patterns
- https://github.com/gary149/llama-agent — light loop for small models
- OpenCode/Ollama issues (tool JSON in content, empty turns, thinking vs tools) — SLM failure modes the harness must absorb

### Concrete lifts (2026-08-07 research)

- https://github.com/MUSE-CODE-SPACE/toolcall-rescue — tool-call salvage; JSON in content → structured tool_calls
- https://github.com/vystartasv/toolcall-proxy — tool-call proxy for LMS tool_choice fallback
- https://arxiv.org/html/2606.15508 — ToolMenuBench; phase-gated tool exposure; smaller models need constrained tool sets
- https://github.com/clay-good/OpenLore/blob/main/examples/opencode/agent-guard.ts — `toolsWereUsed` pattern for CompletionGuard
- https://github.com/1012638836/openwork/commit/e6fd86c6bbd7d880979a054f57e4d71423b8b18f — completion guard + tool verification
- https://github.com/lmstudio-ai/lmstudio-bug-tracker/issues/1592 — LMS thinking steals budget from tool_calls; reasoning_content even with enable_thinking:false
- https://aclanthology.org/2026.acl-long.1880.pdf — FISSION-GRPO; SLM recovery from tool errors; structured next-action
- https://doi.org/10.48550/arxiv.2510.03847 — SLM Agentic agents survey; tool constraints, recovery patterns
- https://rahulkashyap.dev/blog/harness-observability.html — process KPI instrumentation; empty_turn_rate, first_tool_turn

## Scoreboard only (judge / eval — secondary)

- https://micheallanham.substack.com/p/why-success-is-lying-to-you-the-2026 — outcome fallacy
- https://metr.substack.com/p/2025-06-05-recent-reward-hacking — reward hacking
- https://blog.prompt20.com/posts/agent-evaluation/ — state + trajectory; constraints not golden paths
- https://dreaming.press/posts/2026-06-27-pass-at-k-vs-pass-hat-k-agent-reliability-evals.html — pass@k vs pass^k
- https://arxiv.org/pdf/2602.07150 — randomness in agentic evals
- https://github.com/alphadl/adarubrics — dimension masking / min-score
- https://scorable.ai/post/measure-and-reduce-noise-in-agentic-llm-evals — judge noise
- https://zylos.ai/research/2026-05-26-llm-as-judge-agent-evaluation-patterns/ — calibration (use sparingly)

## X.com
- Direct scrape limited; add bookmarks when available.
