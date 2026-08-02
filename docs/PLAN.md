> SUPERSEDED 2026-08-02: ver STATUS.md (pivô LangGraph)

# Self-Improving Harness — PLAN.md

**Versão do documento:** 1.0  
**Status:** core-first, começo limpo, score no centro  
**Princípio:** o harness evolui a si mesmo com evidência — não com feeling.

---

## 0. Visão em uma frase

Um **core autônomo** que executa trabalho real, mede qualidade com score honesto, propõe mudanças em si mesmo, testa em A/B isolado e só promove o que generaliza — com satélites (browser, canais, vault, memória) entrando **depois**, de forma orgânica e versionada.

Não é chatbot. Não é Frankenstein de 10 libs. É um **motor de evolução com verificação**.

---

## 1. Princípio central

```
Core     = planeja + executa + verifica + registra score
Evolução = propor → sandbox A/B → gates → merge ou discard
Satélites = o core ganha o direito de adicionar quando o score exigir
```

### Regras absolutas

1. **Nenhuma tarefa é “done”** sem verificador determinístico (`verify` = 0).
2. **Nenhuma mudança no harness é “melhor”** sem comparação numérica no log de scores.
3. **Proposer ≠ juiz** — quem propõe patch não credita sozinho o merge.
4. **Train ≠ sealed** — evoluir e avaliar no mesmo conjunto é overfitting disfarçado.
5. **Core antes de satélite** — WhatsApp, browser, vault e memória rica não entram no dia 1.

### O que a literatura já mostrou (e nós respeitamos)

| Lição | Implicação no plano |
|-------|---------------------|
| Evolução de harness muitas vezes não bate test-time scaling com o mesmo budget | Toda evolução compete com baseline de scaling barato |
| Benchmarks mentem; agent pode “passar” sem resolver | Score baseado em **artefato**, não em “eu terminei” |
| Harness engorda (tokens, tools) e qualidade fica plana | Score inclui **custo** e complexidade, não só pass rate |
| Modelo ≠ harness | Sempre versionar e reportar `modelo + harness + critérios` |
| Separar propor de creditar | Gates determinísticos + held-out antes do merge |

Inspiração (não cópia): espírito AutoAgent (hill-climb em score), Self-Harness (regressão + held-out), AHE (observability de componente/experiência/decisão), GSME (qualidade-diversidade por tipo de falha).

---

## 2. O que NÃO fazer agora

- WhatsApp / falar com terceiros como assistente  
- Browser logado + senhas no agent  
- Graph temporal, Mem0, multi-agent swarm no bootstrap  
- Otimizar prompt “no feeling” sem `results.tsv`  
- Misturar harness antigo no path de execução  
- Instalar 5 frameworks open source “porque são bons”  
- Publicar plugin de marketplace para não-dev antes do score estável  

**Começa limpo.** Repo novo. Legado só como referência (`legacy/`), nunca como base.

---

## 3. Arquitetura em camadas

```
┌─────────────────────────────────────────────────────────┐
│  Camada 3 — Produto (não-dev)                           │
│  App / skill / instalador · botões · conectores         │
│  (só depois do core com score e A/B funcionando)        │
└─────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────┐
│  Camada 2 — Por projeto                                 │
│  .harness/ · config local · results do projeto · pin    │
└─────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────┐
│  Camada 1 — Core (fonte da verdade)                     │
│  agent · runner · score · evolution · benchmarks        │
│  Repo: harness-core                                     │
└─────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────┐
│  Config global mínima (opcional)                        │
│  ~/.config/harness-core/ · keys · defaults              │
│  NÃO misturar o core inteiro em ~/.claude               │
└─────────────────────────────────────────────────────────┘
```

| Camada | Onde | Quem | Quando |
|--------|------|------|--------|
| Core | `harness-core/` (Git) | Você | Agora |
| Global | `~/.config/harness-core/` | Você | Keys + defaults |
| Projeto | `.harness/` no workspace | Você / time técnico | Após baseline v0 |
| Produto | App / skill marketplace | Não-dev | Após A/B estável |

**Marketplace / plugin** = canal de distribuição da *casca*, não o lugar onde o motor nasce.

---

## 4. Stack mínima (dia 1)

