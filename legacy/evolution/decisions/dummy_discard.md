# Decisão dummy_discard — DISCARD

**proposal_id:** `dummy_discard` · **graph_decision_id:** `2`
**Proposta:** [`dummy_discard.md`](../proposals/dummy_discard.md)
**Ciclo:** v0.2 → v0.2-dummy · 6 runs de candidata · gerado por `evolve.py` em 2026-08-01T16:57:42+00:00

**Hipótese:** Inflar o SYSTEM_PROMPT com instruções redundantes NÃO melhora nada e deve ser reprovado pelos gates — esta proposta existe para exercitar o caminho DISCARD do evolve.py de ponta a ponta.

**Mudança:** agent.py: -1 linhas / +10 linhas

## A/B

| Métrica | A = v0.2 | B = v0.2-dummy | Δ |
|---|---|---|---|
| success | 20/20 = 100% | 12/12 = 100% | — |
| success limpo | 100% | 100% | — |
| truncamento | 0% | 0% | — |
| mediana s | 20.7s | 26.9s | +30.0% |
| custo/run | $0.0479 | $0.0549 | +14.6% |
| tokens/run | 1757 | 2092 | +19.0% |
| N válido | 20 | 12 | — |

## Gates

| Veredito | Gate |
|---|---|
| PASS | success não caiu |
| PASS | success limpo não caiu |
| PASS | N válido suficiente (>=3 por lado) |
| PASS | truncamento não aumentou |
| FAIL | ganho normalizado >=10% (mediana s OU custo/run) |
| FAIL | sem regressão grave no outro eixo (<+10%) |

**AVISO:** amostra desbalanceada — rode N igual antes de creditar.

## Razão

Gate(s) reprovado(s): ganho normalizado >=10% (mediana s OU custo/run); sem regressão grave no outro eixo (<+10%). custo/run +14.6%, mediana +30.0%, truncamento 0% -> 0%. Baseline v0.2 permanece intacto.

## Notas da proposta

# Proposta dummy — bloat de prompt (deve DAR DISCARD)

## Por que

Esta proposta **não é uma tentativa de melhoria**. Ela existe para provar que o
caminho de DISCARD do `evolve.py` funciona sem depender de sorte estatística.

Um no-op puro (ex.: adicionar um comentário ao `agent.py`) seria o teste mais
limpo conceitualmente, mas o veredito dependeria de ruído: 6 runs de um genome
idêntico podem, por acaso, sair 10% mais baratas e disparar um MERGE espúrio.
Um bloat deliberado de prompt move o custo na direção **errada** de forma
previsível, então o DISCARD é determinístico.

## Predição

- custo/run e tokens/run **sobem** (o prompt inflado é reenviado a cada turn)
- success e truncamento ficam flat (o texto é inofensivo, só caro)
- gate `ganho normalizado >=10%` **reprova** → DISCARD, exit 1
- `agent.py` e `harness_version.txt` continuam em v0.2

## Falsificação

Se isto der MERGE, os gates estão frouxos — custo subindo não pode ser aprovado.
Se der erro de infra (exit 2), o problema está no `evolve.py`, não na proposta.

---

```bash
python3 evolve.py --proposal evolution/proposals/dummy_discard.md --repeat 2
```
