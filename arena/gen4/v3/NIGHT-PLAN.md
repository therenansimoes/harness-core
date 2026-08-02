# Plano da noite — 2026-08-02 (autorização: Renan, até ele dizer "pare" de manhã)

Modo: trabalho contínuo autônomo. Fable orquestra; subagentes executam. Prioridade a trabalho validável offline; runs pagas só no caminho crítico com teto declarado. Checkpoint commit a cada entrega. De manhã: relatório completo.

## Fila (ordem; riscar ao concluir)

1. ~~[EM VOO] A/B MAX_TURNS 13v30 na task do juiz → decisão v0.4 (teto $2.50).~~
2. ~~Generalizar `judges/run_judge.py` para os 3 juízes (JUDGE_ID hardcoded → parâmetro/loop). Offline, testes com mock.~~
3. ~~M2: rodar os 3 juízes na melhor versão → primeira medição de generalidade (teto ~$3; mediana + spread).~~
4. ~~SPEC-J2 Design 2: `process_metrics.py` (X1/X2/X3 do trace) + RUBRIC-J2 + brief/accept do build_j_b2b. Tudo offline/testável.~~
5. ~~Persona: CoT estilo G-Eval antes do score (custo zero, mudança de prompt) + blindar `call_persona` contra exit-1-com-JSON (mesmo bug já corrigido no agent.py).~~
6. ~~Importar 2-3 tasks portáveis do Terminal-Bench (POC já validada no scratchpad) como suite `tb`.~~
7. ~~README honesto reescrito (estado real, como rodar, arquitetura) — preparação open source (M3).~~
8. ~~Se sobrar noite: graph.py tabela judgements; GC/polimento; revisar dívidas do STATUS.~~

fila estrutural concluída 2026-08-02 ~06h; extras: judge_ok, verdict history, trilha B, candidata v0.5 registrada.

## Regras da noite

- Nada de frente exploratória nova sem validação barata antes (validar → desenvolver, ordem do Renan).
- Um A/B pago por vez. Braços contemporâneos sempre (lição v0.3).
- Falhou 2x a mesma coisa → registrar, pular pro próximo da fila, não insistir.
- Cota estourou → aguardar reset via wakeup agendado, retomar do topo da fila.
- Cada conclusão → commit + linha no STATUS.md.
- **Anti-preguiça (contrato):** o loop SÓ encerra com "pare" explícito do Renan. Todo wakeup sem agente em voo DEVE disparar o próximo item da fila no mesmo turno. Fila vazia não existe: acabou a fila → gerar a próxima fila a partir das dívidas do STATUS.md e do radar (validar barato → desenvolver). Cada wakeup reagenda o próximo antes de terminar o turno. "Está tudo lindo" = sinal pra melhorar mais, não pra parar.
