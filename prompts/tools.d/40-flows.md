<!--
Fragmento do manual das tools: fluxos de projeto. Estas tools substituem
comandos de shell que o modelo montaria à mão — o valor está no veredito curto,
então o manual insiste no que NÃO fazer (repetir o comando no shell).
-->

## detect_stack

Diz que tipo de projeto é este: python, node, lockfiles e scripts do `package.json`.

- Argumentos: nenhum.
- Exemplo: `detect_stack()`
- É barato (só olha arquivos, não roda nada). Chame antes de chutar comando —
  rodar `npm test` em projeto python é um turno jogado fora.

## install_deps

Instala as dependências do projeto com o gerenciador que ele já usa.

- Argumentos: nenhum.
- Exemplo: `install_deps()`
- Escolhe sozinho: `uv venv` + `uv pip install` para `pyproject.toml`/
  `requirements.txt`, e `npm ci`/`pnpm`/`yarn` conforme o lockfile do
  `package.json`. Não passe comando: o manifesto do workspace manda.
- A saída é `ok=true gerenciador=npm pacotes=N sec=S`. Em falha vêm as últimas
  linhas de erro filtradas — leia ANTES de tentar de novo; instalar duas vezes
  pelo mesmo motivo gasta minutos do orçamento.
- Pegadinha: pode levar até 600s. Não é travamento, e não existe versão mais
  rápida disso pelo `execute` (lá o teto é 120s e o comando morre no meio).
- Pegadinha: instalação GLOBAL não passa (`npm i -g`, `pip install --user`).
  Tudo cai no workspace, é assim que deve ser.

## run_tests

Roda a suíte e devolve quantos passaram e as falhas com arquivo:linha.

- Argumentos: `cmd` (string, opcional — comando alternativo).
- Exemplo: `run_tests()`
- Exemplo: `run_tests(cmd="npm test -- --run src/soma.test.ts")`
- Sem `cmd`, usa `.venv/bin/python -m pytest -q` (python) ou `npm test`
  (node, se houver script `test`).
- Você recebe no máximo 5 falhas resumidas. O log COMPLETO fica em
  `/.harness/tests.log`: se as 5 não bastarem, `read_file` nesse path — é mais
  barato que rodar a suíte de novo.
- Pegadinha: `failed=0` com `ok=false` significa que a suíte nem chegou a rodar
  (erro de import, dependência faltando). Aí o conserto é `install_deps` ou o
  import, não o teste.

## run_lint

Roda o linter (`ruff` no python, `eslint` no node) e lista os erros com arquivo:linha.

- Argumentos: `fix` (bool, opcional; default false).
- Exemplo: `run_lint()`
- Exemplo: `run_lint(fix=True)`
- Devolve `ok=true 0 erros` ou a contagem mais as 10 primeiras linhas
  `file:line: code msg`.
- `skipped:no-linter` quer dizer que não há linter disponível neste ambiente.
  Não é erro seu, não há o que consertar e não tente instalar um.
- Pegadinha: `eslint` só roda se o projeto tiver config de eslint. Sem config,
  ele simplesmente não aparece na saída.

## local_screenshot

Tira um screenshot PNG de uma página servida em `127.0.0.1`.

- Argumentos: `port` (int, obrigatório), `path` (string, opcional; default `/`),
  `out` (string, opcional; default `shot.png`).
- Exemplo: `local_screenshot(port=5173)`
- Exemplo: `local_screenshot(port=3000, path="/login", out="login.png")`
- Pegadinha: a porta precisa estar REGISTRADA (`/.harness/procs.json`), ou seja:
  o servidor tem que ter sido subido pela tool de processo. Porta que você
  adivinhou volta como `porta N não está registrada` — suba o servidor primeiro
  em vez de tentar outra porta.
- O PNG fica no workspace. Ele é evidência para quem lê o run depois; descrever
  a tela sem ter tirado o screenshot é chute.
