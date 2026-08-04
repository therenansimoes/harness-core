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

Se o projeto tem `AGENTS.md` (ou `AGENTS-exec.md`, que ganha dele), ele é lei
local: o que estiver lá vence este manual quando os dois discordarem.