| Peça | Escolha | Motivo |
|------|---------|--------|
| IA | Claude API (ou o modelo que você já paga) | Estável em código |
| Harness | `agent.py` single-file (bash + system prompt) | Editável, auditável |
| Runner | `run_task.py` | Agent → verify → log |
| Sandbox | Pasta isolada por run (Docker depois) | Não contamina o host |
| Score | `results.tsv` | Histórico comparável |
| Tasks | 3 tarefas **suas** + `verify.py` | Score honesto |
| Versão | `harness_version.txt` | Pin e A/B |

Depois (não no bootstrap): CLI `harness`, plugins opcionais, vault, browser-use, OpenClaw-like channels.

---

## 5. Estrutura de pastas (alvo)

```
harness-core/
├── PLAN.md
├── README.md
├── harness_version.txt          # ex: v0
├── agent.py                     # harness editável (v0)
├── run_task.py                  # orquestra uma task
├── results.tsv                  # log append-only de runs
├── evolution/                   # (etapa 4+)
│   ├── proposals/
│   ├── sandboxes/
│   └── decisions/
├── benchmarks/
│   ├── fixed/                   # suite estável para hill-climb
│   └── sealed/                  # só para creditar generalização
├── tasks/
│   ├── task_01/
│   │   ├── prompt.md
│   │   ├── verify.py            # exit 0 = pass
│   │   └── expected/            # opcional
│   ├── task_02/
│   └── task_03/
└── legacy/                      # opcional: ideias do harness antigo (não importar)
```

Por projeto (depois de `harness init`):

```
meu-projeto/
└── .harness/
    ├── harness_version          # pin do core
    ├── config.toml              # overrides
    └── results/                 # scores deste projeto
```

---

## 6. Score — o coração do sistema

### 6.1 Score mínimo por run (`results.tsv`)

| Campo | Significado |
|-------|-------------|
| `timestamp` | ISO time |
| `harness_version` | v0, v0.1… |
| `task_id` | task_01… |
| `success` | 0 \| 1 — **só** o `verify.py` define |
| `seconds` | wall time |
| `tokens` | se a API reportar |
| `notes` | erro, timeout, etc. |

### 6.2 Vetor completo (evoluir para isto)

| Dimensão | O que mede | Como |
|----------|------------|------|
| **Success** | Objetivo atingido | `verify` determinístico |
| **Critical failure** | Resultado inaceitável | fail hard; bloqueia merge |
| **Artifact check** | Feito ≠ dito | arquivos, testes, schema no workspace |
| **Cost** | Tokens, tool calls, tempo | efficiency = success / cost |
| **Human intervention** | “Refaça / lembra da regra” | contador no log |
| **Process / maturity** | Verificou antes de submeter? | separado do success final |
| **Regression** | Held-out não piorou | suite sealed |
| **Stability** | pass@k vs pass^k | reliability > ceiling |

### 6.3 Regras de promoção A/B (mudança no harness)

1. Mesmas tasks, mesmo budget, preferir mesmas seeds  
2. Critérios críticos (success, critical failure) **não podem cair**  
3. Custo pode piorar pouco se success sobe de forma clara  
4. Merge só com evidência em **fixed** + confirmação em **sealed**  
5. Preferir margem mínima + N runs a “subiu 1 ponto uma vez”  
6. Toda evolução compete com **test-time scaling** no mesmo budget  

### 6.4 Quality-diversity (depois)

Arquivo de patches por **tipo de falha** (onde × por quê), não só por pass rate médio — evita overfitting a um único estilo de task.

---

## 7. Loop de evolução (quando chegar a hora)

```
1. Baseline A = harness atual + scores históricos
2. Proposta B (diff + hipótese + critérios esperados)
3. Sandbox isolada com B
4. Roda suite fixed em A e B
5. Gates: validity · activation · no-regression · cost bound
6. Roda sealed (B não “treinou” nela)
7. Decision log: merge | discard | motivo | scores
8. Se merge → bump harness_version
```

**Observability mínima (espírito AHE):**

- **Componente:** o que mudou no harness (arquivo/diff)  
- **Experiência:** traces e falhas legíveis  
- **Decisão:** predição do propositor vs resultado real (contrato falsificável)

---

## 8. Etapas (uma de cada vez)

### Etapa 0 — Repo limpo
- Criar `harness-core`  
- Só este `PLAN.md` no início  
- Zero import do harness antigo  

