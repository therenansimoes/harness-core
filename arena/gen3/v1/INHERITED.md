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

## Sua variação genética: MEDIÇÃO REAL

Seu foco: o marco de peso 20 que a base tem zerado — **trace + medição**.

Hoje todo turno grava `"tokens": 0, "cost_usd": 0.0, "backend": "offline"`. Sem custo por turno não existe eixo de eficiência, e o gate fica preso a passou/não passou. As gerações anteriores só conseguiram melhorar aquilo que conseguiam medir.

Faça o custo ser real e propagado do provider até o log, e faça o **gate usar custo como métrica** — rejeitar candidato que passa mas fica mais caro. Se não houver chave de API, modele custo por tokens contados localmente com uma tabela de preço; o importante é o número deixar de ser zero e passar a mover decisão.
