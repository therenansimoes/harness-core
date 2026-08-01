# harness-core

Motor de evolução com verificação. Plano em [`PLAN.md`](PLAN.md), atalho em
[`FAST_START.md`](FAST_START.md).

**Done = `verify.py` sai 0.** O que o agent diz não conta.

## Rodar

```bash
python3 run_task.py tasks/task_01        # uma task, uma vez
python3 run_task.py --all                # a suite fixed inteira
python3 run_task.py --all --repeat 3     # baseline com ruído
python3 run_task.py --all --suite sealed # só para creditar generalização
python3 run_task.py tasks/task_02 --keep # não apaga o workspace (debug)
```

Cada run: workspace temporário → fixtures → agent → `verify.py` no workspace →
linha em `results.tsv` → workspace apagado.

## Backends

| Backend | Como | Custo | Quando |
|---------|------|-------|--------|
| `cli` (default) | subprocess `claude -p` | assinatura | dia a dia, budget baixo |
| `api` | SDK `anthropic` | tokens | A/B sério, tokens crus |

```bash
HARNESS_BACKEND=api HARNESS_MODEL=claude-sonnet-5 python3 run_task.py --all
```

Variáveis: `HARNESS_BACKEND`, `HARNESS_MODEL`, `HARNESS_TIMEOUT`.

O `cost_usd` do backend `cli` é o custo **nocional** que o CLI reporta — na
assinatura você não paga isso, mas serve como proxy comparável entre A e B.

## Onde mexer

`agent.py` é o harness inteiro. `SYSTEM_PROMPT`, `MAX_TURNS`, `ALLOWED_TOOLS`,
`MODEL` são o genoma. **Um A/B muda uma coisa só.**

## Tasks

| Task | O que exige | Verificador |
|------|-------------|-------------|
| `task_01` | README com seções obrigatórias | estrutura + blocos de código |
| `task_02` | script CSV → resumo | saída bate com golden recomputado |
| `task_03` | consertar bugs até os testes passarem | testes verdes + hash do teste intacto |

`task_03` tem anti-cheat: editar `test_estoque.py` muda o hash e invalida a run.

Toda task nova precisa de um `verify.py` que foi **testado nas duas direções** —
falha no estado errado, passa no estado certo. Verificador que só sabe passar
não mede nada.

## Núcleo self-evolutive

Um ciclo = proposta → sandbox → suite → juiz → decisão → merge|discard → graph.
O baseline **não é tocado** até todos os gates passarem.

```bash
cp evolution/proposals/_template.md evolution/proposals/minha_ideia.md
$EDITOR evolution/proposals/minha_ideia.md      # hipótese + [change] old/new
python3 evolve.py --proposal evolution/proposals/minha_ideia.md --repeat 3
```

Exit **0 = merge** (genome promovido, versão bumpada) · **1 = discard** (baseline
intacto) · **2 = erro de infra** (não é veredito).

Quem julga é o `score.py --ab`, os mesmos gates normalizados de sempre — o
`evolve.py` não tem score próprio. A decisão sai em `evolution/decisions/<id>.md`
e tudo fica ligado no graph (proposta → runs da candidata → decisão).

```bash
python3 graph_query.py decisions          # histórico + placar merge/discard
python3 graph_query.py runs v0.2          # runs de uma versão
python3 graph_query.py ab v0.2 v0.3       # dois lados no graph
python3 tests/test_evolve_paths.py        # 4 caminhos do ciclo, sem gastar API
```

### fixed vs sealed — quando um merge é "creditado"

| Suite | Onde | Pergunta que responde | Papel no gate |
|---|---|---|---|
| `tasks/` (**fixed**) | hill-climb | "melhorou?" | precisa de **ganho** ≥10% + piso |
| `benchmarks/sealed/` | held-out | "generaliza?" | precisa só do **piso** (success, truncamento) |

O `evolve.py` roda a fixed primeiro. Se os gates reprovam, acabou — sealed nem
roda, porque held-out gasto em candidata morta é budget queimado e, repetido,
vira treino disfarçado na própria held-out. Se a fixed aprova, a candidata vai
para a sealed e só é **creditada** se o piso se mantiver lá.

