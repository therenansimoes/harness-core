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

## Sua variação genética: LIVRE — SUPERAR COMO QUISER

Você não tem foco imposto. Leia a base, rode, leia o `NOTES.md` dela, e decida você onde está o maior ganho marginal.

Duas coisas a saber: os outros quatro construtores desta geração estão atacando, respectivamente, medição de custo real, `--repo` de terceiro, o furo do `python3 -c`, e o espaço de busca do proponente. Você tem liberdade para atacar outra coisa — ou para atacar a mesma coisa melhor que eles.

Você também está autorizado a gastar tempo pesquisando e reusando peça pronta de open source (LangGraph, MCP, mini-swe-agent, Aider, isolamento real por container/rlimit). Nenhuma geração reusou nada até hoje: cinco de cinco reinventaram o sandbox com denylist de substring, o padrão sabidamente quebrado. Se for por esse caminho, garanta o fallback: dependência que não instala não pode derrubar o `run.sh`.
