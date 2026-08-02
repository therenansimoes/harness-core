Você está na **geração 2**. A geração 1 rodou com 5 construtores, foi julgada por 4 personas e produziu o material abaixo. Ele é seu por herança — todos os construtores desta geração recebem exatamente o mesmo, íntegro.

## Leitura obrigatória (leia agora, antes de escrever qualquer linha)

Caminhos absolutos, **somente leitura** — você pode ler tudo isto, não pode escrever nada aqui:

- `/Users/renansimoes/projects/harness-core/arena/LEADERBOARD.md` — ranking e notas da geração 1
- `/Users/renansimoes/projects/harness-core/arena/gen1/FEEDBACK.md` — o feedback consolidado da banca (o documento mais valioso que você tem)
- `/Users/renansimoes/projects/harness-core/arena/gen1/scores.json` — nota por critério de cada variante, com os furos nomeados um a um
- `/Users/renansimoes/projects/harness-core/arena/gen1/v1/` … `v5/` — **o código-fonte inteiro dos cinco antecessores**. Leia os que interessam. Copiar e melhorar código deles é permitido e incentivado; herança é o ponto da arena.

Não gaste mais que ~60 segundos lendo. O `FEEDBACK.md` e o código do seu pai (indicado abaixo) são o que importa; o resto é consulta.

## Resultado da geração 1

| # | variante | nota | resumo |
|---|---|---|---|
| 1 | `gen1/v3` | 78.75 | única rejeição de gate real gravada, com rollback; muta o próprio código-fonte; verificador sob a invariante de safety |
| 2 | `gen1/v1` | 78.50 | melhor arquitetura; único gate hermético (baseline e candidato em cópias temporárias); policy plugável |
| 3 | `gen1/v2` | 65.50 | 212 linhas, zero deps, melhor trace (tokens + sha1 do verificador); mas o 1º comando do README quebrava e corrompia a workspace |
| 4 | `gen1/v5` | 50.50 | motor real, fixture entregue já corrigida: verde sem trabalho |
| 5 | `gen1/v4` | 35.50 | sem loop; teste passava até com o artefato do agente deletado |

Dispersão 35.5 → 78.75. Ninguém chegou perto do teto: **há muito espaço acima de 79.**

## O que a banca vai fazer com o seu trabalho

Os quatro juízes **executam `./run.sh` antes de abrir qualquer arquivo**. Eles não leem a sua receita, eles provam o prato. Depois tentam quebrar: o Cético vai deletar o artefato do seu agente e reverter a fixture para ver se o seu verde sobrevive ao que não deveria sobreviver; o de Segurança vai atacar com symlink e path traversal e procurar `subprocess` sem timeout; o de Produto vai apontar o seu harness para código que não é seu e ver se funciona fora da sua bancada.

Construa sabendo disso.


---

## Sua variação genética: LINHAGEM v3 — auto-modificação com prova

Seu pai é `gen1/v3` (1º lugar, 78.75). Leia `gen1/v3/self_improve.py` e `gen1/v3/safety.py` inteiros.

O que ele fez de único e você deve levar adiante: mutar o **próprio código-fonte** com backup → aplicar → re-executar em subprocesso limpo → rollback, e **arquivar a rejeição no repositório** como prova de que o gate tem os dois braços.

Seus furos herdados para fechar: `guard_path` usa `abspath` e foi escapado por symlink (use `realpath`); `guard_command` é denylist de substring que não cobre `python3 -c`, que é a forma que o próprio código usa (troque por allowlist de `argv[0]`); `apply_change` é um `str.replace` com uma única mutação possível (externalize os knobs mutáveis para um espaço de busca declarado); o agente lê a dica no comentário `# BUG: should be a + b` (tire a dica).

**Seu diferencial nesta geração:** faça o harness aceitar um diretório e um comando de teste de terceiro (`--repo <dir> --test-cmd "<cmd>"`). Ninguém na geração 1 apontou o harness para código que não era dele. Demonstre isso no `run.sh`.
