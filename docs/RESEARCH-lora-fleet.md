# LoRA fleet — dossiê de pesquisa (2026-08-04)

Plano: base Qwen3.5-4B (VLM, 262K ctx, thinking default ON) + adapters específicos roteados pelo graph. Fases: 1) frota com adapters prontos → 2) roteamento no graph → 3) flywheel de adapters próprios (trajetórias da fila + MLX QLoRA noturno + gate de promoção).

## Runtime (llama.cpp, lido do source em 2026-08-04)

- `llama-server -m base.gguf --mmproj mmproj.gguf --lora a.gguf,b.gguf --lora-init-without-apply`
- Seleção **por request**: `"lora":[{"id":1,"scale":1.0}]` no body de `/v1/chat/completions`. Swap O(1), todos residentes (~30-100MB cada), mmproj convive.
- Custos: trocar adapter limpa o prompt cache do slot (rotear com afinidade por adapter); requests com lora diferente não batcham; set de adapters é fixo no boot (hot-add = PR #22061 aberto).
- macOS: `brew install llama.cpp`; `-c` compartilhado entre slots (`-np 2` c/ 128K = 64K/slot); KV `-ctk/-ctv q8_0` corta ~50% do cache.
- **Blockers abertos**:
  - #21125: `convert_lora_to_gguf.py` **quebrado p/ arquitetura Qwen3.5** (fix #24627 não mergeado) → PEFT→GGUF não confiável hoje.
  - #16475: GGML_ASSERT crash com LoRA ativo no Metal (M2 Ultra), sem fix confirmado → **smoke test obrigatório antes de tudo**.
  - #26207: contaminação de prompt cache entre adapters (master tem clear; confirmar).
- Alternativas: `mlx_lm.server` aceita adapter por request mas **recarrega do disco a cada troca** (segundos, não O(1)); carrega PEFT/`adapters.safetensors` **nativamente, sem conversão**. llama-box não adiciona nada.

## Curadoria (477 adapters do model tree Qwen/Qwen3.5-4B → ~30 utilizáveis)

Realidade: 150 repos são seed-sweep acadêmico; quase todo repo "lora+GGUF" é **merge full**, não adapter; só 5 GGUF-LoRA verdadeiros no ecossistema (42-130MB), quase todos sem card. **Categorias vazias**: sumarização EN, gestão de projetos, escrita técnica, market intelligence → candidatas ao flywheel (fase 3). Tradução PT/EN deixou de ser requisito (Renan 2026-08-04: inglês com os models está ok inicialmente). Fontes extras a varrer depois: ModelScope (LLM adapters, ecossistema Qwen); Civitai é só diffusion → vale pro nó de imagem via Draw Things API, não pro llama-server.

### Merges GGUF (rodam já, mas ocupam slot de modelo, não de adapter)
| repo | categoria | tam | spec | risco |
|---|---|---|---|---|
| Jackrong/Qwopus3.5-4B-Coder-MTP-GGUF | coding/agentic | Q4 2.78GB +mmproj 676MB | temp 1.0/top_p .95, think ON, 32K | bench self-reported |
| Jackrong/Qwen3.5-4B-Claude-4.6-Opus-Reasoning-Distilled-v2-GGUF | reasoning | Q4 2.71GB | think ON estruturado | alucina dentro do think |
| Kerassy/Qwen3.5-4B-Medical-Reasoning | médica | Q4 2.78GB | think ON, temp .7/top_p .9, max 1024 | 2% timeout |
| DavidOKB/MathThink-Qwen-3.5-4B | matemática | Q8 4.48GB | temp 1.0, **rep_penalty 1.1 obrigatório** | loop sem penalty |
| CFitzsimons/Qwen3.5-4B-APIGen-MT-5k-F16-GGUF | tool multi-turn | F16 8.67GB | chat_template c/ tools | precisa quantizar |
| astrla/apolo-korax | extração JSON (ES) | Q4 2.71GB | schema Apolo | escopo estreito |

### PEFT adapters reais (precisam de MLX ou do conversor consertado)
| repo | categoria | tam | spec crítica | risco |
|---|---|---|---|---|
| carlosmm26/Atanor-4B | agent/tool-use | **85MB (+merge+GGUF!)** | think ON default; `enable_thinking:false` p/ desligar; temp 0 | raro: publica os 3 formatos |
| sukhrobnurali/tooltuned-qwen-3.5-4b | tool-calling | 12.6MB | `<tool_call>`, schemas no system | treinado a 2048 tokens |
| sahilsangwan/qwen35-4b-text-to-sql-data-forge-lora | SQL | 32.5MB | max_tokens 128; formato MLX | evals Spider inclusos |
| singhabhishekkk/apprentice-…-jsonextract | extração JSON | 85MB | — | 70 exemplos; F1 88.9 vs 69.1 |
| singhabhishekkk/apprentice-…-receipts | OCR recibos | 85MB | company/address/date/total | F1 89.2 vs 42.5 (melhor delta) |
| hxcsa/qwen35-4b-docvqa-lora | DocVQA | 170MB | **`enable_thinking=False` essencial**, greedy, max 64 | respostas curtas |
| Yesianrohn/htmlgen-qwen3.5-4b-lora | imagem→HTML | 130MB | system prompt fixo; temp 0; max 10240 | trunca página densa |
| ykylcxlx/qwen35-4b-mmtab-232k-lora | tabelas | 65MB | template `qwen3_5_nothink`; cutoff 2048 | checkpoint parcial |
| SamsungSAILMontreal/chart2table-qwen3.5-4b-v1 | gráfico→tabela | 78MB | nothink; saída = CSV markdown puro | procedência boa |
| zkjzou99/code-prm-critic-lora-32k | critic de trajetória | 8.8MB | entrada=trajetória, saída=score/turno | nicho SWE-bench |
| AbijahKaj/qwen3.5-4b-kicad-netlist | eletrônica/KiCad | 260MB | nothink, temp 0.3, tools embutidas | 10 downloads |
| Tynapse/drift-sentry-4b-v1 | judge/safety | 170MB | temp 0 + JSON format, cap 512 | única c/ métrica de prod |
| marcodsn/remora-4b-v0 | roteamento multi-agente | 312MB | capability cards no system, temp 0 | GRPO 50 steps |
| gabrielgts/qwen3.5-4b-ec-magento | extração e-commerce | 42.5MB | nothink obrigatório; greedy | escopo Magento |
| lukasjordan/qch-vuldet-…-paired-adapter | vuln detection | 85MB | sem specs | checkpoint ambíguo |
| KabirOnline/svg-llm-qwen4b-icons | design/SVG | 85MB | sem specs | única de design |
| Oysiyl/qwen3.5-4b-unslop-good-lora-v1 | reescrita/estilo | 42.5MB | sem specs | card vazio |
| mirazrafi/NSFW-RP-RolePlay-LoRA-Qwen-3.5-4B | criativa/RP | 680MB | **ChatML**, não template Qwen | 18+ |
| unileon-robotics/Qwen3.5-4B-GPSR-LoRA-GGUF | robótica (referência) | **GGUF-LoRA 65MB** | card vazio | usar como cobaia do smoke test |
| athrael-soju/colqwen3.5-4.5B-v3 | retrieval visual | 9GB merge | não é chat — embedder ColQwen | infra própria |

Descartados com nota: teilomillet/quaero (0/16 Spider declarado), Scrymore/stone-preview-4b (vLLM descarta deltas da vision tower; só merge 9GB), mdabis/gui-grounding (README 401).

### Varredura ModelScope (2026-08-04; 937 derivações, colheita fraca — 2 pepitas)
| repo | categoria | formato | tam | spec | risco |
|---|---|---|---|---|---|
| twinkle-kit/Qwen3.5-4B-Condenser (ModelScope) | **sumarização/compressão EN** — preenche gap + serve p/ condensar contexto do harness | PEFT r16 | 78MB | system prompt verbatim no card; temp 0.3; budget dinâmico de tokens; saída `## Summary`+`## More` | baixo — card excelente, 15.8k dl |
| AlexWortega/qwen35-4b-clawd-rift (HF — escapou da 1ª curadoria) | **agente de código** | PEFT r64/α128 | — | ClawGym 37.10 (bate Qwen3-32B, 33.11); SWE-bench 7/89 | melhor agente de código 4B das duas plataformas; reavaliar vs Jackrong merge |
Rejeitados: twinkle CM (acoplado byte-a-byte a runtime GRPO próprio), anker3586 toolcall (456 amostras/45min de treino), o resto é card vazio ou só-zh. Categorias gestão/escrita técnica/BI/design/frontend/testes: **vazias também no ModelScope** → confirmadas como alvo do flywheel.

## Decisão pendente (levar ao architect)

- **A — MLX-first**: mlx_lm serve PEFT nativo (sem conversor quebrado, sem bug de Metal do llama.cpp) e é onde o flywheel treina; custo = swap por reload (segundos) e vision via mlx-vlm à parte.
- **B — llama-server + conversor patchado** (PR #24627 local): mantém per-request O(1) + mmproj; custo = manutenção de patch + risco #16475.
- **C — híbrido**: merges GGUF como modelos trocáveis no LM Studio/llama-server + PEFT via MLX pros nichos.
- Passo 0 em qualquer caso: smoke test Metal+LoRA (base 4B Q4 + GPSR-LoRA 65MB) — decide A vs B em ~10min.

### Curadoria Llama-3.2-3B-Instruct (2026-08-04; 810 adapters no total, 500 varridos, mediana <10 dl)
Upgrades sobre elos fracos: **minpeter/LoRA-Llama-3b-v1-iteration-00-sf-xlam-05** (tool-calling, xLAM-60k+BFCL, treina "quando NÃO chamar" — CAVEAT: template Hermes `<tool_call>`, não o nativo do Llama, exige mudança no wrapper) · **STELLiQ/aria-aar-3b-lora** (JSON estruturado + sumarização, 3 system prompts próprios, seq 6144 — exige consumo do campo `system`) · **mehmet1899/llama32-3b-instruct-nl2sql-lora** (SQL, 25k exemplos Spider+Create-Context, tese de mestrado). Gaps novos: **scthornton/llama-3.2-3b-securecode** (review de segurança OWASP, 2.372 ex, vulnerável+segura+exploit+mitigação) · **vinod-anbalagan/marketing-spend-revenue-qa** (BI numérico multi-step, abstém sem dado, 88% win vs base) · **Despina/re_mixtune-2-shot** (extração de relações F1 0.827, bate GPT-5.4 zero-shot) · slot-value extractor (24MB). Risco alto (card vazio): testsentry (testes), tech_doc, risk-register. **Não existe em nenhum base: git/commit messages e code review geral → flywheel.** Base: meta-llama/Llama-3.2-3B-Instruct 4bit MLX ~1.8GB. Dumps dos 26 finalistas no scratchpad da sessão (out.txt).

## E2E v2 (2026-08-04): base-swap PASS, formato PEFT FAIL

- Troca de base por request no mlx_lm.server: **funciona**, ~1,5s de overhead (Qwen 4B ↔ Llama 3B, um processo). `/v1/models` varre o cache HF inteiro.
- **mlx_lm.server NÃO lê adapter_config.json PEFT** (`'SimpleNamespace' object has no attribute 'num_layers'`, engolido como 404 sem traceback) — espera formato próprio (num_layers+lora_parameters). O falsificador original passou porque o sahilsangwan era MLX-nativo por acaso. Maioria da frota v2 é PEFT → conversor scripts/convert_peft_to_mlx.py em prova (builder timeboxado). Fallback se falhar: dual-runtime (adapters de base Llama convertem pra GGUF — #21125 é só arch qwen3.5 — e rodam no llama-server já smoke-testado).
- **Doctor falso-verde**: check adapters valida dir + /v1/models, não inferência. F2.5: canário de 1 token com adapter real.

## DECISÃO (architect, 2026-08-04): C disciplinado

`mlx_lm.server` (porta 1235) = único runtime de LoRA (PEFT nativo = formato do flywheel); LM Studio (1234) segue com base/merges GGUF e visão base-pura; llama-server engavetado até llama.cpp#24627 mergear (sem patch local: rebase eterno + falha silenciosa do conversor). Fila é serializada (`harness/queue.py:155`) → swap O(1) não tem consumidor; afinidade de run limita trocas a 3-5x/fila. Adapter = 3º eixo fora do router de custo (não é tier!). Spec completa de implementação no output do architect (sessão 2026-08-04): adapters.toml + harness/routing/adapters.py (matcher roubado de skills/loader.py), Selection/ExecRequest/UnitSpec ganham `.adapter`, _route congela adapter nos retries, deepagents_backend._model_for manda `extra_body={"adapters": ref}`, doctor sonda com WARN. Passo 0 bloqueante: falsificador do mlx_lm.server (verifier). Vision+LoRA = TODO linkado a #24627.

## Smoke test executado (2026-08-04) — PASS

- llama.cpp build 10250 (brew), M3 Pro Metal: #16475 NÃO reproduz. 30+ gerações com LoRA, 0 GGML_ASSERT, servidor estável, RAM ~4GB.
- Per-request lora funciona: scale 1.0 → saída do adapter (JSON GPSR), scale 0.0 → saída base, temp 0/seed fixo. ~31 tok/s com adapter, ~34 sem.
- **Gotcha**: `--lora-init-without-apply` quebrado no build 10250 (adapters sobem em scale 1.0). Mitigação: toda request DEVE mandar `"lora":[...]` explícito com scale de todos os adapters desejados (ausente = 0 só quando o campo lora está presente).
- `Qwen/Qwen3.5-4B-GGUF` oficial não existe; base usada: `bartowski/Qwen_Qwen3.5-4B-GGUF` Q4_K_M (2.8GB) + mmproj f16 (641MB). Arquivos no scratchpad da sessão; mover p/ local definitivo na implementação.
- Base loga aviso Qwen-VL recomendando `--image-min-tokens 1024`; blk.32.* (MTP) ignorado pelo runtime — cosmético.