### Etapa 1 — Primeira task fechada
- `agent.py` mínimo (prompt + bash + limite de turns)  
- `tasks/task_01/{prompt.md, verify.py}`  
- Uma run manual → linha no `results.tsv`  

**Done da etapa:** 1 task com `success=1` no log.

### Etapa 2 — Suite de 3 + runner
- task_02, task_03  
- `run_task.py` para as três  
- Cada task × 3 runs (ruído)  

**Done:** baseline v0 (taxa de sucesso + tempo médio).

### Etapa 3 — Primeiro A/B (humano no comando)
- Uma mudança só no `agent.py`  
- Bump → v0.1  
- Mesma suite · comparar TSV · keep ou discard  

**Done:** decisão por número, não por feeling.

### Etapa 4 — Evolução assistida
- Pasta `evolution/`  
- Proposals + sandbox + decision log  
- Fixed vs sealed  
- Baseline obrigatória de test-time scaling  

### Etapa 5 — CLI e multi-projeto
- `harness init` → cria `.harness/`  
- `harness run`  
- Pin de versão por projeto  
- Config global só para keys/defaults  

### Etapa 6 — Satélites (orgânicos)
- Memória, browser, canais, vault  
- Cada um = patch candidata com o mesmo regime de score  
- Usuário de negócio vê “conectores”, não libs  

### Etapa 7 — Produto para não-dev
- UI / skill / instalador  
- Zero terminal obrigatório  
- Marketplace só como canal, core continua no seu repo  

---

## 9. Como rodar (fase lab)

```bash
cd harness-core
python -m venv .venv && source .venv/bin/activate
pip install anthropic   # ou SDK do seu modelo
export ANTHROPIC_API_KEY=...

python run_task.py tasks/task_01
cat results.tsv
```

Cada run:

1. Workspace temporário isolado  
2. Fixtures da task (se houver)  
3. Agent com `prompt.md`  
4. `verify.py` **no workspace**  
5. Append em `results.tsv`  
6. Arquivar ou apagar workspace  

---

## 10. Setup nos workspaces

### Agora (você)
1. Código e evolução em `harness-core`  
2. Opcional: `~/.config/harness-core` para keys  
3. **Não** despejar o core em `~/.claude`  

### Depois (`harness init`)
```text
projeto/
  .harness/
    harness_version
    config.toml
    results/
```
Projeto **referencia** o core pinado; não copia o monstro.

### Muito depois (não-dev)
Instalar app/skill → ligar pasta → “Executar” → status simples.  
Por baixo: o mesmo core. Por cima: zero jargão.

---

## 11. Exemplos de tasks iniciais

Troque pelas suas — têm que ser **reais** e 100% verificáveis:

1. **README a partir de brief** — arquivo existe + seções obrigatórias  
2. **Script CSV → resumo** — roda sem erro + output = golden  
3. **Fix de bug em snippet** — testes passam  

---

## 12. Regras de ouro (checklist)

- [ ] Done = `verify == 0`, nunca “modelo disse que terminou”  
- [ ] Melhoria de harness = A/B no log, nunca feeling  
- [ ] Uma mudança por vez no A/B  
- [ ] Core antes de satélite  
- [ ] Verificador determinístico > LLM-as-judge  
- [ ] Train e sealed separados  
- [ ] Evolução vs test-time scaling no mesmo budget  
- [ ] Começo limpo — legado fora do path  
- [ ] Marketplace só depois do motor estável  

---

## 13. Próximo passo imediato

1. Criar pasta/repo `harness-core`  
2. Colocar este `PLAN.md`  
3. Escrever **task_01** (`prompt.md` + `verify.py`)  
4. Escrever `agent.py` mínimo  
5. Rodar uma vez → ver linha no `results.tsv`  

Quando estiver verde: esqueleto completo de `agent.py` + `run_task.py`, ou o primeiro A/B.

---

## 14. Norte de longo prazo

O “Renan fodástico” que planeja, resolve, traz segurança e é admirado de verdade materializa-se aqui:  
um sistema que **faz o trabalho**, **prova que fez**, e **melhora a si mesmo com evidência** — anônimo e útil no começo, produto sólido depois.

Score no centro. Core primeiro. Satélites depois. Usuário de negócio no final da fila de complexidade — nunca no começo da engenharia.
