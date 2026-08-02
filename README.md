# harness-core

Harness autônomo mínimo (stdlib, sem framework de agente) que executa tasks
com um LLM, verifica o resultado de forma **determinística** — nunca confia
no que o agente diz — e usa esse verify como sinal pra se auto-melhorar via
gate A/B. Em cima disso existe uma **camada de juízes**: projetos derivados de
código open source real, com o teste do próprio mantenedor como gabarito
selado, pra medir se o harness resolve problema de terceiro — não só as
tasks que ele mesmo aprendeu a passar.

**Done = `verify.py`/teste do mantenedor sai 0.** O que o agent diz não conta.

## A prova (números reais, citados)

- **Bug real corrigido em código de terceiro:** o harness consertou um bug do
  [schwifty](https://github.com/mdomke/schwifty) (biblioteca IBAN/BIC real),
  com os **415 testes do upstream verdes** e 0 regressões
  (`results.tsv`, `v0.4 judge task_j_b2b success=1`; commit `8e145d9`).
- **Evolução medida, não sentida:** `v0.2 → v0.4` no juiz `j_b2b` foi de nota
  **58 → 81** (`STATUS.md`), passando por três decisões A/B reais e
  documentadas: `v0.1` MAX_TURNS 12→6 — **DISCARD** (piorou tempo e custo,
  `evolution/decisions/v0.1.md`), `v0.2` prompt enxuto — **KEEP** (−17.8%
  custo/run, `evolution/decisions/v0.2.md`), `v0.4` MAX_TURNS 12→30 — **KEEP**
  (0/6 sucesso com 12 turnos vs 3/4 com 30, `evolution/decisions/v0.4.md`).
- **Generalidade (M2):** rodando a melhor versão nos 3 domínios-juiz —
  `j_b2b` (Python/schwifty), `j_web` (JS/nanostores), `j_hw` (C/jsmn) — os três
  fecharam com `success=1` no `results.tsv`. A nota de persona do `j_b2b`
  nessa mesma rodada foi zerada por **veto de citação inválida**
  (`judges/verdicts/j_b2b/v0.4.json`, `judges/verdicts/summary_v0.4.json:
  "inconclusive": true`) — o mecanismo de veto funcionou, mas expôs uma
  instabilidade da persona que ainda não foi investigada (ver dívidas).
- **Defesa contra trapaça pegando em produção, não em teste:** no mesmo A/B
  do `v0.4`, um run tentou editar o arquivo de teste pra passar mais fácil e
  foi barrado por `tamper:test_file_modified` (`evolution/decisions/v0.4.md`).
- **87 testes automatizados** cobrindo agent, evolve, safety/proveniência,
  juízes, trace, delivery, UI gate e outbound gate (`tests/*.py`), sem gastar
  API.

## Como rodar

Requisitos: `python3` (stdlib só, `pytest` só dentro dos ambientes dos
juízes), [`claude` CLI](https://github.com/anthropics/claude-code) autenticado
(backend default), `git`.

```bash
# uma task da suite de lab
python3 run_task.py tasks/task_01

# a suite inteira, com repetição pra tirar ruído
python3 run_task.py --all --repeat 3

# um juiz: constrói o projeto-alvo real, roda o agente, verifica contra o
# gabarito selado do mantenedor
python3 judges/run_judge.py --judge j_b2b
python3 judges/run_judge.py --all-judges

# um ciclo de auto-evolução: proposta -> A/B -> gate -> merge|discard
cp evolution/proposals/_template.md evolution/proposals/minha_ideia.md
$EDITOR evolution/proposals/minha_ideia.md
python3 evolve.py --proposal evolution/proposals/minha_ideia.md --repeat 3
```

`evolve.py` sai **0 = merge** (genoma promovido), **1 = discard** (baseline
intacto), **2 = erro de infra** (não é veredito). `run_task.py --all` aceita
`--suite fixed|sealed`; a suite `judge` roda por `judges/run_judge.py`, que já
orquestra setup → run_task → verify selado → persona.

## Arquitetura (~15 linhas)

```
agent.py         # o loop do agente: prompt, tools, backend cli/api, trace
run_task.py       # workspace efêmero -> fixtures -> agent -> verify -> results.tsv
score.py          # gates normalizados por run (não por soma) + --ab A vs B
evolve.py         # 1 ciclo: proposta -> sandbox -> fixed -> sealed -> decisão -> merge/discard
graph.py          # store de auto-crítica: proposals, runs, decisions, sessions, governança
safety.py         # guard_path (realpath) + safe_run (allowlist de binário) — código, não prompt

judges/           # camada de juízes: registry.tsv (upstream+sha), _sealed/ (gabarito do mantenedor),
                   #   run_judge.py, persona.py (opus, citação obrigatória, veto sem citação sustentada)
benchmarks/
  fixed  (tasks/)  # hill-climb: "melhorou?" — precisa de ganho >= piso
  sealed            # held-out: "generaliza?" — precisa só do piso
  judge             # código de terceiro real: "resolve problema que não escreveu?"
  tb                # tasks portáveis do Terminal-Bench 2.0
```

## Princípios de design

- **Mecanismo, não instrução.** Sandbox, tamper check, allowlist de safety e
  timeout são código que o agente não pode contornar escrevendo texto
  diferente — nunca uma frase no prompt pedindo pra "não trapacear".
- **Verify determinístico é o piso; persona é refinamento, nunca autoridade.**
  `score.py`/`verify.py` decidem sucesso/fracasso sem LLM. A persona (juízes)
  entra só depois, com peso menor, e citação obrigatória: sem citação o
  critério é descartado; citação que o log não sustenta é **veto**, zera a
  ficha.
- **Citação obrigatória contra ruído de avaliador LLM.** Todo score de
  persona aponta `arquivo:linha` ou `trace.jsonl:N` — sem isso não conta.
- **Braços contemporâneos, sempre.** A/B de custo/tempo comparado entre dias
  diferentes é inválido (variação de ruído de dia chegou a +23.7% em um caso
  real — `evolution/decisions/v0.3.md`); todo A/B roda A e B na mesma janela.
- **Tempo é o gargalo, imposto por mecanismo.** SIGTERM/teto de turnos, não
  instrução de "seja rápido" — o que sobra de folga vira margem real, não
  economia (lição do `v0.1.md`, que cortou turnos que nunca eram usados).

## Estado honesto

**Funciona, verificado por execução:** loop `evolve` com 3 decisões reais
(`v0.1` discard, `v0.2` e `v0.4` keep); suites `fixed`/`sealed`/`tb` rodando;
os 3 juízes (`j_b2b`/`j_web`/`j_hw`) produzindo `success=1` em código de
terceiro real; tamper check e safety allowlist pegando tentativa real de
trapaça; `harness_cli.py` como casca fina sobre tudo isso.

**Dívidas conhecidas:**

- Só testado a fundo com `claude-haiku-4-5` via backend `cli`; backend `api`
  e outros modelos nunca foram exercitados num A/B sério.
- A nota de persona do `j_b2b` na rodada de generalidade (M2) foi zerada por
  veto de citação inválida — o veto é a defesa funcionando, mas a causa raiz
  (por que a persona citou uma linha que o material não sustenta) não foi
  investigada.
- `D4` (custo/turnos até o verde) pune sucesso caro tanto quanto falha
  barata — defeito de régua já registrado, correção prevista em `RUBRIC-J2`.
- Um run do gate manual do `v0.4` registrou `turns=1` num sucesso — possível
  anomalia de parsing do stream, não bloqueia, não investigada.

**FASE 2 (ainda não construída):** juízes `j_web`/`j_hw` com peso pleno
(P3/P4 — recuperação de erro e adoção), tabela `judgements` no graph,
`judge_ok` automático no gate do `evolve.py`, segundo modelo/backend no A/B.

## Satélites congelados

`whatsapp.py`, `channel/`, `delivery.py`, `assist.py` e o gate de UI
(Playwright) existem, têm teste e já foram usados — mas estão **congelados**
até o core provar valor em código de terceiro (decisão registrada em
`STATUS.md`). Não é dívida técnica: é ordem de prioridade.

## História

`arena/` é o método generativo que produziu os mecanismos deste harness —
gerações de candidatos competindo sob briefing mínimo e julgamento por
persona, antes de qualquer linha aqui existir. Está preservada, commitada,
não roda mais: quando a camada de juízes provar que mede honesto sobre
código de terceiro, o método volta pra gerar a próxima geração de candidatos
sobre esta base (ver `STATUS.md`, "Método generativo v2").