- fixed aprova + sealed confirma → **MERGE creditado**
- fixed aprova + sealed reprova → **DISCARD** (ganho que não generaliza é overfit)
- fixed aprova + sealed vazia ou `--no-credit` → **MERGE sem crédito**, e a
  decision diz isso com todas as letras

## WhatsApp assist — nunca envia sem confirmação

Canal de assistência **ao dono**, não atendimento a terceiros. A regra é de
código, não de disciplina:

> `whatsapp.confirm_send()` é a única função do repo que chama o transporte.

Qualquer outro caminho no máximo cria um `pending` no graph — um pedido de
permissão, não uma mensagem. Quatro camadas independentes seguram: allowlist ao
criar, allowlist ao confirmar, máquina de estados no SQLite (só `confirmed` vira
`sent`) e o serviço Node, que revalida a allowlist e só escuta em `127.0.0.1`.

```bash
# 1. configure (fora do git)
mkdir -p ~/.config/harness-core && python3 -c "import config; print(config.example_toml())" \
  > ~/.config/harness-core/config.toml && $EDITOR ~/.config/harness-core/config.toml

# 2. suba o serviço e pareie lendo o QR uma única vez
cd channel/whatsapp && npm install && WA_ALLOWLIST="55SEUNUMERO@s.whatsapp.net" npm start

# 3. assist: lê o inbox, obedece só o dono
python3 assist.py --watch
```

Fluxo de um envio, de ponta a ponta:

```bash
python3 harness_cli.py whatsapp-pending      # o que está esperando permissão
python3 harness_cli.py whatsapp-confirm 3    # ÚNICO caminho que envia
python3 harness_cli.py whatsapp-cancel 3     # nada sai
python3 tests/test_outbound_gate.py          # 7 provas de que o gate segura
```

Pelo próprio WhatsApp o dono manda `status`, `pendentes`, `confirmar <id>`,
`cancelar <id>`, `decision`. Mensagem de qualquer outro número é ignorada, grupo
nunca comanda nada, e **até a resposta ao dono fica pendente** — a menos que
você ligue `allow_auto_reply_to_owner` (default `false`).

Não existe comando "enviar". Se você procurar um atalho que cria e já confirma,
não vai achar: a ausência dele é a funcionalidade.

## Projeto real: spec → session → verify em camadas → post-work → resume

Um `verify.py` congelado no dia 1 não escala para um site que cresce. Aqui a
verificação tem camadas, e o critério é versionado como qualquer outro artefato.

```
projects/demo_site/
├── spec/SPEC.md              # spec VIVA (front matter com version/updated/ui)
├── spec/CHANGELOG_SPEC.md    # o que mudou no critério, e quando
├── regression/               # invariantes: só CRESCEM, protegidos por MANIFEST.json
├── acceptance/<session>/     # o aceite da DELTA desta sessão
└── sessions/<session>/       # brief.md · state.json · delivery_report.md
```

```bash
python3 harness_cli.py project-init meu_site --ui
python3 harness_cli.py session-new --project meu_site --session s001
python3 harness_cli.py verify     --project meu_site --session s001   # exit 1 se falta algo
python3 harness_cli.py post-work  --project meu_site --session s001
python3 harness_cli.py resume     --project meu_site --session s001
python3 harness_cli.py promote-checks --project meu_site --session s001  # aceite vira regression
python3 tests/test_delivery.py    # 11 provas, sem API
```

**Dois eixos de score, tabelas separadas.** `results.tsv` + `runs` respondem "o
motor melhorou?". `delivery_events` responde "ficou bom pro Renan?". Um
`task_01` passando no lab nunca conta como entrega, e vice-versa.

**Governança.** O worker pode ADICIONAR check de regression (só fortalece a
barra), mas apagar ou editar um existente é **violação** — o `MANIFEST.json`
guarda o sha256 de cada um, e a verificação falha mesmo que todo o resto esteja
verde. Senão o caminho mais fácil para ficar verde seria apagar o check. Só
`governance-approve` reescreve o manifest, e ele é do dono:

