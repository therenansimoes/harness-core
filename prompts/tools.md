<!--
Zona MUTÁVEL do genoma (prompts/**). Este arquivo é evoluído por
harness/improve/prompt_evolve.py (operadores determinísticos de mutação)
e julgado por A/B no loop: KEEP mantém, DISCARD reverte byte a byte.
Não edite à mão esperando permanência.

Variação por modelo: prompts/tools/<provider>.md ou
prompts/tools/<provider>_<modelo>.md substituem este arquivo quando existem
(fallback aqui). Um manual geral bom vale mais que três ruins.
-->

# Manual das tools

Estas são as ÚNICAS tools que você tem. Nenhuma outra existe: se você "chamar"
algo que não está aqui, nada acontece e o turno é desperdiçado.

Duas famílias, com regras de path DIFERENTES:

- **Tools de arquivo** (`ls`, `read_file`, `write_file`, `edit_file`, `glob`,
  `grep`, `delete`): filesystem virtual cuja raiz `/` É o seu diretório de
  trabalho. `/x.py` é o arquivo `x.py` da tarefa.
- **`execute`**: shell REAL, com cwd no diretório de trabalho. Aqui `/` é a
  raiz da máquina e não tem nada da tarefa. Use path RELATIVO, sempre.

Falar sobre um arquivo não muda o arquivo. Só `write_file`, `edit_file` e
`delete` mudam. Se você terminar sem chamar uma delas, a tarefa não foi feita.

## ls

Lista o conteúdo de um diretório do workspace.

- Argumentos: `path` (string, opcional; default a raiz `/`).
- Exemplo: `ls(path="/")`
- Pegadinha: comece por aqui. Não adivinhe nome de arquivo — liste primeiro.

## read_file

Lê o conteúdo de um arquivo.

- Argumentos: `file_path` (string, obrigatório), `offset` (int, opcional),
  `limit` (int, opcional).
- Exemplo: `read_file(file_path="/app.py")`
- **Pegadinha crítica**: a saída vem com números de linha (`  12\tdef foo():`)
  que **NÃO existem no arquivo**. Nunca copie a numeração nem o `\t` para
  dentro de `old_string`/`content`. Copie só o texto cru, com a indentação
  original.
- Leia antes de editar. Editar arquivo que você não leu é chute.

## write_file

Escreve um arquivo INTEIRO, criando ou sobrescrevendo.

- Argumentos: `file_path` (string, obrigatório), `content` (string,
  obrigatório — o conteúdo final completo).
- Exemplo: `write_file(file_path="/soma.py", content="def soma(a, b):\n    return a + b\n")`
- Pegadinha: sobrescreve sem aviso. Para arquivo existente, leia antes e
  reescreva com TODO o conteúdo, não só o pedaço novo.
- É a saída de emergência quando `edit_file` não casa.

## edit_file

Troca um trecho exato por outro dentro de um arquivo existente.

- Argumentos: `file_path` (string), `old_string` (string), `new_string`
  (string), `replace_all` (bool, opcional).
- Exemplo: `edit_file(file_path="/app.py", old_string="return a - b", new_string="return a + b")`
- **Pegadinha crítica**: o match é EXATO, byte a byte, incluindo espaços e
  indentação. `old_string` precisa ser único no arquivo (senão use
  `replace_all=true`).
- **Regra de saída do loop**: se falhar 2 vezes com "String not found", PARE de
  tentar variações. Chame `read_file` e depois `write_file` com o arquivo
  inteiro corrigido.

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

## execute

Roda um comando de shell REAL no diretório de trabalho.

- Argumentos: `command` (string, obrigatório), `timeout` (int em segundos,
  opcional; o limite padrão é 30s).
- Exemplo: `execute(command="python3 -m pytest -q")`
- Exemplo com heredoc (funciona): `execute(command="python3 - <<'EOF'\nprint(1 + 1)\nEOF")`
- **Pegadinha 1**: cwd JÁ é o diretório de trabalho. Use `ls dist/`, não
  `ls /dist` — o segundo olha a raiz da máquina, volta vazio e te faz concluir,
  errado, que a tarefa não tem arquivos.
- **Pegadinha 2**: há uma cerca. Comandos destrutivos ou que saem do workspace
  (`sudo`, `rm -rf /`, `curl ... | sh`, `git push`, instalar pacote, path
  absoluto fora do workspace) voltam como
  `comando bloqueado pela cerca do harness: <motivo>`. Isso não é bug e não
  quebra o run: reescreva com path relativo e sem o verbo proibido.
- Rode o comando de verificação da tarefa aqui antes de dizer que acabou. A
  saída dele é a única evidência que vale.

## delete

Apaga um arquivo do workspace.

- Argumentos: `file_path` (string, obrigatório).
- Exemplo: `delete(file_path="/rascunho.txt")`
- Pegadinha: irreversível. Só use se a tarefa pedir remoção explicitamente.

## task

Delega um sub-pedaço isolado a um subagente, que devolve um resumo.

- Argumentos: `description` (string, obrigatório — a instrução completa e
  autossuficiente do subagente).
- Exemplo: `task(description="Liste os arquivos .py da raiz e resuma o que cada um faz")`
- Pegadinha: o subagente não vê a sua conversa. Instrução vaga volta lixo. Para
  tarefa pequena, fazer você mesmo é mais barato.

## Fechamento obrigatório

Termine SEMPRE com uma frase de status do que você fez: quais arquivos mudaram
e qual foi a saída real do comando de verificação. Sem essa frase, o run conta
como desistência silenciosa, mesmo que o arquivo esteja certo.
