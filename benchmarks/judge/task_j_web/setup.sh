#!/usr/bin/env bash
# setup.sh — provisiona fixtures/ com o nanostores no base_sha (juiz j_web).
#
# Clona o upstream, fixa no commit anterior ao bug fix (registry.tsv),
# remove o .git (o agente não pode "trapacear" olhando o histórico/tags
# nem farejar o fix_sha), roda um scan de segredo simples e aborta se o
# sha do checkout divergir do registry. Idempotente: rodar de novo com
# fixtures/ já correto e node_modules/ instalado não reclona nem reinstala
# (fica offline nesse caso).
#
# Diferente do j_b2b (Python, venv compartilhado fora do workspace via
# PYTHONPATH): node resolve módulos por node_modules/ dentro do próprio
# diretório do projeto, não existe um equivalente direto a PYTHONPATH
# apontando pra fora. Por isso o ambiente aqui (node_modules/, via
# `corepack pnpm install`) é provisionado DENTRO de fixtures/ mesmo — o
# workspace de cada run é uma cópia de fixtures/ e carrega node_modules/
# junto. fixtures/ é gitignored (benchmarks/judge/task_*/fixtures/), então
# isso não infla o repositório.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../../.." && pwd)"
FIXTURES="$HERE/fixtures"
REGISTRY="$REPO_ROOT/judges/registry.tsv"
JUDGE_ID="j_web"
MARKER="$HERE/.setup_base_sha"

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

install_deps() {
    (cd "$FIXTURES" && corepack pnpm install --offline 2>/dev/null || corepack pnpm install)
}

# idempotência: fixtures já provisionadas no sha certo, sem .git, com
# node_modules instalado (bnt presente) -> não reclona nem reinstala.
if [ -f "$MARKER" ] && [ "$(cat "$MARKER")" = "$BASE_SHA" ] && [ -f "$FIXTURES/package.json" ] \
   && [ ! -d "$FIXTURES/.git" ] && [ -x "$FIXTURES/node_modules/.bin/bnt" ]; then
    scan_secrets
    echo "fixtures/ já provisionadas em $BASE_SHA (idempotente, sem reclone/reinstall)."
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

scan_secrets
install_deps

# node_modules pode conter binários/licenças de terceiros — roda o scan de
# novo depois do install pra cobrir o que veio das deps.
scan_secrets

echo "$BASE_SHA" > "$MARKER"
echo "fixtures/ provisionadas em $BASE_SHA ($UPSTREAM_URL), .git removido, deps instaladas, scan de segredo ok."
