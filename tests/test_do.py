"""`harness do`: o caminho de quem só tem uma pasta e uma frase.

O que estes testes protegem não é o formato da saída, é a promessa: de um
diretório qualquer sai um run com régua determinística, o repo do usuário volta
limpo, e nada do vocabulário interno (unit, kind, tier) vaza para quem não
pediu.
"""

import subprocess
from pathlib import Path

import pytest

from harness import add, cli, do
from harness.ledger import store

MOCK_VERIFY = "test -f mock_output.txt"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def virgem(tmp_path, monkeypatch):
    """Pasta com um arquivo e nenhum git — o ponto de partida do leigo."""
    pasta = tmp_path / "meu-projeto"
    pasta.mkdir()
    (pasta / "target.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(pasta)
    return pasta


# --- detecção de régua ------------------------------------------------------------


def test_detect_verify_pyproject_com_pytest(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'x'\n[tool.pytest.ini_options]\n", encoding="utf-8"
    )
    cmd, motivo = do.detect_verify(tmp_path)
    assert cmd == do.PYTEST_CMD
    assert "pyproject" in motivo


def test_detect_verify_diretorio_tests(tmp_path):
    (tmp_path / "tests").mkdir()
    assert do.detect_verify(tmp_path) == (do.PYTEST_CMD, "tests/")


def test_detect_verify_package_json_com_script_test(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}', encoding="utf-8")
    cmd, motivo = do.detect_verify(tmp_path)
    assert cmd == do.NPM_CMD
    assert "package.json" in motivo


def test_detect_verify_package_json_sem_script_test_nao_conta(tmp_path):
    """`npm test --silent` num package.json sem `scripts.test` falha por motivo
    errado — e régua que falha sempre é tão inútil quanto régua que passa sempre."""
    (tmp_path / "package.json").write_text('{"scripts": {"build": "vite"}}', encoding="utf-8")
    assert do.detect_verify(tmp_path)[0] == do.FALLBACK_CMD


def test_detect_verify_makefile_com_alvo_test(tmp_path):
    (tmp_path / "Makefile").write_text("build:\n\tcc x.c\n\ntest:\n\t./t\n", encoding="utf-8")
    assert do.detect_verify(tmp_path) == (do.MAKE_CMD, "Makefile alvo test")


def test_detect_verify_cargo_e_go(tmp_path):
    rust, go = tmp_path / "rust", tmp_path / "go"
    rust.mkdir()
    go.mkdir()
    (rust / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    (go / "go.mod").write_text("module x\n", encoding="utf-8")
    assert do.detect_verify(rust)[0] == do.CARGO_CMD
    assert do.detect_verify(go)[0] == do.GO_CMD


def test_detect_verify_fallback_passa_na_validacao_do_add(tmp_path):
    from harness.add import validate_verify_cmd

    cmd, motivo = do.detect_verify(tmp_path)
    assert cmd == do.FALLBACK_CMD
    assert "nenhuma suíte" in motivo
    validate_verify_cmd(cmd)  # não levanta: a régua fraca ainda é uma régua


def test_pytest_vence_package_json(tmp_path):
    """Repo poliglota: a evidência mais forte (suíte declarada) manda."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}', encoding="utf-8")
    assert do.detect_verify(tmp_path)[0] == do.PYTEST_CMD


# --- repo -------------------------------------------------------------------------


def test_ensure_repo_cria_git_e_commit_em_pasta_virgem(virgem):
    repo, criado = do.ensure_repo(virgem)

    assert criado is True
    assert repo == virgem.resolve()
    assert (repo / ".git").is_dir()
    assert do.GITIGNORE_LINE in (repo / ".gitignore").read_text(encoding="utf-8")
    versionados = subprocess.run(
        ["git", "-C", str(repo), "ls-files"], capture_output=True, text=True, check=True
    ).stdout
    assert "target.py" in versionados


def test_ensure_repo_respeita_repo_existente(virgem):
    _git(virgem, "init", "-q")
    _git(virgem, "add", "-A")
    _git(virgem, "commit", "-q", "-m", "meu commit")

    repo, criado = do.ensure_repo(virgem)

    assert criado is False
    assert not (repo / ".gitignore").exists()  # arquivo do usuário não é nosso
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "--oneline"], capture_output=True, text=True, check=True
    ).stdout
    assert log.count("\n") == 1 and "meu commit" in log


def test_ensure_repo_commita_repo_sem_nenhum_commit(virgem):
    """`git init` sem commit não tem HEAD, e o worktree do run sai de HEAD."""
    _git(virgem, "init", "-q")

    repo, criado = do.ensure_repo(virgem)

    assert criado is False
    assert do._has_commit(repo)


def test_ensure_repo_encontra_a_raiz_de_um_subdiretorio(virgem):
    _git(virgem, "init", "-q")
    _git(virgem, "add", "-A")
    _git(virgem, "commit", "-q", "-m", "raiz")
    sub = virgem / "src" / "deep"
    sub.mkdir(parents=True)

    repo, criado = do.ensure_repo(sub)

    assert (repo, criado) == (virgem.resolve(), False)


# --- unidade ----------------------------------------------------------------------


def test_write_unit_carrega_no_load_unit_com_project(virgem, tmp_path, monkeypatch):
    from harness.projects import init_project

    repo, _ = do.ensure_repo(virgem)
    init_project(repo, "meu-projeto")
    unit_dir = do.write_unit(repo, "do-x-abc123", "conserta o bug", MOCK_VERIFY, "meu-projeto")

    unit = cli.load_unit(unit_dir)

    assert unit_dir == repo / do.UNITS_REL / "do-x-abc123"
    assert unit.id == "do-x-abc123"
    assert unit.project == "meu-projeto"
    assert unit.prompt == "conserta o bug"
    assert unit.verify_cmd == MOCK_VERIFY


def test_write_unit_preserva_pedido_com_aspas_e_quebra(virgem):
    from harness.projects import init_project

    repo, _ = do.ensure_repo(virgem)
    init_project(repo, "meu-projeto")
    pedido = "troca '''isto''' por \"aquilo\"\ne roda de novo\\"

    unit_dir = do.write_unit(repo, "do-y-abc123", pedido, MOCK_VERIFY, "meu-projeto")

    assert cli.load_unit(unit_dir).prompt == pedido


def test_prompt_da_unidade_proibe_arquivo_de_anotacao():
    """Numa tarefa grande o agente deixou `COMMIT_INSTRUCTIONS.txt` e
    `DASHBOARD_CHANGES.md` na raiz e o integrate commitou os dois no branch
    default. A convenção mora no prompt, então ela é testada aqui."""
    prompt = do.unit_prompt("conserta o bug em target.py")

    assert prompt.startswith("conserta o bug em target.py")
    assert "Entregue SÓ os arquivos do produto" in prompt
    assert "NÃO crie arquivo de anotação, resumo, instrução ou plano" in prompt
    assert "COMMIT_*" in prompt
    assert "vai na sua RESPOSTA, não em arquivo" in prompt


def test_new_unit_id_casa_com_o_slug_do_add():
    from harness.add import SLUG_RE

    gerado = do.new_unit_id("Conserta o BUG em ação/target.py!!")

    assert SLUG_RE.fullmatch(gerado)
    assert gerado.startswith("do-conserta-o-bug-em-acao")
    assert do.new_unit_id("x") != do.new_unit_id("x")  # dois pedidos iguais coexistem


def test_project_name_desempata_por_hash_do_path(tmp_path, virgem):
    from harness.projects import init_project

    outro = tmp_path / "outro" / "meu-projeto"
    outro.mkdir(parents=True)
    _git(outro, "init", "-q")
    (outro / "a.txt").write_text("a", encoding="utf-8")
    _git(outro, "add", "-A")
    _git(outro, "commit", "-q", "-m", "x")
    init_project(outro, "meu-projeto")

    repo, _ = do.ensure_repo(virgem)

    assert do.project_name(outro) == "meu-projeto"
    assert do.project_name(repo).startswith("meu-projeto-")


def test_ensure_project_reusa_o_registro_do_mesmo_repo_com_outro_nome(virgem, capsys):
    """O bug de quem já tinha projeto: `/…/bancada-app` estava registrado como
    `bancada`, e rodar `harness do` de dentro da pasta abria uma SEGUNDA entrada
    para o mesmo repo — fila, histórico e custo partidos entre dois nomes."""
    from harness.projects import init_project, load_projects

    repo, _ = do.ensure_repo(virgem)
    init_project(repo, "bancada")

    nome = do.ensure_project(repo)

    assert nome == "bancada"
    assert list(load_projects()) == ["bancada"]
    assert "usando projeto já registrado: bancada" in capsys.readouterr().out


# --- cmd_do -----------------------------------------------------------------------


def test_dry_run_mostra_regua_e_rota_sem_executar(virgem, capsys):
    (virgem / "tests").mkdir()

    rc = cli.main(["do", "conserta o bug em target.py", "--dry-run"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "pedido" in out and "conserta o bug em target.py" in out
    assert do.PYTEST_CMD in out and "detectado: tests/" in out
    assert "rota" in out and "auto →" in out
    # O router consulta o ledger para escolher a rota (e por isso o banco nasce),
    # mas nada foi executado: nenhuma linha de run.
    assert store.history() == []


def test_verify_cmd_invalido_sai_2_com_a_mensagem_do_add(virgem, capsys):
    rc = cli.main(["do", "faz algo", "--verify-cmd", "true"])

    assert rc == 2
    assert "trivial" in capsys.readouterr().err


def test_do_mock_grava_no_ledger_do_home_e_deixa_o_repo_limpo(virgem, capsys, harness_home):
    rc = cli.main(
        [
            "do",
            "escreve o mock",
            "--backend",
            "mock",
            "--route",
            "manual",
            "--verify-cmd",
            MOCK_VERIFY,
        ]
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "ACEITO" in out

    # Ledger no home, e não em nenhum canto do repo do usuário.
    assert store.db_path() == harness_home / "data" / "runs.sqlite"
    linhas = store.history()
    assert len(linhas) == 1 and linhas[0].ok is True
    assert not (virgem / "data").exists()

    # Repo do usuário: limpo, com o merge da entrega e sem resto de unidade.
    sujo = subprocess.run(
        ["git", "-C", str(virgem), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert sujo == ""
    assert not (virgem / ".harness" / "units").exists()
    assert (virgem / "mock_output.txt").is_file()  # o trabalho chegou no repo


def test_keep_unit_deixa_a_unidade_no_lugar(virgem):
    rc = cli.main(
        [
            "do",
            "escreve o mock",
            "--backend",
            "mock",
            "--route",
            "manual",
            "--verify-cmd",
            MOCK_VERIFY,
            "--keep-unit",
        ]
    )

    assert rc == 0
    unidades = list((virgem / ".harness" / "units").iterdir())
    assert len(unidades) == 1 and (unidades[0] / "unit.toml").is_file()
    # A convenção de entrega chega ao executor pelo prompt da unidade gravada.
    assert "COMMIT_*" in (unidades[0] / "unit.toml").read_text(encoding="utf-8")


def test_no_apply_deixa_o_resultado_na_branch(virgem, capsys):
    rc = cli.main(
        [
            "do",
            "escreve o mock",
            "--backend",
            "mock",
            "--route",
            "manual",
            "--verify-cmd",
            MOCK_VERIFY,
            "--no-apply",
        ]
    )

    assert rc == 0
    assert "--no-apply" in capsys.readouterr().out
    assert not (virgem / "mock_output.txt").exists()


def test_do_verify_vermelho_nao_aplica_nada(virgem, capsys):
    rc = cli.main(
        [
            "do",
            "escreve o mock",
            "--backend",
            "mock",
            "--route",
            "manual",
            "--verify-cmd",
            "test -f arquivo_que_nunca_existe.txt",
        ]
    )

    saida = capsys.readouterr()
    assert rc == 1
    assert "NÃO ACEITO" in saida.out
    assert "nada foi aplicado no seu repo" in saida.err
    assert not (virgem / "mock_output.txt").exists()


def test_ui_gruda_o_ui_verify_na_regua(virgem, capsys):
    rc = cli.main(["do", "faz a home", "--ui", "--dry-run"])

    assert rc == 0
    assert add.UI_VERIFY_SUFFIX.strip() in capsys.readouterr().out


def test_ui_nao_duplica_o_ui_verify(virgem, capsys):
    rc = cli.main(
        [
            "do",
            "faz a home",
            "--verify-cmd",
            "npm run build && harness ui-verify dist --expect-asset css",
            "--ui",
            "--dry-run",
        ]
    )

    assert rc == 0
    assert capsys.readouterr().out.count("ui-verify") == 1


def test_plano_explica_o_criterio_nos_dois_lados():
    from harness.graph.run_graph import PLAN_PROMPT_CHARS

    sim = cli._plano_linhas(True, "faz a home", "meu-projeto")
    nao = cli._plano_linhas(False, "faz a home", "meu-projeto")

    assert any("mandou o agente planejar" in linha for linha in sim)
    assert any(str(PLAN_PROMPT_CHARS) in linha for linha in nao)
    assert any("refatorar" in linha for linha in nao)
    for linhas in (sim, nao):
        assert any("harness decompose" in linha for linha in linhas)
        assert any("--project meu-projeto" in linha for linha in linhas)


def test_do_mock_mostra_etapas_e_o_ponteiro_do_decompose(virgem, capsys, harness_home):
    rc = cli.main(
        [
            "do",
            "escreve o mock",
            "--backend",
            "mock",
            "--route",
            "manual",
            "--verify-cmd",
            MOCK_VERIFY,
        ]
    )

    assert rc == 0
    saida = capsys.readouterr()
    assert "régua rodada" in saida.err
    assert "harness decompose" in saida.out
    # Discriminador do formato do chunk: se `values` chegasse defasado em
    # relação a `updates`, o `record` do último superstep sumiria do estado
    # final e o ledger apareceria como "#None" no relatório.
    assert "#None" not in saida.out


# --- ajuda ------------------------------------------------------------------------


def test_help_do_mostra_as_flags_avancadas(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["do", "--help"])

    out = capsys.readouterr().out
    assert exc.value.code == 0
    assert "--backend" in out
    assert "avançado" in out


def test_help_raiz_separa_basico_de_avancado(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])

    out = capsys.readouterr().out
    assert "BÁSICO" in out and "quickstart · do" in out
    assert "AVANÇADO" in out


def test_quickstart_semeia_config_e_lista_o_que_falta(virgem, capsys, harness_home):
    rc = cli.main(["quickstart"])

    out = capsys.readouterr().out
    assert rc == 0
    assert (harness_home / "config" / "ruler.toml").is_file()
    assert 'harness do "' in out


def test_quickstart_nao_grita_falha_para_pendencia_nao_bloqueante(
    virgem, capsys, harness_home, monkeypatch
):
    from harness import doctor

    monkeypatch.setattr(
        doctor,
        "checks",
        lambda root=None: [
            doctor.Check("backend:x", doctor.FAIL, "sem credencial"),
            doctor.Check("catalog", doctor.WARN, "vazio"),
        ],
    )

    rc = cli.main(["quickstart"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "FALHA" not in out
    assert "nada aqui impede tentar" in out
    assert "backend:x" in out
    assert "catalog" in out


def test_quickstart_mostra_falha_so_para_bloqueador(virgem, capsys, harness_home, monkeypatch):
    from harness import doctor

    monkeypatch.setattr(
        doctor,
        "checks",
        lambda root=None: [doctor.Check("backend:x", doctor.FAIL, "sem credencial")],
    )
    monkeypatch.setattr(cli.shutil, "which", lambda n: None if n == "git" else f"/usr/bin/{n}")

    rc = cli.main(["quickstart"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "o que impede rodar" in out
    linha_git = next(line for line in out.splitlines() if "git" in line and "PATH" in line)
    assert "FALHA" in linha_git
    linha_backend = next(line for line in out.splitlines() if "backend:x" in line)
    assert "aviso" in linha_backend
