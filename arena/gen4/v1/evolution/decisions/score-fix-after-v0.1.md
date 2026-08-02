# Fix do juiz — `score.py --ab` · pós-v0.1

**Contexto:** [`decisions/v0.1.md`](v0.1.md) registrou DISCARD por decisão humana
enquanto `score.py --ab` votava MERGE. Divergência entre juiz e realidade é a
dívida mais cara possível — um harness que evolui por score confia no score.

## O que estava errado

**1. Comparava somas entre amostras de tamanho diferente.** v0 tinha 13 runs,
v0.1 tinha 9. O gate leu `$0.7871 → $0.4249` como "custo caiu 46%". Não caiu
nada: rodei 4 vezes menos. Normalizado, era $0.0605 → $0.0607 — flat.
Consequência: **qualquer candidata que rodasse menos vezes passava no gate de
custo.** O juiz premiava amostra pequena.

**2. Runs sem telemetria entravam no agregado de custo.** As 2 runs truncadas
pelo teto de turns saíram com `cli_exit_1` e reportaram $0 / 0 tokens. Somadas,
puxavam a média de B para baixo e inventavam eficiência a partir de uma falha.
Falha barateando o custo é o incentivo exatamente invertido.

**3. Truncamento era invisível.** 2 de 9 runs (22%) terminaram cortadas e o
resumo mostrava só `9/9 = 100%`. O success sobreviveu porque `verify.py` checa
artefato e os artefatos já estavam prontos quando o corte veio — sorte de
timing, exibida como robustez.

**4. Bug de parsing (achado no caminho).** `load()` fazia `zip(header, cells)`
sem padding. A última linha do arquivo perde o `\t` final no `strip()`, então a
coluna `notes` sumia — justo da run mais recente, a que você está olhando.

## O que mudou em `score.py`

| Antes | Depois |
|---|---|
| soma de custo/tokens | **custo/run e tokens/run** (normalizado) |
| runs inválidas no agregado | excluídas do custo; contam em success e tempo |
| truncamento invisível | coluna `trunc N/M` + linha de taxa no `--ab` |
| N não reportado | `N total` e `N válido` dos dois lados + aviso de desbalanceio |
| 4 gates | 6 gates |
| `zip` truncando `notes` | padding explícito das células |

Gates novos: `success limpo não caiu` (success só entre runs não truncadas),
`truncamento não aumentou`, `ganho normalizado >=10%` (mediana s OU custo/run),
`sem regressão grave no outro eixo (<+10%)`.

## Output antes vs depois

**Antes** (juiz errado):

```
A v0     13/13 = 100%  med 23.9s  26707tok  $0.7871  eff 16.5
B v0.1    9/9  = 100%  med 25.1s  13982tok  $0.4249  eff 21.2
  [PASS] custo sob controle    [PASS] ganho real (rate subiu ou custo caiu)
=> MERGE candidato
```

**Depois** (juiz corrigido):

```
A v0     13/13 = 100%  med 23.9s  2054tok/run  $0.0605/run  trunc 0
B v0.1    9/9  = 100%  med 25.1s  1997tok/run  $0.0607/run  trunc 2/9
  mediana s   23.9 -> 25.1   +5.0%
  custo/run   $0.0605 -> $0.0607   +0.3%
  truncamento 0% -> 22%
  [FAIL] truncamento não aumentou
  [FAIL] ganho normalizado >=10% (mediana s OU custo/run)
=> DISCARD
```

## Confirmação: juiz alinhado

`python3 score.py --ab v0 v0.1` → **DISCARD**, exit 1. Bate com a decisão humana
da v0.1, pelos motivos certos: sem ganho normalizado, e truncamento subiu.

O juiz também foi testado **nas duas direções**, pela mesma regra que vale para
`verify.py` — um juiz que só sabe reprovar não mede nada:

| Cenário sintético | Esperado | Obtido |
|---|---|---|
| B 25% mais rápido, 30% mais barato, N igual | MERGE | MERGE (exit 0) |
| B com N menor (5 vs 13), métricas/run idênticas | DISCARD | DISCARD (exit 1) + aviso |
| dados reais v0 vs v0.1 | DISCARD | DISCARD (exit 1) |

O segundo cenário é a regressão direta da dívida: com o juiz antigo, aquelas
somas teriam lido "−62% de custo" e aprovado uma candidata que não mudou nada.

## O que aprendemos

O `verify.py` sempre teve a regra de ser testado nas duas direções; o juiz não
tinha. **Testar o avaliador é tão obrigatório quanto testar o verificador** — a
diferença é que um verificador quebrado erra uma task, e um juiz quebrado erra
a direção da evolução inteira. Nenhum gate deste harness deve rodar sem um caso
sintético que o faça votar dos dois lados.
