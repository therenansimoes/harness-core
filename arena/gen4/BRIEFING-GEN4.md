# BRIEFING GEN4 — mutação auto-dirigida

Você recebe uma cópia completa do harness-core atual (o repo que a arena constrói,
não um projeto de exemplo). Outros três construtores recebem a mesma base, em
paralelo, isolados de você.

## Goal

Melhore este harness no que **você diagnosticar** como maior alavanca. Ninguém
vai te dizer o que atacar — isso é o ponto: quando eu dito o alvo, quem melhora
sou eu, não você.

Declare no `NOTES.md`, **ANTES de codar**:

1. Seu diagnóstico ranqueado (top 4 oportunidades, na sua leitura).
2. Qual rank você ataca — **v1 ataca o SEU #1, v2 o SEU #2, v3 o SEU #3, v4 o
   SEU #4** (o número que cabe a você está no seu `INHERITED-vN.md`).
3. O ganho previsto: uma hipótese falsificável ("se eu fizer X, Y deve melhorar
   de A para B, medido por Z").

## Regras

- **Não edite testes existentes para fazê-los passar.** Testes novos que você
  escrever são bem-vindos.
- **Não toque em `judges/_sealed/`.**
- **`python3 -m pytest tests/ -q` tem que terminar verde** quando você parar.
- **Entregue `run.sh`** que demonstra a melhoria de verdade — não só a base
  rodando intacta.
- **Não invente resultado.** Alegar verde sem ter executado zera a nota. Vale
  para qualquer artefato publicado (README, NOTES, docstring) — a evidência
  citada precisa existir no log.

## Dicas

- Você pode usar subagentes (a tool `Task` está liberada) se isso te der mais
  julgamento por segundo de relógio.
- O relógio é o gargalo, não a permissão: você tem `Read,Write,Edit,Bash,Glob,
  Grep,WebSearch,WebFetch,Task`.
