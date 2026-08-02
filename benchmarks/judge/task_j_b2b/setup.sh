#!/usr/bin/env bash
# setup.sh — provisiona fixtures/ com o schwifty no base_sha (juiz j_b2b) E
# o ambiente Python pra rodar os testes (judges/_env/j_b2b/).
#
# Clona o upstream, fixa no commit anterior ao bug fix (registry.tsv),
# remove o .git (o agente não pode "trapacear" olhando o histórico/tags
# nem farejar o fix_sha), roda um scan de segredo simples e aborta se o
# sha do checkout divergir do registry. Idempotente: rodar de novo com
# fixtures/ já correto não reclona (e fica offline nesse caso).
#
# O ambiente Python é responsabilidade do juiz, não do agente: o agente só
# corrige o bug no workspace, não precisa criar venv nem instalar nada.
# judges/_env/j_b2b/ é um venv COMPARTILHADO (fora do workspace de qualquer
# run), criado uma vez, com pytest + as deps de runtime do schwifty (puro
# Python — não precisa instalar o pacote em si, só rodar com PYTHONPATH
# apontando pro workspace). Ver verify.py.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../../.." && pwd)"
FIXTURES="$HERE/fixtures"
REGISTRY="$REPO_ROOT/judges/registry.tsv"
JUDGE_ID="j_b2b"
MARKER="$HERE/.setup_base_sha"
ENV_DIR="$REPO_ROOT/judges/_env/j_b2b"
ENV_MARKER="$ENV_DIR/.setup_ok"

setup_env() {
    # idempotente: já tem o marker -> venv pronto, nada a fazer.
    if [ -f "$ENV_MARKER" ]; then
        return 0
    fi
    rm -rf "$ENV_DIR"
    python3 -m venv "$ENV_DIR"
    "$ENV_DIR/bin/pip" install --quiet --upgrade pip
    "$ENV_DIR/bin/pip" install --quiet pytest pydantic
    # instala o schwifty (deste FIXTURES, no base_sha) em modo editável só
    # pra registrar a metadata do pacote (schwifty/__init__.py lê a própria
    # versão via importlib.metadata) e puxar as deps de runtime do
    # pyproject.toml (pycountry, rstr, ...) de uma vez, sem hardcodar lista.
    # Não trava o código importado num path fixo: verify.py roda com
    # PYTHONPATH=<workspace>, que entra ANTES do site-packages no sys.path
    # — "import schwifty" acha o código do workspace da run, a metadata
    # (versão) vem do registro deste install. hatch-vcs precisa de
    # SETUPTOOLS_SCM_PRETEND_VERSION porque fixtures/ não tem .git.
    SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0 "$ENV_DIR/bin/pip" install --quiet -e "$FIXTURES"
    touch "$ENV_MARKER"
}

if [ ! -f "$REGISTRY" ]; then
    echo "registry.tsv não encontrado: $REGISTRY" >&2
    exit 1
fi

ROW="$(awk -F'\t' -v id="$JUDGE_ID" '$1 == id { print }' "$REGISTRY")"
if [ -z "$ROW" ]; then
    echo "$JUDGE_ID não encontrado em $REGISTRY" >&2
    exit 1
fi
UPSTREAM_URL="$(echo "$ROW" | cut -f2)"
BASE_SHA="$(echo "$ROW" | cut -f3)"

scan_secrets() {
    # scan simples: chaves privadas, padrão de AWS access key, .env versionado.
    local hits
    hits="$(grep -RIl \
        -e 'BEGIN RSA PRIVATE KEY' \
        -e 'BEGIN OPENSSH PRIVATE KEY' \
        -e 'BEGIN PRIVATE KEY' \
        -e 'AKIA[0-9A-Z]\{16\}' \
        "$FIXTURES" 2>/dev/null || true)"
    if [ -f "$FIXTURES/.env" ]; then
        hits="$hits
$FIXTURES/.env"
    fi
    if [ -n "$hits" ]; then
        echo "scan de segredo encontrou possíveis segredos, abortando:" >&2
        echo "$hits" >&2
        exit 1
    fi
}

# idempotência: fixtures já provisionadas no sha certo, sem .git nem venv -> não reclona.
if [ -f "$MARKER" ] && [ "$(cat "$MARKER")" = "$BASE_SHA" ] && [ -f "$FIXTURES/pyproject.toml" ] \
   && [ ! -d "$FIXTURES/.git" ] && [ ! -d "$FIXTURES/.venv" ] && [ ! -d "$FIXTURES/venv" ]; then
    scan_secrets
    setup_env
    echo "fixtures/ já provisionadas em $BASE_SHA (idempotente, sem reclone)."
    exit 0
fi

rm -rf "$FIXTURES"
mkdir -p "$FIXTURES"

TMP_CLONE="$(mktemp -d)"
trap 'rm -rf "$TMP_CLONE"' EXIT

git clone --quiet "$UPSTREAM_URL" "$TMP_CLONE"
git -C "$TMP_CLONE" checkout --quiet "$BASE_SHA"

ACTUAL_SHA="$(git -C "$TMP_CLONE" rev-parse HEAD)"
if [ "$ACTUAL_SHA" != "$BASE_SHA" ]; then
    echo "sha divergente do registry: esperado $BASE_SHA, obtido $ACTUAL_SHA" >&2
    rm -rf "$FIXTURES"
    exit 1
fi

rm -rf "$TMP_CLONE/.git"
cp -R "$TMP_CLONE/." "$FIXTURES/"

# defesa em profundidade: fixtures/ nunca deve ter .venv/venv — o ambiente
# de teste é judges/_env/j_b2b/, fora do workspace, não algo que viva nas
# fixtures. Se algo vazar aqui (upstream com venv commitado, cópia manual),
# apaga antes de seguir.
rm -rf "$FIXTURES/.venv" "$FIXTURES/venv"

scan_secrets
setup_env

echo "$BASE_SHA" > "$MARKER"
echo "fixtures/ provisionadas em $BASE_SHA ($UPSTREAM_URL), .git removido, scan de segredo ok."
