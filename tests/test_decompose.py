"""`harness decompose`: quebra de task grande com o backend MOCKADO.

Nenhum teste gasta chamada paga — `_call_planner` (a única fronteira com o
backend) é trocado por monkeypatch devolvendo JSON fixo.
"""

from __future__ import annotations

import json
import tomllib

import pytest

from harness.cli import load_unit
from harness.improve import decompose as dec
from harness.improve.decompose import DecomposeError, apply_decompose, propose_decompose

PLAN = [
    {
        "id_slug": "botao-tema",
        "prompt_md": "# Passo 1\n\nAdicione `<button id=\"tema\">` em `index.html`.",
        "verify_cmd": "grep -q 'id=\"tema\"' index.html || { echo 'falta o botao'; exit 1; }",
        "kind": "code",
    },
    {
        "id_slug": "css-vars",
        "prompt_md": "# Passo 2\n\nO passo 1 já criou o botão. Agora crie `style.css`.",
        "verify_cmd": "test -f style.css || { echo 'falta style.css'; exit 1; }",
        "kind": "code",
    },
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Site\nSite da oficina.\n")
    (repo / "index.html").write_text("<h1>oi</h1>\n")
    projects_file = tmp_path / "projects.toml"
    projects_file.write_text(f'[projects.oficina]\nrepo = "{repo}"\n')
    queue = tmp_path / "queue"

    def fake_call(prompt: str, backend: str, model, max_usd: float) -> str:
        fake_call.prompts.append(prompt)
        return fake_call.payload

    fake_call.prompts = []
    fake_call.payload = json.dumps(PLAN, ensure_ascii=False)
    monkeypatch.setattr(dec, "_call_planner", fake_call)

    class Env:
        pass

    e = Env()
    e.repo, e.projects_file, e.queue, e.fake = repo, projects_file, queue, fake_call
    return e


def _propose(env, task="deixa o site com tema escuro", **kw):
    return propose_decompose(
        task, "oficina", projects_file=env.projects_file, queue_dir=env.queue, **kw
    )


def test_grava_fila_ordenada_e_valida(env):
    created = apply_decompose(_propose(env))

    assert [p.name for p in created] == ["01_botao-tema", "02_css-vars"]
    # a ordem alfabética da fila É a dependência: o driver lê sorted()
    assert sorted(p.name for p in created) == [p.name for p in created]
    for i, path in enumerate(created):
        unit = load_unit(path)  # autochecagem pelo parser oficial
        assert unit.id == path.name
        assert unit.project == "oficina"
        assert unit.prompt == PLAN[i]["prompt_md"]
        assert unit.verify_cmd == PLAN[i]["verify_cmd"]
        assert (path / "prompt.md").read_text().strip() == PLAN[i]["prompt_md"]
        origin = tomllib.loads((path / "unit.toml").read_text())["origin"]
        assert origin["task"] == "deixa o site com tema escuro"
        assert origin["step"] == f"{i + 1}/2"
    # segunda decomposição entra DEPOIS da fila pendente, não colide com 01/02
    env.fake.payload = json.dumps(
        [dict(PLAN[0], id_slug="passo-c"), dict(PLAN[1], id_slug="passo-d")]
    )
    again = apply_decompose(_propose(env, "outra tarefa"))
    assert [p.name for p in again] == ["03_passo-c", "04_passo-d"]


@pytest.mark.parametrize(
    "payload",
    [
        "desculpa, não consegui",                      # sem array
        json.dumps([PLAN[0]]),                         # uma unit não é decomposição
        json.dumps([PLAN[0], dict(PLAN[1], verify_cmd="true")]),  # régua trivial
        json.dumps([PLAN[0], dict(PLAN[1], id_slug="botao-tema")]),  # slug repetido
    ],
)
def test_plano_torto_devolve_none_sem_escrever(env, payload):
    env.fake.payload = payload

    assert _propose(env) is None
    assert not env.queue.exists()


def test_apply_e_tudo_ou_nada(env):
    proposal = _propose(env, start=1)
    # o segundo destino já existe: o LOTE cai, e o primeiro passo NÃO fica
    # gravado — fila meio escrita faria o driver rodar o passo 1 de um plano
    # que não existe.
    (env.queue / "02_css-vars").mkdir(parents=True)

    with pytest.raises(DecomposeError):
        apply_decompose(proposal)

    assert sorted(p.name for p in env.queue.iterdir()) == ["02_css-vars"]
    assert not (env.queue / "02_css-vars" / "unit.toml").exists()
    assert not (env.queue / "01_botao-tema").exists()
