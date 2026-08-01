# Delivery report — demo_site / s001

**Gerado:** 2026-08-01T18:09:38+00:00 · **delivery_success:** 0
**next_action:** `continue_delivery` — Falta trabalho de ENTREGA — os checks dizem o quê.

Este é o eixo de ENTREGA. Não se mistura com o score de laboratório do harness
(`results.tsv`): aqui a pergunta é se o trabalho serviu, não se o motor melhorou.

## Regression (3/3) — não pode regredir

| | check | motivo |
|---|---|---|
| PASS | `check_css_linkado.py` | — |
| PASS | `check_html_bem_formado.py` | — |
| PASS | `check_index_existe.py` | — |

## Acceptance (0/2) — a delta desta sessão

| | check | motivo |
|---|---|---|
| FAIL | `check_precos_existe.py` | site/precos.html não existe |
| FAIL | `check_precos_tem_planos.py` | site/precos.html não existe |

## Governança

Nenhuma violação.

## Alinhamento ao brief

- [ ] `site/precos.html` existe e é HTML válido
- [ ] três blocos de plano com `class="plano"`
- [ ] título `<h1>` na página
- [ ] link da home para a página de preços

## UI

Projeto marcado como UI. **needs_human_ui_review** — a rubrica abaixo não é automatizável ainda:

- [ ] hierarquia visual clara (o olho sabe onde olhar primeiro)
- [ ] contraste legível em texto corrido
- [ ] espaçamento consistente entre blocos
- [ ] usável em tela estreita (~375px)
- [ ] estados interativos visíveis (hover/focus)


