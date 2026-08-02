# LEADERBOARD — arena harness-core

Nota 0–100: 30 determinísticos (código mede, com veto) + 70 da banca (mediana de 4 personas, nota sem citação descartada).

## Acumulado

| # | variante | geração | nota | o que a fez pontuar |
|---|---|---|---|---|
| 1 | **gen1/v3** | 1 | **78.75** | única rejeição de gate real gravada no artefato, com rollback; muta o próprio código-fonte, não um JSON; verificador sob a invariante de safety |
| 2 | **gen1/v1** | 1 | **78.50** | melhor arquitetura; único gate hermético (baseline e candidato em cópias temporárias); policy plugável por protocolo de ação |
| 3 | gen1/v2 | 1 | 65.50 | maior cobertura de marcos por linha (212 linhas, zero deps); melhor trace; reset de fixture no medidor |
| 4 | gen1/v5 | 1 | 50.50 | motor real, mas fixture entregue já corrigida: verde sem trabalho. −6 por evidência citada que o trace não sustenta |
| 5 | gen1/v4 | 1 | 35.50 | sem loop, gate causalmente desconectado da proposta, teste passa com o artefato deletado. −8 por README contradizer o próprio NOTES |

**Empate técnico no topo:** v3 e v1 estão dentro do ruído (0.25 de diferença). Ambos viram pais — e o crossover entre eles é o operador óbvio: o gate hermético em tmpdir de v1 combinado com a auto-modificação de código com rollback provado de v3.

## Geração 1 — saúde da sonda

| | |
|---|---|
| dispersão | 35.5 → 78.75 (amplitude 43.25) — **a sonda discrimina** |
| prazo | 300s, **não pegou em ninguém**: todos saíram entre 78s e 143s com `exit_code 0` |
| custo construtores | $3.81 |
| violação de escopo | nenhuma (verificado contra snapshot do git) |
| vetos | nenhum; 2 penalidades por incoerência entre artefato e alegação |

## Defeitos do próprio processo (corrigidos para a gen 2)

- **Cegueira vazou:** `_blind/` usou symlink e `ls -la` revela o alvo. O juiz de Segurança reportou espontaneamente e declarou ter descartado a informação. Corrigido em `blind.sh` — cópia real, artefato read-only, rastro do runner removido.
- **Prazo subutilizado:** o briefing deixou "acabei" ser decisão do construtor. O desenho pede que ele seja *morto* trabalhando. Corrigido no briefing da gen 2.
- **Régua incompleta:** o veto v1 só previa `NOTES.md` contradizendo o artefato; um candidato mentiu no README com NOTES honesto. Régua v2 amplia para qualquer artefato publicado e torna determinística a checagem de que a evidência citada existe.
