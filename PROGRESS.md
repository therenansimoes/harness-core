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
2. `~/.harness/config/projects.toml` tem a duplicata antiga `bancada` +
   `bancada-app` (mesmo repo). Novas duplicatas estão bloqueadas; apagar a
   existente órfã o histórico dos 2 runs do `do` — unificar ou deixar?

~~Fallback no grafo~~ e ~~fix do replan~~: autorizados verbalmente e FEITOS
(commits `e6b95a8` e `6133efe`, 2026-08-05).

## Teste leigo fim-a-fim (2026-08-05, noite): PASSOU

Testador cego (só --help/FAST_START) rodou `harness do` duas vezes no
bancada-app: contador no rodapé E dashboard completo (gráfico SVG sem lib,
cartões, busca, tema) — **2/2 aceitas e integradas, $0.50 total, zero flag**.
Veredito: "leigo consegue". Também entrou: **marketplace de skills**
(`harness market sync|search|install|approve` — skill de fora nasce inerte em
pending/, ativa só com aprovação humana; lista de registries no genoma
imutável). Smoke real: 17 skills do repo da Anthropic.

## Fila do harness (fricções do teste leigo + pequenos)

1. ~~projeto duplicado silencioso~~ e ~~lixo de rascunho no master~~: FEITOS
   (do.py reusa registro por repo path; prompt do `do` proíbe arquivo de
   anotação — regras de entrega limpa).
2. `quickstart` imprime FALHA vermelho pra coisa que não bloqueia — virar aviso.
3. `do` fica 200s+ mudo — precisa feedback de progresso.
4. Régua fraca em site estático: `do` não expõe `--ui` (o `add` expõe).
5. `plano não` não explica critério nem cita `decompose`.
6. `ui-verify.png` vaza pro commit de entrega; piso 20kb na navalha.
7. Limpar worktree órfã em `data/ws/` e branches `harness/*` antigas.
8. Refresh do STATUS.md (números de 2026-08-04).

## Feito recentemente (não reabrir)

- **2026-08-05 tarde:** primeira entrega real fim-a-fim (bancada 5/5, $3.63);
  fallback off-grid implementado+testado; prompts das unidades corrigidos
  (convenção dist/ como raiz); decompose ensinado na fonte.
- Modo leigo completo; tune loop D1-D5; suíte verde; primeiro rewrite retido.
