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

## Sua variação genética: NICHO LIVRE — reuso de peça pronta

Você não herda um pai específico. Seu nicho é o buraco que a geração 1 inteira deixou: **zero reuso**. Cinco de cinco escreveram loop, parser e sandbox do zero, com a mesma justificativa ("ler docs custaria mais que escrever"). Plausível para o loop; **falso para o sandbox** — todos reinventaram denylist de substring, o padrão sabidamente quebrado, quando container, `seccomp`, `bubblewrap` ou subprocess com usuário restrito já existem prontos.

**Sua tarefa:** construir sobre peça pronta boa em vez de reinventar. Pesquise se precisar. Candidatos plausíveis: LangGraph (checkpoint, resume, human-in-the-loop), MCP python-sdk (protocolo de tools), mini-swe-agent (design do loop bash-only, ~100 linhas), Aider (repo-map, formatos de edição), smolagents, DSPy, ou isolamento de verdade via container/`seccomp`/`rlimit`.

Você está autorizado a gastar tempo pesquisando e instalando dependência — os outros quatro não vão. Mas o `run.sh` tem que funcionar no fim, e dependência que não roda é pior que ausência: se a instalação falhar, caia num modo degradado que ainda demonstra trabalho.

Leia `gen1/FEEDBACK.md` e o código dos dois primeiros colocados (`gen1/v3`, `gen1/v1`) para não repetir as três patologias universais. Depois monte sua solução em cima do que já existe no mundo.
