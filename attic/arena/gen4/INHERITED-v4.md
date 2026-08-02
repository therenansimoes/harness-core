# HERANÇA — GEN4 / v4

## Seu rank

Você ataca o **#4** da SUA própria lista ranqueada (não a minha, não a de
outro candidato). Se seu diagnóstico honesto só tiver 3 itens claros, declare
isso no NOTES.md e explique o que você faria no #4 se tivesse mais tempo —
não invente um item fraco só para preencher a lista.

## Lições de método das gerações 1-3 (não repetir)

- **Pedido no prompt não é mecanismo.** Na gen 2, "escreva cedo" dito em texto
  não bastou — 4 de 5 construtores morreram no prazo sem `run.sh` nem
  `NOTES.md`. Por isso o runner força fase 1 curta com esse objetivo único.
- **Sonda tem que discriminar.** Se todos empatam, quem falhou foi a tarefa —
  registre no NOTES.md o que você mediu, não só o que você acha.
- **Não completar não penaliza; mentir zera.** Alegar verde sem rodar é pior
  que admitir que não deu tempo.
- **Verificar a base por execução antes de mexer é obrigatório.** Rode
  `python3 -m pytest tests/ -q` antes de qualquer edição — ela já deve estar
  verde no estado em que você recebeu (validado na preparação desta geração).
- **Circuito fechado é a patologia mais comum:** nas gerações passadas,
  candidatos que criam a própria tarefa/bug/agente não provam nada — o gate
  não consegue reprovar. Prefira melhorias que se sustentam contra código ou
  cenário que você não escreveu.

## Estado atual do harness

Veja `STATUS.md` e `generative-project.md` no próprio repo para o detalhe
completo. Resumo: o marco "saiu do lugar" já foi atingido (o harness corrigiu
um bug real em código de terceiro, upstream verde). As réguas de juízo
(j_web, j_hw, j_b2b) rodam e medem com citação obrigatória + veto. Buracos
conhecidos e não fechados até aqui: consistência do j_b2b em tarefas longas
(bimodal 47-81), catálogo de mutação ainda pequeno, contenção do runner é
detecção (não prevenção) — containerizar é dívida reconhecida.

Isso é ponto de partida para SEU diagnóstico, não a resposta pronta: seu rank
pode ser algo completamente diferente disso.
