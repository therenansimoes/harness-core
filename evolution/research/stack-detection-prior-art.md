# Prior art — detecção determinística de stack/comandos (pré-D3, 2026-08-02)

Pesquisa do researcher. Veredito: **criar `profile.py` do zero, sem dependência nova.**

## Fonte principal a roubar

`universal-test-runner` (xavdid, MIT, Python 3.9+): `commands.py` + `context.py` (~360 linhas, stdlib puro; click/colorama só na CLI). Estrutura: dataclass `Command(name, should_run, _test_command, debug_line)` + lista ordenada + primeiro-match. Resolve só `test` — por isso não vale como dependência (queremos test+lint+build). Creditar MIT em comentário de cabeçalho ao vendorizar a heurística.

Ordem dele (task runner genérico VENCE linguagem — oposto do Projectile; adotar a dele): justfile → makefile → uv_pytest → pdm_pytest → poetry_pytest → pytest → django → py fallback → go → rust → npm/yarn/pnpm → bun.

Outros: nixpacks (detect por marcador puro, package manager por lockfile, `packageManager` do package.json vence lockfile); nx Project Crystal (config file do runner é marcador mais confiável que package manager); starter-workflows do GitHub (comandos canônicos por stack); aider NÃO detecta comando de teste (usuário passa `--test-cmd`) — descartado como prior art.

## Tabela consolidada (ordem = prioridade, primeiro match vence)

| # | Marcador | Condição extra | test | lint | build |
|---|---|---|---|---|---|
| 1 | `justfile`/`Justfile`/`.justfile` | receita `test:` (regex `^@?test(:\| )`, sem subprocess) | `just test` | `just lint` se existir | `just build` se existir |
| 2 | `Makefile` | linha começa com `test:` | `make test` | `make lint` | `make build` |
| 3 | `pnpm-workspace.yaml` \| `turbo.json` \| `nx.json` \| `package.json` c/ `workspaces` | monorepo — detectar ANTES de linguagem | `pnpm -r test` / `turbo run test` / `nx run-many -t test` | idem `lint` | idem `build` |
| 4 | `uv.lock` \| `pdm.lock` \| `poetry.lock` | + pytest detectado | `uv run pytest` / `pdm run pytest` / `poetry run pytest` | `<pm> run ruff check .` | — |
| 5 | pytest detectado | `.pytest_cache`, `pytest.ini`, `[pytest]` em tox.ini, `[tool:pytest]` em setup.cfg, `[tool.pytest.ini_options]`, ou dep `pytest*` em dependency-groups/optional-deps/poetry groups/uv dev-deps | `pytest` | `ruff check .` se `[tool.ruff]`, senão `flake8` | — |
| 6 | `manage.py` | e NÃO pytest (pytest-django vence) | `./manage.py test` | — | — |
| 7 | `pyproject.toml`/`setup.py`/`tox.ini`/`setup.cfg`/`requirements.txt` | fallback py | `python -m unittest` | — | `python -m build` |
| 8 | `go.mod` | | `go test ./...` (NUNCA `go test` puro — zero testes em módulo com subpacotes) | `go vet ./...` | `go build ./...` |
| 9 | `Cargo.toml` | | `cargo test` | `cargo clippy -- -D warnings` | `cargo build` |
| 10 | `package.json` + `scripts.test` | exige script E lockfile; pm por lockfile (package-lock→npm, yarn.lock→yarn, pnpm-lock→pnpm); campo `packageManager` vence lockfile | `<pm> test` | `<pm> run lint` se script | `<pm> run build` se script |
| 10b | `bun.lockb`/`bun.lock` | não exige script | `bun test` | — | `bun run build` |
| 11 | `tsconfig.json` | TS sem script test | `npx tsc --noEmit` (typecheck, marcar como tal) | — | — |

## Pegadinhas (copiar as defesas)

- `npm test` stub do `npm init` (`echo "Error: no test specified" && exit 1`) passa no check de existência — filtrar por regex `exit 1|no test specified`.
- JS: exigir `scripts.test` E lockfile (bun é exceção deliberada).
- `[tool.poetry]` no pyproject não implica poetry instalado — decidir pelo lockfile presente.
- Monorepo: rodar pytest/npm test na raiz é quase sempre errado — workspace antes de linguagem.
- Marcadores fantasma: `requirements.txt` em repo Node, `Makefile` só com `make docs` — checar conteúdo (`test:`), não só existência.
- Coluna lint/build da tabela é inferência do researcher (starter-workflows + convenção), não citação — confiança média.
- Sempre retornar `debug_line`/motivo do match — essencial pra depurar quando o profile errar.

## Não verificado

turborepo (inferência de tasks), mise/devbox (provavelmente só toolchain, irrelevante), Continue/Cursor, busca PyPI exaustiva (scrape falhou; "não existe lib melhor" é ausência de evidência). A suíte de testes do universal-test-runner provavelmente tem fixtures de repo por stack prontas pra roubar.
