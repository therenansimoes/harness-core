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

## Sua variação genética: FERRAMENTA, NÃO BANCADA

Seu foco: o buraco que **nenhuma geração fechou** — apontar o harness para código que não é dele.

Implemente `--repo <dir> --test-cmd "<cmd>"`: o harness recebe um projeto de terceiro, usa o comando de teste **daquele projeto** como oráculo, e tenta consertar o que estiver vermelho. Baseline por `git stash` ou por cópia descartável.

Demonstre no `run.sh` com um projetinho de terceiro que você mesmo cria — separado da fixture da base, com estrutura diferente (outro layout, outro runner de teste se der). O juiz de Produto vai fazer exatamente isso: criar um projeto dele e apontar o seu harness para lá.
