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

## Sua variação genética: CROSSOVER v3 × v1 — os dois primeiros colocados

Você é o cruzamento dos dois melhores da geração 1, que empataram dentro do ruído (78.75 e 78.50) e são **complementares**. Leia os dois inteiros:

- `gen1/v3/self_improve.py` + `gen1/v3/safety.py` — auto-modificação do próprio código-fonte com backup/rollback, rejeição real gravada em log, e o verificador colocado sob a mesma invariante de safety que o loop (ninguém mais fez isso).
- `gen1/v1/evolve.py` + `gen1/v1/policy.py` — gate hermético medindo em cópias temporárias isoladas, e protocolo de ação em JSON com backend plugável.

**Sua tarefa é a combinação que nenhum dos dois conseguiu sozinho:** auto-modificação de código com rollback provado, medida por um gate hermético em cópias descartáveis. O pai 1 muta bem mas mede na fixture viva; o pai 2 mede bem mas o proponente dele tem ponto fixo e nunca rejeita nada.

Feche os furos dos dois: `realpath` em vez de `abspath`, allowlist de `argv[0]` em vez de denylist, custo real no trace, e melhoria **estrita** exigida no aceite.
