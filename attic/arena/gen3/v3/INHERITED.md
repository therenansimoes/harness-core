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

## Sua variação genética: FECHAR O FURO E ENDURECER

Seu foco: invariantes de segurança com mecanismo de verdade.

O `NOTES.md` da base denuncia o próprio furo: `python3 -c "os.system(...)"` passa pela allowlist, porque `python3` está permitido e o guard não olha o que vem depois. Feche isso. Opções que o próprio autor sugeriu: checagem AST do patch antes de executar (sem `import os`, `subprocess`, `open` fora do task_dir), ou remover `python3`/`sh` nus da allowlist para conteúdo escrito pelo agente.

Depois **prove** que fechou: adicione ao `run.sh` a tentativa de exploração e mostre-a sendo bloqueada. O juiz de Segurança vai atacar com symlink, traversal, `python3 -c` e ausência de timeout — chegue lá antes dele.
