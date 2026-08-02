# generative-project — construção do harness por evolução em arena

Documento de continuidade. Registra a metodologia, o estado real e o próximo passo. Escrito no fim da sessão de 2026-08-01.

---

## 1. O conceito (não confundir as camadas)

**Os agentes que disparamos NÃO são o harness.** A metodologia generativa — gerações de construtores competindo com prazo, banca avaliando, feedback virando herança — é o **processo de construção** do harness. O harness é o artefato que sai disso.

Três camadas distintas:

| Camada | O que é |
|---|---|
| **Arena** | o processo: gerações, prazo duro, banca, leaderboard, herança. Scripts em `arena/` |
| **Candidatos** | 5 construtores por geração, cada um com uma variação genética, produzindo um harness |
| **Harness** | o artefato que evolui de geração em geração. Hoje: `arena/gen3/v*/` |

O objetivo final: harness open source capaz de trabalho autônomo e auto-melhoria — de si próprio e do projeto de quem o usa.

O plano de destino completo (arquitetura, rede de critérios, marcos) está em
`/Users/renansimoes/.claude/plans/majestic-hatching-feigenbaum.md`. Ele é o norte; a arena é o método.

---

## 2. Mecânica da arena

```
arena/
  BRIEFING.md          # o pedido — reescrito a cada geração conforme o aprendizado
  RUBRIC.md            # régua, congelada dentro da geração, versionada entre elas
  LEADERBOARD.md       # ranking acumulado
  run_variant.sh       # runner de fase única (gen 1 e 2) — obsoleto
  run_variant2.sh      # runner de DUAS FASES com checkpoint (gen 3+) — o bom
  launch_gen.sh        # dispara a geração em tmux
  measure.sh           # medição determinística, sem LLM
  blind.sh             # anonimiza para a banca (cópia real, não symlink)
  gen<N>/
    v1..v5/            # os candidatos
    _blind/A..E/       # cópias anonimizadas que a banca vê
    _blind_map.json    # mapa (NUNCA mostrar a juiz)
    _measured.md       # telemetria determinística
    scores.json        # notas consolidadas
    FEEDBACK.md        # herança para a geração seguinte
```

**Prazo duro por tmux + `timeout -s TERM`.** Cada construtor é um `claude -p` headless isolado, morto no relógio. Não é instrução, é SIGTERM. `result.json` traz `usage`, `total_cost_usd`, `num_turns` — custo e turnos entram na nota sem instrumentação extra.

**Runner de duas fases** (`run_variant2.sh`, 100s + 195s): fase 1 só garante `run.sh` + `NOTES.md`; extrai o `session_id` do JSON e **retoma a mesma conversa** (`claude --resume`) na fase 2 para endurecer. Existe porque na gen 2 pedir "escreva cedo" no prompt não funcionou — pedido não é mecanismo.

**Herança:** `INHERITED.md` por variante = parte comum (leaderboard + FEEDBACK íntegro + acesso de leitura ao código de todos os antecessores) + a variação genética individual.

---

## 3. Histórico das gerações

### Gen 1 — 5 construtores do zero, 300s
Notas: v3 **78.75** · v1 78.50 · v2 65.50 · v5 50.50 (−6) · v4 35.50 (−8). Dispersão 35.5→78.75: a sonda discriminou. Custo $3.81.

**Prazo não pegou em ninguém** — todos saíram sozinhos entre 78s e 143s dos 300s.

**Três patologias em 5 de 5:**
1. **O gate nunca reprova.** Quatro só tinham aceites no log; em três o candidato era *idêntico* ao baseline com `accepted: true`.
2. **O stub offline É o gabarito.** Constante hardcoded, lookup do fonte exato, ou leitura do comentário `# BUG: should be X` plantado pelo próprio autor.
3. **O critério de aceite mora no diretório gravável pelo agente.** Um juiz deletou o artefato do agente e o teste continuou verde.

**O que ninguém fez:** apontar o harness para código que não era dele. Todos criaram tarefa, bug e agente — circuito fechado. E zero reuso de open source: cinco reinventaram o sandbox com denylist de substring, o padrão sabidamente quebrado.

### Gen 2 — herança + briefing endurecido — **RODADA ANULADA**
Quatro de cinco morreram no SIGTERM **sem entregar `run.sh` nem `NOTES.md`** (obrigatórios, valendo 14 dos 30 pontos determinísticos). Trabalharam mais (turnos +50%, custo +60%, linhas +45%) e entregaram menos.

Causa: eu escrevi *"você deve ser morto trabalhando"* e *sugeri* empacotar cedo. Sugestão não constrange — o próprio princípio que o plano defende, violado por mim. Juízes foram parados no meio para não queimar custo.

**Salvado:** `gen2/v3` (o crossover dos dois primeiros da gen 1) foi o único completo e virou a base da gen 3.

### Gen 3 — base semeada + runner de duas fases — **CONSTRUÍDA, NÃO JULGADA**
Cada variante começou com `gen2/v3` instalado. Resultado do mecanismo: **5/5 entregaram `run.sh` + `NOTES.md`**, todos retomaram sessão, e **5/5 rodam limpo em cópia limpa** (exit 0, 59–69 linhas de saída). Só v2 foi morto pelo prazo (exit 124, 241s); os outros pararam sozinhos entre 92s e 173s.

Variações genéticas impostas por mim (v1 medição de custo real, v2 `--repo` de terceiro, v3 fechar o furo do `python3 -c`, v4 espaço de busca do proponente, v5 livre com reuso).

---

## 4. A base atual (o melhor harness da arena)

`arena/gen3/v*/`, derivado de `gen2/v3`. ~437 linhas Python, stdlib. Verificado por execução: roda, é idempotente (só o wall time varia).

