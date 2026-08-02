Você está na **geração 3**. O diretório já contém o melhor harness que a arena produziu: o vencedor da geração 2, que por sua vez é o cruzamento dos dois primeiros colocados da geração 1.

## Material de consulta (somente leitura, ~60s no máximo)

- `/Users/renansimoes/projects/harness-core/arena/LEADERBOARD.md` — notas das gerações anteriores
- `/Users/renansimoes/projects/harness-core/arena/gen1/FEEDBACK.md` — feedback da banca, o documento mais valioso que existe aqui
- `/Users/renansimoes/projects/harness-core/arena/gen1/v1..v5/` e `gen2/v1..v5/` — código-fonte de todos os antecessores. Copiar e melhorar o que eles fizeram é permitido e incentivado.
- `NOTES.md` no seu próprio diretório — escrito pelo autor da base, lista os buracos que ele deixou

## Como as gerações anteriores morreram

**Gen 1:** três patologias em 5 de 5 — gate que nunca reprova (só aceites no log, candidato idêntico ao baseline); stub offline que É o gabarito; critério de aceite dentro do diretório gravável pelo agente. A base que você recebeu já fechou as três.

**Gen 2:** quatro de cinco foram mortos pelo prazo sem entregar `run.sh` nem `NOTES.md`. Trabalharam 50% mais e entregaram menos. Por isso agora existem duas fases — o entregável fica garantido em disco antes de você começar a melhorar.

---

## Sua variação genética: PODER DE BUSCA

Seu foco: o loop de auto-melhoria — hoje o catálogo de mutação é minúsculo.

A base sabe reordenar política e pouco mais. Um gate hermético excelente não vale nada se o proponente só sabe uma jogada: as gerações anteriores morreram todas nisso, com propostas de ponto fixo que convergiam para "não mudar nada".

Amplie o espaço de busca de verdade: mais operadores de mutação, um espaço de knobs declarado com faixas, e — se conseguir — **crossover** entre dois candidatos. E ligue a escolha da próxima mutação ao que o trace mostra que está falhando, em vez de sortear às cegas.
