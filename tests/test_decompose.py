"""`harness decompose`: quebra de task grande com o backend MOCKADO.

Nenhum teste gasta chamada paga — `_call_planner` (a única fronteira com o
backend) é trocado por monkeypatch devolvendo JSON fixo.

O gate (`plan_gate`) roda de verdade nestes testes: ele não chama backend, só
provisiona um workspace descartável do repo do fixture e roda os `verify_cmd`.
Por isso todo plano daqui tem `checks`/`files` e réguas VERMELHAS no fixture.
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
        "prompt_md": '# Passo 1\n\nAdicione `<button id="tema">` em `index.html`.',
        "verify_cmd": "grep -q 'id=\"tema\"' index.html || { echo 'falta o botao'; exit 1; }",
        "kind": "code",
        "files": ["index.html"],
        "deps": [],
        "checks": [{"name": "tem-button", "cmd": "grep -q '<button' index.html", "weight": 1.0}],
    },
    {
        "id_slug": "css-vars",
        "prompt_md": "# Passo 2\n\nO passo 1 já criou o botão. Agora crie `style.css`.",
        "verify_cmd": "test -f style.css || { echo 'falta style.css'; exit 1; }",
        "kind": "code",
        "files": ["style.css"],
        "deps": ["botao-tema"],
        "checks": [{"name": "tem-css", "cmd": "test -s style.css", "weight": 2.0}],
    },
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    # O gate provisiona workspace descartável: a raiz tem que ser a do tmp.
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Site\nSite da oficina.\n")
    (repo / "index.html").write_text("<h1>oi</h1>\n")
    projects_file = tmp_path / "projects.toml"
    projects_file.write_text(f'[projects.oficina]\nrepo = "{repo}"\n')
    queue = tmp_path / "queue"

    def fake_call(prompt: str, backend: str, model, max_usd: float, adapter=None) -> str:
        fake_call.prompts.append(prompt)
        payload = fake_call.payload
        # Lista = uma resposta por chamada (o gate vale UMA re-chamada).
        return payload.pop(0) if isinstance(payload, list) else payload

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
        [dict(PLAN[0], id_slug="passo-c"), dict(PLAN[1], id_slug="passo-d", deps=["passo-c"])]
    )
    again = apply_decompose(_propose(env, "outra tarefa"))
    assert [p.name for p in again] == ["03_passo-c", "04_passo-d"]


@pytest.mark.parametrize(
    "payload",
    [
        "desculpa, não consegui",  # sem array
        json.dumps([PLAN[0]]),  # uma unit não é decomposição
        json.dumps([PLAN[0], dict(PLAN[1], verify_cmd="true")]),  # régua trivial
        json.dumps([PLAN[0], dict(PLAN[1], id_slug="botao-tema")]),  # slug repetido
    ],
)
def test_plano_torto_devolve_none_sem_escrever(env, payload):
    env.fake.payload = payload

    assert _propose(env) is None
    assert not env.queue.exists()


VERDE = json.dumps(
    [
        dict(PLAN[0], verify_cmd="test -f README.md || { echo falta; exit 1; }"),
        PLAN[1],
    ],
    ensure_ascii=False,
)


def test_gate_reprovado_vale_uma_rechamada_com_os_motivos(env, capsys):
    """Régua que já passa no HEAD volta ao planejador COM o motivo — e a segunda
    resposta, agora vermelha, é aceita."""
    env.fake.payload = [VERDE, json.dumps(PLAN, ensure_ascii=False)]

    proposal = _propose(env)

    assert proposal is not None
    assert len(env.fake.prompts) == 2
    assert "REPROVADO" in env.fake.prompts[1]
    assert "JÁ PASSA" in env.fake.prompts[1]
    assert "gate reprovou" in capsys.readouterr().err


def test_gate_reprovado_duas_vezes_devolve_none_e_manda_escalar(env, capsys):
    env.fake.payload = [VERDE, VERDE]

    assert _propose(env) is None
    assert len(env.fake.prompts) == 2  # UMA re-chamada, não um laço
    assert "escale" in capsys.readouterr().err
    assert not env.queue.exists()


def test_prompt_de_autoria_ensina_a_convencao_de_dist_e_tela_nao_vazia():
    """A fila da bancada travou porque o planejador autorava `href="dist/css/..."`
    dentro de `dist/index.html` — 404 no gate de tela, screenshot em branco. A
    convenção mora no prompt, então ela é testada aqui."""
    prompt = dec.planning_prompt("faz o site", "oficina", "README.md", 5)

    assert "`dist/` é a RAIZ servida" in prompt
    assert "css/style.css" in prompt
    assert "NUNCA escreva o prefixo `dist/` em `href`, `src` ou `fetch()`" in prompt
    assert "conteúdo visível renderizado" in prompt
    assert "reprova página vazia" in prompt


def test_planner_local_usa_deepagents_com_adapter_de_raciocinio():
    assert dec.planner_backend("remote") == (dec.DECOMPOSE_BACKEND, None)
    assert dec.planner_backend("local") == ("deepagents", "reasoning")
    with pytest.raises(DecomposeError):
        dec.planner_backend("telepatia")


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
