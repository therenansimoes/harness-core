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

## Sua variação genética: LINHAGEM v1 — gate hermético

Seu pai é `gen1/v1` (2º lugar, 78.50). Leia `gen1/v1/evolve.py`, `gen1/v1/policy.py` e `gen1/v1/safety.py` inteiros.

O que ele fez de único e você deve levar adiante: medir baseline e candidato em **cópias descartáveis** (`shutil.copytree` para `tempfile.TemporaryDirectory`), nunca no diretório vivo — resolve por construção o benchmark que se autodestrói. E o protocolo de ação em JSON com dispatcher, que torna o backend intercambiável sem tocar loop, verificador nem gate.

Seus furos herdados para fechar: `propose_change` tem **ponto fixo** — o juiz rodou 6 vezes e obteve 6 aceites de mudança nula, zero rejeições; `cost_usd` e tokens existem no schema do trace e nenhum chamador preenche (sempre 0.0); `check_command` é código morto; `import trace` sombreia módulo da stdlib; `verify.py` hardcoda `python3` em vez de `sys.executable`.

**Seu diferencial nesta geração:** trace com **custo em USD real** por turno, propagado do provider até o log — e um gate que usa custo como métrica, não só passou/não passou. A banca notou que os cinco só conseguiram melhorar o que conseguiam medir, e por isso três foram tunar `max_turns`.
