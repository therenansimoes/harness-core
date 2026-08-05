# PROGRESS — o que falta, quem faz, quanto tempo

**Atualizado:** 2026-08-05 (tarde). Regra deste arquivo: máx 1 página, itens com
dono e tamanho. Detalhe técnico vive em STATUS.md / docs/ROADMAP.md, não aqui.

## 🎯 ENTREGA REAL FEITA HOJE (a primeira)

O harness entregou o **site da bancada inteiro** com Claude real (backend
`claude_code`, assinatura, sem API key): 5/5 unidades aceitas e integradas no
master de `~/projects/bancada-app` — estrutura, 22+ componentes em JSON, tabela
filtrável, calculadora de divisor de tensão, tema claro/escuro. Verify verde
nas 5, regressão verde, **custo total $3.63** (cap $5/run). Abra
`bancada-app/dist/index.html` no navegador pra ver.

Também entrou: **fallback off-grid** (Anthropic primário; caiu → degrada pro
modelo local com carimbo no ledger; genoma intocado) e o `decompose` aprendeu a
convenção `dist/` que causou a falha do dia.

## Fila do Renan (decisões de 1 linha, quando quiser)

1. As 5 propostas mineradas do tune seguem esperando selo (item antigo, sem
   pressa; frente meta segue congelada).

~~Fallback no grafo~~ e ~~fix do replan~~: autorizados verbalmente e FEITOS
(commits `e6b95a8` e `6133efe`, 2026-08-05).

## Fila do harness (pequena, sem decisão nova)

1. `ui-verify.png` vaza pro commit de entrega (`deliver()` só exclui
   `.harness`) — excluir artefatos do gate.
2. Piso de screenshot 20.0kb reprovou página real por 0.1kb — revisar margem.
3. Limpar worktree órfã em `data/ws/` e branches `harness/*` antigas no
   bancada-app.
4. Refresh do STATUS.md (números de 2026-08-04 desatualizados).

## Feito recentemente (não reabrir)

- **2026-08-05 tarde:** primeira entrega real fim-a-fim (bancada 5/5, $3.63);
  fallback off-grid implementado+testado; prompts das unidades corrigidos
  (convenção dist/ como raiz); decompose ensinado na fonte.
- Modo leigo completo; tune loop D1-D5; suíte verde; primeiro rewrite retido.
