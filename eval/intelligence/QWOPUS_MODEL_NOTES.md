# QWOPUS_MODEL_NOTES — model-aware harness tuning
> Filed 2026-08-07 · research by Cursor researcher agent

---

## 1. Model identity

| Field | Value |
|---|---|
| Served name | `qwopus3.5-4b-coder-mtp` |
| HuggingFace | [`Jackrong/Qwopus3.5-4B-Coder-MTP-GGUF`](https://huggingface.co/Jackrong/Qwopus3.5-4B-Coder-MTP-GGUF) |
| Format | GGUF — **Q4_K_S** (2.63 GB on disk, user-confirmed LMS load) |
| Author | Jackrong (community, 2026) |
| Base | Qwen/Qwen3.5-4B (dense transformer, 4B-class) |
| Context (native / architectural) | 262 144 tokens |
| Context (fine-tune training max) | 32 768 tokens (HF card: "training data constructed around samples up to 32K") |
| **Context (LMS loaded — ground truth)** | **8 192 tokens** (user LMS screenshot 2026-08-07; earlier notes said 32768 — WRONG) |
| Quant | Q4_K_S (2.63 GB) — **not** Q4_K_M |
| GPU offload | 33 layers |
| MTP | n=2 draft tokens; draft probability 0.75; min draft 0; speculative decode via `--speculative-draft-mtp` |
| Flash Attention | ON |
| KV | Unified KV + Offload KV to GPU ON |
| Sampling | temp 0.1 · top-k 40 · top-p 0.95 · min-p 0.05 · repeat-penalty 1.1 |
| Context overflow | Truncate Middle |
| Model path | `/Users/renansimoes/.lmstudio/models` |
| Parallel | 1 (sequential; single LMS slot) |

**Sources:** user LMS screenshots 2026-08-07 (ground truth for machine config); [`Jackrong/Qwopus3.5-4B-Coder-MTP-GGUF`](https://huggingface.co/Jackrong/Qwopus3.5-4B-Coder-MTP-GGUF) HF model card (architecture, quant table, training recipe); HF file tree (Q4_K_S = 2.63 GB confirmed).

---

## Who sets what (code vs LMS UI)

Per-request fields in the OpenAI-compat `chat.completions` body **override** LMS UI sampling defaults for that call. Load-time LMS knobs (context, GPU offload, MTP, flash attn) are **not** set by harness over the chat API.

| Setting | Source on `harness run` (deepagents) | Notes |
|---|---|---|
| `temperature` | **code** `MODEL_TEMPERATURE=0.2` | Wins over LMS panel (e.g. 0.1). Adapter card can override (`adapters.toml`). |
| `max_tokens` | **code** `DEFAULT_OPENAI_MAX_TOKENS=4096` | Per-call output cap; not context length. |
| `top_p` / repeat penalty | **LMS UI** (default path) | Harness omits them unless an adapter sets `top_p` / `repeat_penalty`. |
| `enable_thinking` | **code** `False` via `THINKING_EXTRA_BODY` | Serve/Cursor path defaults **ON** (`cursor_openai.py`). |
| context length | **LMS load / UI** | e.g. `-c 8192`; harness cannot change loaded ctx via chat API. |
| MTP / GPU offload / flash attn | **LMS load / UI** | `lms load … --gpu max --speculative-draft-mtp`; not per-request. |

`base_url` default: `http://127.0.0.1:1234/v1` (`OPENAI_BASE_URL`). Built in `_model_for` → `init_chat_model(model, temperature=…, max_tokens=…, extra_body=…)`.

---

## 2. Architecture and training facts

### 2.1 Qwen3.5 base architecture

- Dense transformer; 16 Q heads / 4 KV heads (GQA); hidden 2048; FFN intermediate 9216.
- Tied embeddings (LM Output 248 320 tokens).
- Trained natively on long context (262 k); RoPE positional encoding.
- **Dual-mode thinking:** operates in thinking mode by default (`<think>…</think>` tokens); can be suppressed via `chat_template_kwargs: {enable_thinking: false}` at inference time.

*Source: `Qwen/Qwen3.5-4B` HF model card (architecture table), retrieved 2026-08.*

### 2.2 MTP — Multi-Token Prediction

MTP here is a **speculative decoding** technique (not a training objective that changes reasoning depth):
- The model architecture includes an MTP head predicting n=2 tokens ahead.
- At inference, the draft head proposes 2 tokens; the main head verifies; accepted tokens are free throughput.
- MTP does NOT change the semantic capability of the model — it speeds up decode on hardware with memory bandwidth headroom.
- LM Studio flag: `--speculative-draft-mtp` enables it; without it, the model runs single-token.

*Source: Qwen3.5-4B HF model card (MTP section); `docs/CURSOR.md` (lms load command).*

### 2.3 Qwopus training recipe (Coder fine-tune on top of Qwen3.5-4B)

Three training phases, per Jackrong model card:
1. **Trace Inversion** — learnable reasoning traces: the fine-tune teaches the model to generate *useful* intermediate steps rather than generic CoT.
2. **High-quality agent trajectories** — SFT on curated tool-use sequences; prioritises structured tool-call format stability.
3. **Curriculum SFT** — graduated difficulty to preserve formatting stability under longer contexts.

Result: vs. base Qwen3.5-4B-MTP, Qwopus-Coder shows improved structured output (function calling, JSON, code diff) and reduced hallucination of tool names.

*Source: `Jackrong/Qwopus3.5-4B-Coder-MTP-GGUF` HF model card (training recipe table), 2026-08.*

### 2.4 Chat template and special tokens

Qwen3.5 uses the standard Qwen3 template:
- System prompt in `<|im_start|>system … <|im_end|>`.
- Thinking enclosed in `<think>…</think>` before the response text.
- Tool calls in standard OpenAI JSON schema (`{"name": …, "arguments": …}`).
- `/no_think` user-message prefix suppresses thinking for one turn (alternative to `enable_thinking: false`).

*Source: Qwen3.5-4B HF model card; QwenCloud docs (thinking controls).*

---

## 3. Thinking / reasoning_content — known LMS behaviour

**Key observed fact (harness comment, `harness/backends/deepagents_backend.py`):**

> "o build qwen3.5-9b-mlx IGNORAVA a flag … os builds novos (qwen/qwen3.5-9b, qwopus-coder) OBEDECEM — com True o turno afoga em reasoning_content e o agente lê mas nunca escreve"

Translation: the current Qwopus-Coder GGUF build **obeys** `enable_thinking: false`. The harness deliberately sets it to **False** since 2026-08-06 because with thinking ON + a short `max_tokens` budget, the model fills tokens with reasoning and produces an empty `content` field with no `tool_calls`.

**Why LM Studio still emits `reasoning_content` regardless:**
- LMS maps the model's `<think>` block to `reasoning_content` in the OpenAI-compat delta even when `enable_thinking: false` is passed.
- The flag controls whether the Jinja2 chat template *prompts* the model to think; if the model already generated the think block, LMS still surfaces it.
- `langchain-openai 1.4.1` does not extract `reasoning_content` from the `AIMessage`, so harness traces do not show it — the model's thinking is invisible in spans.

**Tool-calls + thinking interaction:**
- When thinking is ON and `max_tokens` is tight, reasoning tokens eat the budget → `content=""` + no `tool_calls` → EmptyTurnMiddleware fires.
- With thinking OFF + `DEFAULT_OPENAI_MAX_TOKENS=4096`, the turn has room to emit a tool call before hitting the cap.

*Sources: `deepagents_backend.py` comments (inline, lines referencing runs 7f71cce2457b and 1d9542428a9c); QwenCloud thinking docs.*

---

## 4. Context and KV budget ⚠ CORRECTED 2026-08-07

**Loaded context is 8 192 tokens — not 32 768.** All earlier notes that said "32k is comfortable" were wrong. The budget is extremely tight. Raising LMS context to 16k/32k if VRAM allows would help; until then **8k is the contract**.

| Layer | Tokens | Notes |
|---|---|---|
| LMS loaded ctx | **8 192** | user LMS screenshot — ground truth (Q4_K_S, MTP n=2) |
| Always-on prompts | ~1 700 tok | executor.md + tools prompt + procedures index |
| Per-turn skill bodies (pre-E3) | ≤ 3 × ~650 tok ≈ 1 950 tok | SELECT_LIMIT=3 × 500w bodies at ~1.3 tok/word |
| Per-turn skill bodies (control) | ≤ 3 × ~650 tok ≈ 1 950 tok | SELECT_LIMIT=3 × 500w bodies at ~1.3 tok/word (E3 150w DISCARD) |
| Subtotal (system + skills, post-E3-DISCARD) | **~3 650 tok** | consumed before the first user message |
| Available for dialogue + tool I/O + generation | **~4 540 tok** | with `max_tokens=4096`, output alone could eat this |
| Headroom after max_tokens | **~440 tok** | for accumulated tool output across turns |

**This is the critical constraint — not attention or working memory.** 500w bodies keep the KV at equilibrium. E3 (150w cap) was shipped but **DISCARD** (9/12 accept, 3 verify failures at 600s: rename/jt timeout).

**Implications:**

- E3 (body cap 150w) DISCARD — truncated proc cards below actionable threshold.
- `max_tokens=4096` may be too high given the 8k window. **E2** (`max_tokens=2048` on simple micros) is next P1 after POST_E3 (`ym0.22`) — no runtime change until then.
- `HARNESS_SERVE_MAX_PROMPT_CHARS=24000` is calibrated for a larger ctx; at 8k it can be the overflow trigger for longer units — review if truncation artefacts appear.
- Docs corrected 2026-08-07: `BASELINE.md`, `CAMPAIGN.md`, this file. Runtime still sends temp=0.2 max_tokens=4096 thinking=false.

*Sources: user LMS screenshots 2026-08-07 (loaded ctx = 8192 ground truth); `harness/backends/deepagents_backend.py` (`DEFAULT_OPENAI_MAX_TOKENS=4096`, `HARNESS_SERVE_MAX_PROMPT_CHARS`); `harness/skills/loader.py` (SELECT_LIMIT=3, 500w cap).*

---

## 5. Failure modes characteristic of Qwopus/SLM class — mapped to harness levers

| Failure mode | Root cause | Harness lever | Status |
|---|---|---|---|
| **Empty turn** (content="", no tool_calls) | Thinking ate `max_tokens` budget OR model uncertain and stalls | `EmptyTurnMiddleware` nudge + `max_tokens=4096` fix + `enable_thinking=false` | **Shipped** (ym0.6, ym0.8) — still 100% hit rate; middleware ceiling reached |
| **Prose-as-tools** (JSON in content, not in tool_calls) | Fine-tune didn't fully overcome base Qwen3 text-generation bias for tool schemas | `ToolSalvage` middleware silently re-routes | **Shipped** (ym0.14) |
| **Path confusion** (`/p/file.py`, `/tmp/…`) | 4B model hallucinates absolute paths from training distribution | `openai_qwopus3.5-4b-coder-mtp.md` tools prompt, virtual root rule | **Shipped** |
| **Long CoT steals turn budget** | With thinking ON, ~2k reasoning tokens leave <2k for output at 4k max | `enable_thinking=false` default | **Shipped** (2026-08-06) |
| **Skill body ignored / overloaded** | Small working memory; fat procedures not attended | SELECT_LIMIT=3, PATH_CAP=1, 500w body cap | **Shipped** (ym0.18 KEEP) |
| **Overly broad `paths=` match** | Skill loader matches too many skills → prompt bloat → attention dilution | Narrow `paths=` entries | **Open (ym0.19)** |
| **Content-task wall** (marketing/inventory slow) | Domain methodology cards + SELECT_LIMIT=3 × body cost | Reduce body or specialise CTA/inventory | **Open (ym0.20)** |
| **Multi-turn KV decay** | At 4B, cross-turn coherence degrades after ~15 tool turns; plan evaporates | Procedure chaining (each turn is recoverable) | **Shipped (MICRO_PROCEDURES design, ym0.13–ym0.14)** |
| **Rename partial (misses callsites)** | SLM completes rename of declaration but forgets callsites without grep confirmation | `proc-rename-via-write.md` + `proc-check-listed-files.md` | **Shipped (ym0.14)** |
| **Verify hallucination** ("it passed" without running) | Training distribution: models report completion optimistically | `CompletionGuard` + `proc-verify-then-stop.md` | **Shipped** |

---

## 6. Implications for micro-procedures (prompt engineering for this tokenizer/template)

**What works for Qwen3.5-4B-class:**

- **Imperative, numbered steps.** Not "consider doing X" — "1. call `read_file`. 2. call `edit_file`." The model follows ordered lists reliably; abstract guidance evaporates.
- **One action per procedure.** A 500-word card with 10 rules is attended less than a 200-word card with 3 rules for one situation.
- **Name the tool explicitly.** "use `write_file`" beats "create the file". The fine-tune mapped tool names to actions; exploit that.
- **No abstract rubric language.** Phrases like "ensure correctness" or "be careful about" are filler at 4B. Replace with observable actions: "run `grep` to confirm no old name remains".
- **Situation header first.** The model pattern-matches on the first line to decide whether a procedure is relevant. "After empty-turn nudge:" selects the right card faster than a paragraph explanation.
- **Short = selected.** Bodies under 250 words are more reliably loaded within PATH_CAP; bodies over 500 words are trimmed by the loader.

**What does NOT work (contrast with Claude/Opus):**

- Long reasoning chains in prompts — the model doesn't retain them across tool turns.
- Abstract methodology sections — domain knowledge like "understand the customer journey" is noise for a task requiring `write_file("/checkout.tsx", …)`.
- Asking the model to "plan and then execute" in one turn — at 4B, planning output competes with action tokens under max_tokens cap.

*Sources: `eval/intelligence/MICRO_PROCEDURES.md` (design principles); `eval/intelligence/CAMPAIGN.md` (design lock section); backend comments.*

---

## 7. Five concrete experiments tuned to this weight

All judge-blind (process KPIs only: `empty_turn_hits`, `sec_total`, `tool_calls_per_turn`, `first_tool_turn`). Run against POST_LIMIT3 as control.

### E1 — `/no_think` prefix vs `enable_thinking=false` extra_body
**Hypothesis:** Prepending `/no_think` to the user message (Qwen native signal) further reduces thinking token emission even when `enable_thinking=false` is already set via template, freeing ~500 tok of budget for the first tool call.  
**Measure:** `first_tool_turn` (expect drop from 1.0 to <1.0 mean), `sec_total`.  
**Lever:** `prompts/executor.md` — prepend `/no_think\n` to system or first user turn.

### E2 — `max_tokens=2048` on simple micro-units vs 4096
**Bead:** `harness-core-ym0.22` (P1 next after POST_E3). **Do not change runtime until POST_E3 measure lands.**  
**Hypothesis:** For units with ≤2 file edits (micro_python_add, micro_edit_line), 2k is sufficient and halves median `sec_total`; no empty-turn increase because thinking is off. At 8k loaded ctx this is more important than originally ranked.  
**Measure:** `sec_total`, `empty_turn_hits`, pass rate. KEEP if wall/empty_turn better without accept drop.  
**Lever:** route-level `max_tokens` by unit complexity tag in `config/agents.toml`.

### E3 — Procedure body cap at 150 words (vs current 500)
**Status: DISCARD** (reverted 2026-08-07). 150w cap shipped but failed POST_E3: 9/12 accept, 3 verify timeouts (rename, json_transform at 600s).  
**Result:** Truncation of proc card bodies below actionable threshold. Revert to 500w + keep `CONTENT_SELECT_LIMIT=2` (ym0.20 KEEP).  
**Measure attempted:** `warn_nudge` count, `empty_turn_hits` on marketing/inventory quarantine set.  
**Lever reverted:** `_BODY_WORD_LIMIT=500` in `harness/skills/loader.py` (POST_E3 control).

### E4 — Explicit `grep`-confirmation gate in rename procedure
**Hypothesis:** Adding "step 3: `grep` for old name; if any hits remain, repeat edit" to `proc-rename-via-write.md` eliminates partial-rename failures in micro_refactor_rename without adding turns.  
**Measure:** `tool_calls_per_turn` on rename units, pass rate.  
**Lever:** edit `skills/proc-rename-via-write.md` body.

### E5 — PATH_CAP=0 for recovery procedures (EmptyTurn nudge path)
**Hypothesis:** When EmptyTurn fires, loading `proc-recover-after-empty-turn.md` via the nudge path (not the path-trigger path) without the PATH_CAP=1 restriction gives the model the full recovery card immediately, reducing second-consecutive empty turns.  
**Measure:** second-empty-turn rate (consecutive `empty_turn_hits`), `sec_total`.  
**Lever:** `harness/backends/empty_turn.py` — inject procedure body directly in nudge text (bypasses SELECT entirely).

---

## 8. What remains unresolved

- ~~Exact GGUF quant~~ — **resolved: Q4_K_S** (user LMS confirmed 2026-08-07; 2.63 GB).
- Whether LMS's speculative-draft-mtp acceptance rate is high enough to justify `--parallel 2` for the next RAM slot.
- Trace Inversion details (paper/code not publicly available as of 2026-08); claims are from Jackrong model card only.
- `langchain-openai 1.4.1` `reasoning_content` gap — confirmed by docstring, not by patch yet; upstreaming not tracked.
