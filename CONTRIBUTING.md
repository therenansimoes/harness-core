# CONTRIBUTING

Leia `docs/ARCHITECTURE.md` antes do primeiro PR. Regra geral: PR pequeno, com
aceite executável — "melhorou" sem comando que prove não entra.

## Rodar os testes

```bash
uv sync --extra deepagents
uv run --extra deepagents pytest -q     # 364 passed, 2 deselected
```

Sem o extra (`uv sync && uv run pytest -q`) a suite também fica verde —
`362 passed, 2 skipped`: os testes que importam `deepagents` pulam quando a lib
não está instalada. Todo o resto roda com o backend `mock`, que é
determinístico e não toca rede.

Teste não escreve no repo. Isolamento é por env var, com `tmp_path`:

```python
@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"
```

Mesma convenção para `HARNESS_CONFIG_DIR`, `HARNESS_ROOT` e
`HARNESS_PROJECTS_ROOT`. Teste que depende do `config/` do repo é teste que
quebra quando o loop calibrar um TOML.

## Markers `ollama` e `claude_cli`

Dois markers marcam o que precisa de máquina de verdade, e os dois estão
**desligados por padrão** (`addopts = "-m 'not ollama and not claude_cli'"` no
`pyproject.toml`):

- `ollama` — exige servidor Ollama local rodando. Custo $0, mas depende do
  modelo instalado.
- `claude_cli` — exige o CLI oficial instalado e autenticado. **Gasta
  dinheiro.**

```bash
uv run --extra deepagents pytest -m ollama -q      # opt-in explícito
```

Teste que chama modelo de verdade sem um desses markers é bug de teste: a
suite default tem que rodar em qualquer máquina, offline, de graça. E teto de
hardware é regra, não sugestão — nesta máquina (18GB), modelo local acima de
~8B não entra em tier nenhum.

## Plugar um backend

Três métodos (`harness/backends/base.py`): `capabilities()`, `preflight()` e
`execute(ExecRequest) -> ExecResult`. `preflight()` é determinístico e **não
chama LLM** — é o que faz `harness backends` ser barato de rodar a qualquer
hora.

Backend de terceiro não precisa de PR aqui: publique um pacote que se anuncie
no entry point.

```toml
[project.entry-points."harness.backends"]
meu_backend = "meu_pacote.backend:MeuBackend"
```

Em teste (ou para plugin não instalado), `registry.register(nome, factory)`.
Auth segue o mesmo desenho no entry point `harness.auth` (`env()` + `check()`);
o repo shippa só `NullAuth`, e adapter de OAuth de assinatura em cliente de
terceiro é área cinzenta de ToS — fica fora daqui.

Backend novo no repo só entra com: preflight honesto (declara indisponível em
vez de estourar), `exit_reason` dentro do vocabulário
(`done|max_turns|timeout|error|blocked`) e a mesma unidade de fixture passando
nele.

## Genoma: o que não se muda sem conversa

`config/genome.toml` declara as zonas. PR de fora que toca uma zona
`immutable` — `harness/ruler/**`, `harness/genome/**`, `harness/routing/**`,
`harness/graph/**`, `uv.lock`, `benchmarks/sealed/**` — precisa de discussão
**antes** do código: essas zonas são o que impede o loop de auto-melhoria de
aprovar a si mesmo, e mudar uma delas invalida todo o histórico de A/B do
ledger. Abra uma issue com a hipótese e o que ela quebra.

Zona mutável (`config/*.toml`, `prompts/**`) é onde calibração é bem-vinda —
inclusive vinda do próprio loop. Mudança de knob sem número que a sustente,
porém, é achismo com diff: rode `harness ab` e cole o veredito.

## Estilo

Siga o arquivo que você está editando. O repo é consistente em:

- docstrings e comentários em pt-BR, explicando **por quê**, não o quê;
- `dataclass(frozen=True)` para tipo de dado; `Protocol` para contrato;
- `from __future__ import annotations` no topo;
- vocabulário fechado (constante nomeada) em vez de string solta para
  `exit_reason`, motivo de escalação e afins;
- falhar fechado e ruidoso: config contraditória não vira default silencioso.
