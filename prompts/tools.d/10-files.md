## ls

Lista o conteúdo de um diretório do workspace.

- Argumentos: `path` (string, opcional; default a raiz `/`).
- Exemplo: `ls(path="/")`
- Pegadinha: comece por aqui. Não adivinhe nome de arquivo — liste primeiro.

## read_file

Lê o conteúdo de um arquivo.

- Argumentos: `file_path` (string, obrigatório), `offset` (int, opcional,
  0-indexado), `limit` (int, opcional).
- Exemplo: `read_file(file_path="/app.py")`
- **Pegadinha crítica**: a saída vem com números de linha (`  12\tdef foo():`)
  que **NÃO existem no arquivo**. Nunca copie a numeração nem o `\t` para
  dentro de `old_string`/`content`. Copie só o texto cru, com a indentação
  original.
- **Guarda de arquivo grande**: chamada sem `offset`/`limit` em arquivo com mais
  de 2000 linhas devolve só as **60 primeiras** e o total:
  `TOTAL: 5000 linhas, 61.3 KB`. Isso não é erro — é o aviso de que ler tudo
  não caberia. Chame `file_outline` para achar o trecho e volte paginando.
- Como paginar: a margem é 1-indexada e o `offset` é 0-indexado. Para ler a
  partir da linha 320: `read_file(file_path="/app.py", offset=319, limit=80)`.
- Leia antes de editar. Editar arquivo que você não leu é chute.

## file_outline

Mostra o esqueleto de um arquivo sem trazer o corpo — o mapa em vez do terreno.

- Argumentos: `path` (string, obrigatório).
- Exemplo: `file_outline(path="/app.py")`
- O que sai por extensão: `.py` classes, `def`s e decoradores; `.md` headings;
  `.toml` seções `[assim]`; `.json` chaves de nível 1 e 2; `.js`/`.ts`
  `function`/`class`/`const` exportados; outros, as linhas não indentadas.
- Cada entrada vem com o número da linha, e o fim traz
  `TOTAL: N linhas, M KB`. Use esses números direto no `read_file` (menos 1) ou
  no `edit_range` (como estão).
- Pegadinha: é o PRIMEIRO passo em arquivo desconhecido com mais de umas
  200 linhas. Custa quase nada e evita a leitura cega que queima a tarefa.

## write_file

Escreve um arquivo INTEIRO, criando ou sobrescrevendo.

- Argumentos: `file_path` (string, obrigatório), `content` (string,
  obrigatório — o conteúdo final completo).
- Exemplo: `write_file(file_path="/soma.py", content="def soma(a, b):\n    return a + b\n")`
- Pegadinha: sobrescreve sem aviso. Para arquivo existente, leia antes e
  reescreva com TODO o conteúdo, não só o pedaço novo.
- **Gate READ-BEFORE-WRITE**: reescrever arquivo existente exige `read_file`
  dele nesta sessão, na versão atual. Sem leitura, ou se o arquivo mudou depois
  dela, a escrita é recusada (`leia o arquivo antes de reescrever` / `o arquivo
  mudou desde tua leitura`) — leia e refaça. `content` idêntico ao arquivo passa
  como no-op (`Nada a mudar`).
- **Shrink-guard**: se o `content` tiver menos de 70% do tamanho atual do
  arquivo, a escrita é **recusada** com `isso apagaria ~X% do arquivo`. Não
  existe flag para forçar. A saída é `edit_range` (mudar o trecho) ou reenviar
  o arquivo completo de verdade.
- REGRA DE OURO ao reescrever página/módulo existente: preserve TUDO que a
  tarefa não mandou mudar — cada `<script>`, `<link>`, import e bloco que já
  estava lá continua lá. Caso real: uma reescrita derrubou o
  `<script src="app.js">` e quebrou a feature de outra unidade.
- É a saída de emergência para arquivo NOVO ou pequeno. Em arquivo grande,
  `edit_range` é mais barato e não perde nada.

## edit_file

Troca um trecho exato por outro dentro de um arquivo existente.

