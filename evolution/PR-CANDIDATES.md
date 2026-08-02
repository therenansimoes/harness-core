# Candidatos a PR / contribuição — DeerFlow & LLM Space

Doutrina (Renan, 2026-08-02): **MCP resolve integração — usar, não reimplementar.** PR de core só para gancho pequeno com ganho grande e genérico (não feature de vendor). Ficar em skill/MCP/config. Esta lista é para ANOTAR e priorizar depois, não para construir tudo.

## Já publicado (conta therenansimoes)
- ✅ bytedance/deer-flow#4644 — test(skillscan): documenta false negatives instance-client. Aberto, aguardando triagem.
- ✅ bytedance/deer-flow#4645 — fix(tests): isola test_custom_agent do config.yaml local. Aberto, aguardando triagem.

## Pronto, não publicado
- 🟡 Fork llm-space `feat/custom-eval-methods` (`26a9912`) — eval methods plugáveis (Wilson/ternário/KPI), 569 testes verdes, painel populado. Plano: push no fork + ISSUE upstream (Evaluate pluggable methods) com link+screenshot. Aguarda decisão de publicar.
- 🟡 Fork deer-flow `feat/whatsapp-baileys-channel` — canal WhatsApp Baileys (EM VOO, builder). Fork-only, reusa sidecar service.mjs.

## Via MCP — SEM PR (config do usuário, usar já)
DeerFlow tem cliente MCP nativo com OAuth por servidor (refresh_token/client_credentials), schema em `backend/packages/harness/deerflow/config/extensions_config.py:64-96`, doc `backend/docs/MCP_SERVER.md`. Declarar em `extensions_config.json → mcpServers`.
- Google Workspace (Gmail + Drive + Calendar) — MCP server público + OAuth refresh_token. ~30 linhas JSON.
- Microsoft Graph (Outlook mail+calendar, OneDrive, Teams) — idem, client_credentials ou refresh_token.
- Risco conhecido: issue #3322 (MCP não isola credencial por usuário — relevante só multiusuário).

## Backlog de PR (anotado, priorizar depois)
| Prioridade | Item | Via | Tamanho | Valor | Risco |
|---|---|---|---|---|---|
| **TOPO** | **Form "Add MCP server" em Settings→Tools** — o backend JÁ tem CRUD completo (`backend/app/gateway/routers/mcp.py`: GET/PUT `update_mcp_configuration` aceita server novo + PATCH estado + atomic write). Falta SÓ a tela de criar no frontend; hoje o painel só liga/desliga o que já está no extensions_config.json. Dor real sentida pelo Renan 2026-08-02. | **frontend-only** (backend pronto) | médio (form + validação) | altíssimo: conserta gap visível, melhora produto = visibilidade | baixo (zero core, backend valida) |
| Alta | Skill pública `inbox-triage`/`email-digest` que orquestra tools MCP (autonomia: lê email → decide → age) | skill em skills/public/, molde newsletter-generation | ~100-150 linhas | narrativa de autonomia, área que eles aceitam | baixo |
| Média | Generalizar bridge de credencial do sandbox: `lark-cli` hardcoded (`sandbox/tools.py:1739`) → registry `{provider: cli}` | PR upstream de core (genérico, não vendor) | ~80-150 linhas + testes | destrava toda integration skill de 3º | médio (área sandbox; mitigar mantendo lark como 1º entry) |
| Baixa | Canal WhatsApp Cloud API oficial (pywa) — crava slot `whatsapp` upstream | PR upstream | ~400-600 linhas | slot + UI de conexão | médio (Renan escolheu Baileys-only por ora) |
| Baixa | `deerflow-merit`: gate de mérito (Wilson/KPI) como middleware+MCP; PR do gancho `skill_evolution.write_policy` | extensão + 1 PR pequeno | ver evolution/specs/DEERFLOW-MERIT.md | diferencial nosso | médio |

## Não investigado (buracos de pesquisa)
- Issues/PRs em CHINÊS (base majoritariamente CN) — demanda pode estar lá.
- Handshake real de MCP server Google/MS com o McpOAuthConfig deles (schema suporta, não testado).
