<!--
Fragmento do manual das tools: índice de símbolos (definição, uso, assinatura).
Some do prompt quando as tools de símbolo não estão montadas — descrever tool
que não existe gasta turno do modelo tentando chamá-la.
-->

## find_symbol

Diz **onde um nome é definido**: arquivo, linha, tipo e a linha da assinatura.

- Argumento: `name` (string) — o nome exato ou o começo dele.
- Exemplos: `find_symbol(name="handleSubmit")` · `find_symbol(name="Ledger")` ·
  `find_symbol(name="render_")`
- **Chame isto ANTES de `grep` ou de `read_file` num repo que você não conhece.**
  É ~10x mais barato em contexto: devolve a DEFINIÇÃO em uma linha, enquanto o
  grep devolve os 40 usos primeiro e o `read_file` traz o arquivo inteiro para
  você achar uma linha. Ler arquivo para descobrir onde algo mora é o erro que
  queima o orçamento da run.
- Indexa `.py` (classe, `def`, `async def` — inclusive métodos e decorados),
  `.js/.jsx/.ts/.tsx` (`function`, `class`, `const`/`let` e arrow com nome;
  `interface`/`type`/`enum` em TypeScript) e `.html` (ids, com `<section>`,
  `<main>` e `<nav>` marcados pela tag).
- Casa nome exato primeiro; se sobrar espaço na lista, completa por prefixo. Saída
  no topo de 20 — se vier truncada, o nome é genérico demais, refine.
- "nenhuma definição" é resposta útil e **final**: o nome não é definido neste
  workspace (veio de dependência, ou você errou o nome). Não vale sair varrendo o
  repo à mão atrás dele.
- Não indexa `.venv`, `node_modules`, `dist`, `build`, `.git` nem pasta oculta, e
  para em 2000 arquivos. Símbolo de biblioteca instalada não está aqui de
  propósito: você não vai editar dependência.

## find_references

Diz **quem usa** um nome, ignorando string e comentário.

- Argumento: `name` (string).
- Exemplo: `find_references(name="MAX_HITS")`
- Só os arquivos que o índice conhece, casamento por palavra inteira, topo 20.
  `"handleSubmit"` dentro de string ou de comentário **não** conta — é isso que
  separa esta tool de um `grep`, que casa os dois e mente sobre o raio do uso.
- Use antes de renomear, mudar assinatura ou apagar: a lista é o estrago que a
  mudança vai causar. Depois de editar, rode de novo para conferir que sobrou
  zero uso do nome antigo.

## signature_of

Devolve **a linha da assinatura** de uma função ou classe.

- Argumento: `name` (string).
- Exemplo: `signature_of(name="index_workspace")` →
  `def index_workspace(ws: str | Path) -> dict[...]:`
- Use antes de CHAMAR algo que você não escreveu: resolve nome e ordem dos
  parâmetros em uma linha. Inventar argumento e descobrir o erro no `run_tests`
  custa dois turnos; isto custa um.
- É a linha literal do arquivo, não a documentação: se a função tem `*args` ou
  default esquisito, é o que você vai ver. Precisa do corpo? Só então
  `read_file` na linha que o `find_symbol` deu.
