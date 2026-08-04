# Ecossistema LoRA — roadmap (2026-08-04)

Visão: o graph do harness como **gerente** que roteia cada tarefa para `(runtime, base, adapter)` — primeiro numa máquina, depois em três. Não existe supermodel: existe composição roteada.

## Princípios (decididos, não reabrir sem motivo)

1. **Habilidade vive em adapter, fato vive em RAG** (LoRA não decora fato; embedding acha, adapter processa).
2. **Base não é fixo**: qualquer base ≤5B entra; teto de ~9GB por modelo individual; nunca 30B (histórico: travou o note).
3. **PEFT nativo é o formato canônico** (mlx_lm serve direto; flywheel produz; conversor GGUF da arch Qwen3.5 está quebrado upstream — llama.cpp #21125).
4. **Toda request declara o adapter explicitamente** (per-request; nunca depender de estado default do servidor).
5. **Afinidade por adapter na fila** (troca custa ~1,2s de reload no MLX; agrupar tarefas do mesmo adapter).
6. **Gate de promoção obrigatório** pra adapter novo/treinado (suite de smoke congelada; sem gate = regressão silenciosa).
7. Execução barata: Fable orquestra; researcher/builder/mechanic/verifier executam; lookup simples = haiku.
8. **Roteamento é reflexo, não deliberação** (Renan+arquitetura 2026-08-04): decidir rota nunca gasta token de LLM nem injeta descrições de adapter em prompt. Escada: hoje = matcher determinístico (explícito > kind > path-globs > tokens; score 0 = base pura); upgrade 1 (se o placar do ledger denunciar misroute) = router por embedding com o nomic local (descrições embedadas 1x, cosseno, ~ms); upgrade 2 (último caso) = embedding pré-seleciona top-3 + mini-LLM 2B decide entre 3 descrições curtas. Deliberação cara só pós-travamento (debate F2.6).

## Tiers de execução (do mais barato pro mais caro)

| tier | runtime | o quê | status |
|---|---|---|---|
| T0 sistema | Apple FM (~3B, grátis, on-device) via `afm` server | tarefas leves, iPad nativo | fase 2.5, avaliar |
| T1 frota | mlx_lm.server :1235 — bases ≤5B + adapters PEFT | especialidades (o grosso) | **rodando (falta subir servidor)** |
| T2 pesados | LM Studio :1234 — merges GGUF + qwen3.5-9b | coder-heavy, reasoning, vision base-pura | ativo |
| T3 nuvem | deepagents openai:* | o que o local não dá conta | ativo (zera adapter) |

## Fases

- **F1 — infra + 5 adapters** ✅ (2026-08-04): registry `config/adapters.toml`, matcher, routing com afinidade, backend extra_body, doctor. sql/tools/jsonextract/judge/condense.
- **F1.5 — pesos-pesados** 🔄 (mechanic rodando): +code (clawd-rift), agent (Atanor), receipts, coder-heavy e reasoning (merges LM Studio). Frota = 10.
- **F2 — expansão escolhida pelo Renan**: +medical, maththink (merges), htmlgen (pendente vision), nl2jq e OCR sobre base 2B (multi-base). Censo de ecossistemas ≤5B (2026-08-04): Llama-3.2-3B-Instruct = 200+ adapters (maior ecossistema alternativo, sem vision, maduro — provável fonte pras categorias vazias tipo sumarização/escrita); Qwen3.5-2B = 139 (VLM); SmolLM3-3B = 47; Ministral/granite/gemma ≤6. Próxima curadoria: Llama-3.2-3B focada nos gaps. Frota ≈ 15.
- **F2.5 — qualidade e medição**: coluna `adapter` no ledger (placar por adapter, A/B vs base); pricing do base MLX em models.toml; consumo do campo `system`; avaliar runtime `afm` (Apple FM + adapter toolkit oficial — Renan é Developer Program; atenção: .fmadapter retreina a cada release do OS); consertar doctor `--root`; ask-local → LM Studio.
- **F2.6 — multi-expert debate (decidido com Renan 2026-08-04)**: o graph inicia debate quando a tarefa está **difícil de resolver** — gatilho por sinal de travamento (retries falhando, score estagnado, loop guard / blocker tipado), não por rota fixa. Escada: (1) debate-lite = 3x sampling + agregação (union p/ cobertura, majority p/ extração — evidência no bench VLM do dossiê) antes de retry caro; (2) debate completo = 2-3 adapters de vieses diferentes (ex. reasoning × securecode × judge, bases distintas) + veredito estruturado, disparado antes de escalar pra nuvem e no gate do flywheel; (3) reavaliar `remora` como moderador; (4) **sem consenso no debate → blocker tipado sobe pro Fable como ADVISOR, não executor** (Renan 2026-08-04): Fable devolve só o veredito/decisão; a execução volta pra frota local. Decisão registrada no ledger vira trajetória de treino pro flywheel. Design: architect, depois da v2 estável — junto com a decisão do destravamento Hermes/xLAM (2 rotas documentadas pelo builder).
- **F3 — flywheel**: trajetórias boas da fila (score-gradient rotula) → dataset → QLoRA no Colab free (T4; Unsloth) ou MLX local → adapter PEFT → gate → registry versionado. Alvos: os buracos que nenhuma plataforma tem — gestão de projetos, escrita técnica, BI/market, sumarização do NOSSO domínio, e o "conductor" próprio.
- **F4 — cluster M3**: Air = worker (SSH/Tailscale + frota própria); iPad = app mlx-swift (LoRAContainer troca adapter EM MEMÓRIA, sem reload; MLXEmbedders com o mesmo nomic → índice RAG portável); exo só se precisarmos de modelo que não cabe em 1 máquina (27B shardado). Imagem: Draw Things API + LoRAs do Civitai como nó do graph.

## Riscos vivos

- llama.cpp #21125 (conversor) e #16475 (Metal crash) — destravam vision+LoRA quando resolvidos; monitorar release notes.
- Swap MLX ~1,2s — se virar gargalo com fila paralela, wrapper Swift (LoRAContainer) elimina.
- Cards da comunidade mentem/faltam — todo adapter novo passa pelo gate antes de virar rota default.
- `.fmadapter` amarrado à versão do OS — churn de retreino se adotarmos T0 com adapter.

## Métricas de sucesso

1. % de tarefas da fila resolvidas local (sem T3) — subir sem derrubar score.
2. Placar por adapter no ledger vs base puro (exige F2.5).
3. Custo USD/tarefa e tokens Fable/tarefa — cair com o tempo.
4. Tempo de fila — swap-afinidade funcionando = não degradar com frota maior.

Dossiê técnico: `docs/RESEARCH-lora-fleet.md`. Decisões de arquitetura: architect 2026-08-04 (C disciplinado).
