# FEEDBACK-GEN4 — herança (decisão Fable, 2026-08-02)

**Resultado bruto:** 0/4 no milestone_gate ($9 de build, $0 de julgamento — gate escalonado pagou). Todos quebraram a suite verde que receberam. Dados completos em FEEDBACK-GEN4-dados.md.

## O que a geração comprou (vale mais que o código dela)

1. **Convergência 3/4 no mesmo #1, por caminhos independentes:** o gate de verde da UI não é hermético — `run_ui_suite` devolve `ran=False/0 testes` e quem consome lê como "sem falha = verde". Classe de bug: *vacuous pass*. Profecia do método confirmada ("se todos elegem o mesmo #1, o gargalo é óbvio e real").
2. **v2 falsificou a própria hipótese com números:** repeats intra-juiz N=3 + mediana só reduz flip de 41%→30%; o ganho real é `spread_intra > 25 ⇒ unstable/abstenção` (flip 5.9%, mantendo 43% conclusivas). Design pronto pra reimplementar limpo.
3. **v4 (outlier) nomeou a verdade estrutural:** o loop de auto-melhoria não tem GERADOR de mutações — proposta é escrita à mão; o "auto" hoje é humano. Vai pro roadmap como degrau próprio.
4. **Convergência secundária 4/4:** N=1 por juiz sobre task bimodal é ruído institucionalizado.

## Por que 0/4 (lição de mecanismo pra gen5)

Opus em 12min faz mudança ambiciosa e larga a suite vermelha. "Mantenha verde" era REGRA no briefing — pedido, não mecanismo. Gen5: o runner injeta o resultado do milestone_gate entre as fases (fase 1.5 roda o gate e o resume da fase 2 abre com o veredito) — verde-primeiro vira feedback estrutural, não instrução.

## Decisões

- Candidatos v1-v4: DESCARTADOS (código morto; diagnósticos herdados).
- Adotar via dev normal (não geração): fix da classe vacuous-pass (0 testes coletados = FALHA) e repeats intra-juiz com abstenção (design do v2).
- Anti-tamper prompt (experimento paralelo à geração): REJEITADO — 5/6 vs 5/6, diff 0.
- Gen5: mesma mutação auto-dirigida + gate intermediário entre fases. Só disparar após os dois fixes acima entrarem (senão a convergência re-diagnostica o que já sabemos).