- Argumentos: `file_path` (string), `old_string` (string), `new_string`
  (string), `replace_all` (bool, opcional).
- Exemplo: `edit_file(file_path="/app.py", old_string="return a - b", new_string="return a + b")`
- **Pegadinha crítica**: o match é EXATO, byte a byte, incluindo espaços e
  indentação. `old_string` precisa ser único no arquivo (senão use
  `replace_all=true`).
- **Gate READ-BEFORE-WRITE**: vale igual aqui e em `edit_range`/`insert_lines`/
  `append_file` — sem `read_file` da versão atual do arquivo, a edição é
  recusada. Ler é barato; reescrever em cima do que você lembra apaga trabalho.
- **Regra de saída do loop**: se falhar 2 vezes com "String not found", PARE de
  tentar variações. Use `edit_range` com o número da linha que o `read_file`
  mostrou — lá não existe casamento de texto para errar.

## edit_range

Substitui uma faixa de linhas por outro conteúdo. Edita por NÚMERO, não por texto.

- Argumentos: `path` (string), `start_line` (int), `end_line` (int),
  `new_content` (string), `expect_first_line` (string, opcional).
- `start_line`/`end_line` são 1-indexados e **inclusivos**: são exatamente os
  números que aparecem na margem do `read_file` e do `file_outline`.
- Exemplo: `edit_range(path="/app.py", start_line=12, end_line=14, new_content="def foo():\n    return 42", expect_first_line="def foo():")`
- Use `expect_first_line` com o texto exato da linha `start_line` (sem a
  numeração). Se não casar, a tool não escreve nada e devolve as linhas reais
  ao redor, numeradas — corrija a faixa e refaça.
- Retorno em caso de sucesso: `linhas 12-14 substituídas (3→2); validação: ok`
  mais 3 linhas já **renumeradas** e o novo total.
- Pegadinha: `end_line` menor que `start_line` é recusado — para inserir sem
  apagar nada use `insert_lines`. Para apagar linhas, passe `new_content=""`.
- Segurança: a escrita é atômica, o conteúdo anterior fica em backup e, se o
  arquivo ficar inválido (`.py`, `.json`, `.toml`), a tool **desfaz sozinha** e
  responde `REVERTIDO`. Nesse caso o arquivo continua como estava — o erro é no
  seu `new_content`.

## insert_lines

Insere linhas novas sem apagar nada.

- Argumentos: `path` (string), `after_line` (int), `content` (string).
- `after_line=0` insere no topo do arquivo; `after_line=40` insere entre a 40 e
  a 41.
- Exemplo: `insert_lines(path="/app.py", after_line=0, content="import os")`
- Pegadinha: é a tool certa para import novo, função nova no meio do arquivo e
  linha de config — `edit_range` no lugar dela apagaria a linha de referência.
- Mesmas garantias de `edit_range`: atômica, com backup e revert se o arquivo
  ficar inválido.

## append_file

Acrescenta conteúdo no fim do arquivo (cria o arquivo se não existir).

- Argumentos: `path` (string), `content` (string).
- Exemplo: `append_file(path="/notas.md", content="- terminei o passo 2")`
- Pegadinha: nunca reescreve o que já está lá, então é seguro em log e em
  arquivo de notas. Falta de `\n` no fim do arquivo é resolvida pela tool.
- Mesmas garantias de `edit_range`: atômica, com backup e revert.

## glob

Encontra arquivos por padrão de nome.

- Argumentos: `pattern` (string, obrigatório), `path` (string, opcional).
- Exemplo: `glob(pattern="**/*.py")`
- Pegadinha: casa nome de arquivo, não conteúdo. Para conteúdo, use `grep`.

## grep

Busca uma regex no conteúdo dos arquivos.

- Argumentos: `pattern` (string, obrigatório), `path` (string, opcional),
  `glob` (string, opcional).
- Exemplo: `grep(pattern="def soma", glob="*.py")`
- Pegadinha: é o jeito certo de achar onde um símbolo está definido antes de
  editar. Barato — use em vez de ler o repositório todo.

