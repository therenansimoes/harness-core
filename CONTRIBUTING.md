# CONTRIBUTING

Read `docs/ARCHITECTURE.md` before your first PR. General rule: small PRs, with
an executable acceptance criterion — "it got better" without a command that
proves it does not land.

## Running the tests

```bash
uv sync --extra deepagents
uv run --extra deepagents pytest -q     # 726 passed, 2 deselected
```

Without the extra (`uv sync && uv run pytest -q`) the suite is green too: the
tests that import `deepagents` skip when the library is not installed.
Everything else runs on the `mock` backend, which is deterministic and touches
no network.

Tests do not write into the repo. Isolation is by env var, with `tmp_path`:

```python
@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"
```

Same convention for `HARNESS_CONFIG_DIR`, `HARNESS_ROOT` and
`HARNESS_PROJECTS_ROOT`. A test that depends on the repo's `config/` is a test
that breaks the moment the loop calibrates a TOML.

## The `ollama` and `claude_cli` markers

Two markers flag what needs a real machine, and both are **off by default**
(`addopts = "-m 'not ollama and not claude_cli'"` in `pyproject.toml`):

- `ollama` — requires a local Ollama server running. Costs $0, but depends on
  which model you have installed.
- `claude_cli` — requires the official CLI installed and authenticated. **Spends
  money.**

```bash
uv run --extra deepagents pytest -m ollama -q      # explicit opt-in
```

A test that calls a real model without one of those markers is a broken test:
the default suite has to run on any machine, offline, for free. And the hardware
ceiling is a rule, not a suggestion — on this machine (18GB), a local model
above ~8B does not belong in any tier.

## Plugging in a backend

Three methods (`harness/backends/base.py`): `capabilities()`, `preflight()` and
`execute(ExecRequest) -> ExecResult`. `preflight()` is deterministic and **makes
no LLM call** — that is what makes `harness backends` cheap to run at any time.

A third-party backend needs no PR here: publish a package that announces itself
on the entry point.

```toml
[project.entry-points."harness.backends"]
my_backend = "my_package.backend:MyBackend"
```

In tests (or for a plugin that is not installed), use
`registry.register(name, factory)`. Auth follows the same design on the
`harness.auth` entry point (`env()` + `check()`); the repo ships only `NullAuth`,
and an OAuth adapter for someone else's subscription client is a ToS grey area —
it stays out of here.

A new backend inside this repo only lands with: an honest preflight (declares
itself unavailable instead of blowing up), an `exit_reason` from the closed
vocabulary (`done|max_turns|timeout|error|blocked`), and the same fixture unit
passing on it.

## Genome: what you do not change without a conversation

`config/genome.toml` declares the zones. An outside PR that touches an
`immutable` zone — `harness/ruler/**`, `harness/genome/**`, `harness/routing/**`,
`harness/graph/**`, `uv.lock`, `benchmarks/sealed/**` — needs discussion
**before** the code: those zones are what stops the self-improvement loop from
approving itself, and changing one of them invalidates the ledger's entire A/B
history. Open an issue with the hypothesis and what it breaks.

The mutable zone (`config/*.toml`, `prompts/**`) is where calibration is
welcome — including calibration coming from the loop itself. A knob change with
no number behind it, though, is a hunch with a diff: run `harness ab` and paste
the verdict.

## Style

Follow the file you are editing. The repo is consistent about:

- docstrings and comments in pt-BR, explaining **why**, not what;
- `dataclass(frozen=True)` for data types; `Protocol` for contracts;
- `from __future__ import annotations` at the top;
- a closed vocabulary (a named constant) instead of a loose string for
  `exit_reason`, escalation reasons and the like;
- fail closed and loud: a contradictory config does not silently become a
  default.