O `run.sh` demonstra, na tela: fixture vermelha → ação do agente → veredito do verificador → decisão do gate. Inclui:
- **gate hermético** medindo baseline e candidato em cópias descartáveis, com **um accept e um reject reais** gravados em `evolve_log.jsonl` (`strict_improvement` vs `no_strict_improvement_ge_baseline`)
- fixture entregue quebrada, sem dica em comentário
- `safety.py`: allowlist de binário, symlink e path traversal bloqueados (comprovado na saída)
- **tamper check**: agente editar `test_*.py` falha o turno mesmo que o pytest passe

As três patologias da gen 1 estão fechadas nesta base.

**Buracos conhecidos, herdados:** `tokens: 0` e `cost_usd: 0.0` em todo turno (marco de peso 20 zerado); catálogo de mutação minúsculo; `python3 -c "os.system(...)"` passa pela allowlist (denunciado pelo próprio autor no NOTES); só funciona na própria bancada.

---

## 5. Régua

**v1:** 30 determinísticos (com veto) + 70 da banca (4 personas, mediana por critério, nota sem citação descartada).

**v2** (atual): o juiz **executa antes de ler** — prova o prato, não lê a receita. Determinístico repreçado: `run.sh` que demonstra trabalho 10 · idempotência 4 · **rejeição gravada** 6 · **fixture entregue quebrada** 4 · custo real no trace 3 · eficiência 3. Veto ampliado para qualquer artefato publicado (não só `NOTES.md`) e para evidência citada que o log não sustenta.

Marcos do briefing, por peso: loop 25 · verificação determinística 20 · trace/medição 20 · auto-melhoria com gate 15 · invariantes com mecanismo 10 · rodável por terceiro 10.

---

## 6. PRÓXIMO PASSO — v3 da régua: juízes-persona de uso real

**Decidido, não implementado.** Muda o que a banca é.

Hoje as personas são funções de auditoria (Arquiteto, Cético/QA, Produto, Segurança). Passam a ser **personas de uso real**, cada uma com o próprio projeto de teste:

- quem desenvolve **sites**
- quem desenvolve **plataforma B2B**
- quem desenvolve **hardware / firmware**
- (outras conforme fizer sentido)

Cada juiz **usa o harness candidato de ponta a ponta no projeto dele** — aponta a ferramenta para código real do seu domínio e vê o que acontece. Não audita código: exercita como usuário.

**Todos pontuam pela MESMA lista de critérios**, independente do projeto de teste usado. É isso que torna as notas comparáveis e é isso que passa a medir **generalidade** — a coisa que a arena ainda não mede, porque até aqui todo candidato só foi exercitado na bancada que ele mesmo construiu.

### Também decidido para a próxima geração: mutação auto-dirigida

Em vez de eu ditar o foco de cada variante (quando eu dito, o proposer sou eu, e a arena mede execução das minhas ordens, não auto-melhoria):

1. Cada variante começa com o **melhor overall** e faz o **próprio diagnóstico**, montando a própria lista ranqueada de oportunidades.
2. **Diversidade por deslocamento de rank:** v1 ataca o #1 da lista *dela*, v2 o #2 da *dela*, v3 o #3, e assim por diante. Ninguém recebe alvo meu; a diversidade vem do mecanismo. Risco que isso resolve: na gen 1, cinco agentes sem coordenação convergiram para o mesmo desenho e as mesmas três patologias — livres sobre a mesma base, provavelmente 4 de 5 atacariam a mesma coisa.
3. **Hipótese falsificável:** cada variante declara no `NOTES.md`, antes de codar, o que escolheu, por quê e **qual ganho prevê**. A banca pontua se a previsão se confirmou. Variante que erra sistematicamente perde peso nas gerações seguintes.

A lista de cada uma também é dado: se as cinco elegem o mesmo #1, o gargalo é óbvio e real; se divergem, o espaço ainda é largo.

**Pendência:** gen 3 foi construída com foco ditado por mim e pode servir de **controle** — comparar com uma geração auto-dirigida mede se a minha direção agrega ou se é overhead.

---

## 7. Lições de método (custaram dinheiro, não repetir)

- **Pedido no prompt não é mecanismo.** Gen 2 inteira se perdeu nisso. Se algo é obrigatório, o runner impõe.
- **Sonda tem que discriminar.** Se as variantes empatam, quem falhou foi a tarefa. Registrar dispersão sempre.
- **Prazo é botão de calibragem**, não regra de justiça: curto separa por velocidade e escolha de caminho, longo por profundidade.
- **Cegueira precisa de mecanismo:** `_blind` com symlink vazou o alvo em `ls -la` (o juiz de Segurança reportou espontaneamente). Corrigido em `blind.sh` com cópia real + read-only.
- **A régua também está sob avaliação.** Toda geração registra os defeitos do próprio processo de julgamento.
- **Semear base validada acelera muito** — mas verificar a base por execução antes de semear é obrigatório.
- Não completar não penaliza; **mentir zera**. Isso está funcionando: duas penalidades aplicadas na gen 1 por incoerência entre artefato e alegação.

## 8. Risco aberto

Os construtores rodam com `--permission-mode bypassPermissions` na máquina do Renan, com `cwd` fixado no diretório da variante. É o risco **R1** do plano. A contenção hoje é detecção (snapshot do git antes/depois em `measure.sh`, zero violações até agora), não prevenção. Containerizar o runner é a primeira melhoria devida ao próprio processo.

## 9. Custo até aqui

Gen 1 $3.81 (construtores) + 4 juízes · Gen 2 ~$5.05 + 4 juízes parados no meio · Gen 3 construtores (não medido em detalhe), sem banca.
