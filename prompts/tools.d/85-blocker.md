<!--
Fragmento do manual das tools: declaração tipada de bloqueio. Some do prompt
quando a tool de blocker não está montada.
-->

## declare_blocker

Diz POR QUE você não consegue concluir, com tipo fechado e um detalhe em texto.

- Assinatura: `declare_blocker(type="...", detail="...")`. `type` é um de
  `missing_evidence`, `needs_user_input`, `external_wait`, `goal_not_met_yet`.
  Tipo inventado não grava nada: a tool devolve a lista dos válidos.
- Quem lê isto é o gate do harness, não uma pessoa: o tipo decide se a próxima
  tentativa acontece, se ela espera antes de rodar, ou se a tarefa vai para um
  humano. Parar sem declarar chega ao gate como "não fez nada" e te devolve outra
  tentativa pelo mesmo caminho morto.
- Qual tipo usar:
  - `missing_evidence` — falta prova, não falta decisão. Você não conseguiu ler o
    arquivo/log/saída que diria se a mudança está certa. A próxima tentativa pode
    resolver: diga no `detail` exatamente qual evidência falta.
  - `needs_user_input` — a decisão não é sua. Duas leituras da tarefa são
    defensáveis, falta credencial/segredo, ou o pedido conflita com o que está no
    repo. **Isto NÃO é desistência: é a rota para o humano**, e é o único tipo que
    não gasta tentativa. No `detail` faça a pergunta fechada que destrava, não um
    relatório.
  - `external_wait` — depende de algo fora daqui que ainda não respondeu (build de
    terceiro, serviço, propagação). A próxima tentativa continua, só depois de uma
    espera. Diga o que você está esperando.
  - `goal_not_met_yet` — você entendeu a tarefa, o caminho existe, e o que você
    fez ainda não chega lá. Retry normal. No `detail` diga o que falta fazer, para
    a próxima tentativa não recomeçar do zero.
- Declare **uma vez**, e antes disso escreva o que já dá para escrever: a régua
  julga o que está no workspace, e progresso parcial declarado conta. Blocker não
  apaga o seu trabalho, ele explica onde ele parou.
- Não use para reclamar de teste vermelho que você pode consertar, nem para pedir
  permissão de algo que a tarefa já autorizou. Blocker declarado sem bloqueio real
  queima a tentativa que você teria.
