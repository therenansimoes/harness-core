+++
# Identidade do ciclo. Vira o nome da decision e a chave no graph.
id = "v0.3-exemplo"

# Baseline de onde parte. Precisa bater com harness_version.txt (ou use --force).
from_version = "v0.2"
to_version = "v0.3"

# UMA hipótese, mensurável, escrita ANTES de rodar. Se não dá para falsificar
# com os números do results.tsv, não é hipótese — é torcida.
hypothesis = "Trocar X por Y corta custo/run >=10% sem derrubar success nem aumentar truncamento."

# A mudança no genome. 'old' precisa aparecer EXATAMENTE UMA VEZ no arquivo,
# senão evolve.py aborta com exit 2 em vez de adivinhar.
[change]
file = "agent.py"
old = '''
MAX_TURNS = 12
'''
new = '''
MAX_TURNS = 10
'''

# Documental: o juiz real são os gates de score.py, que valem para todo ciclo.
# Estes campos registram o que VOCÊ previu, para a decision confrontar depois.
[expected_gates]
must_improve = ["cost_run"]
must_not_worsen = ["success", "trunc_rate"]
+++

# Proposta: <título>

## Por que

Qual observação do `results.tsv` ou de uma decision anterior motivou isto.
Evolução que não parte de evidência é chute com mais passos.

## Predição

O que você espera nos números, com valor. Ex.: custo/run $0.0498 → ≤$0.0448.

## Falsificação

O que faria você desistir da ideia. Se não há resposta aqui, a proposta não é
testável e o ciclo vai produzir uma decision sem informação.

---

## Como rodar

```bash
python3 evolve.py --proposal evolution/proposals/<este-arquivo>.md --repeat 3
```

Exit 0 = merge · 1 = discard · 2 = erro de infra.

O `[change]` é aplicado numa sandbox em `evolution/sandboxes/<id>/`. O baseline
em `agent.py` **não é tocado** até os gates passarem.