```bash
python3 harness_cli.py governance-approve --project meu_site --note "removi o check X porque..."
```

O worker também não altera gates de `score.py`. Quando o post-work detecta o
mesmo check falhando em sessões diferentes, ele abre um **stub de proposta** em
`evolution/proposals/` e para por aí — mudar o critério de avaliação para ficar
verde é exatamente o que este harness existe para impedir.

**`next_action`** sai do post-work e é o que a próxima sessão lê:

| valor | significa |
|---|---|
| `continue_delivery` | falta trabalho de entrega; os checks dizem o quê |
| `await_renan` | decisão humana: UI, item do brief ou governança |
| `evolve_harness` | falha recorrente — o gargalo parece ser o motor |
| `done` | tudo verde, nada pendente |

### UI: gate automático com Playwright

Terceira camada de verify. `ui = true` **não** significa mais "espera o Renan":
significa "roda a suite de UI". O humano entra só onde a máquina não conclui.

```bash
python3 harness_cli.py ui-test     --project demo_site   # roda a suite
python3 harness_cli.py ui-baseline --project demo_site --note "novo header aprovado"
python3 tests/test_ui_gate.py                            # 5 provas do gate
```

Os checks vivem em `projects/<nome>/ui/tests/*.spec.mjs`, as baselines em
`ui/baselines/` (versionadas). O que roda hoje no demo: home 200 + estrutura
mínima, CSS de fato aplicado no browser (não só o `<link>` no HTML), links
internos sem 404, console sem erro, screenshot desktop vs baseline, e ausência
de scroll horizontal em 375px — mais screenshot mobile.

Diff de screenshot usa `maxDiffPixelRatio: 0.02`. Pixel-perfect é frágil
(antialiasing, versão do Chrome, fonte); 2% pega quebra real de layout sem
alarme falso a cada patch do browser.

`needs_human_ui_review` agora só acontece em quatro casos:

| gatilho | por quê |
|---|---|
| suite de UI falhou | ambíguo: bug real ou baseline desatualizada? a máquina não distingue |
| `review_subjective = true` (ou `REVIEW_UI_SUBJECTIVE=1`) | você pediu rubrica semântica |
| check `manual_ui*` na acceptance | alguém marcou explicitamente |
| `ui = true` mas a suite não roda | UI declarada e não verificável falha fechado |

Com tudo verde, um projeto de UI chega a **`done` sem passar pelo Renan** — que
era o objetivo desta camada.

**`ui-baseline` é sensível e por isso fica registrado na governança:** a baseline
nova vira a verdade. Se ela for gravada com um bug visual na tela, o bug passa a
ser o esperado e nenhum check reclama de novo.

Config por projeto em `.harness/config.toml`:

```toml
[ui]
enabled = true
review_subjective = false   # true = sempre pede olho humano
review_on_failure = true    # false = falha de UI vira continue_delivery, não await_renan
```

**Browser:** a config usa `channel: 'chrome'` — o Chrome do sistema, sem baixar
bundle. O runner (`@playwright/test`) está instalado na raiz do repo, e o Node
resolve subindo a árvore: **projeto fora de `harness-core/` precisa do próprio
`npm i -D @playwright/test`**, senão a suite falha com `ERR_MODULE_NOT_FOUND` e
o post-work cai em `needs_human_ui_review` por não conseguir verificar.

## CLI

```bash
python3 harness_cli.py status                # versão, score, última decision, pendentes
python3 harness_cli.py run --all             # roda a suite de lab
python3 harness_cli.py evolve --proposal ... # um ciclo de evolução do motor
python3 harness_cli.py init --path ~/projeto # cria .harness/ com pin de versão
python3 graph_query.py sessions              # sessões de entrega
python3 graph_query.py delivery demo_site    # histórico de entrega do projeto
python3 graph_query.py governance            # quem aprovou o quê
```
