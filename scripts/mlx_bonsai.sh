#!/usr/bin/env bash
# Sobe mlx_lm.server com Ternary-Bonsai-27B (2bit) na porta 1235.
# NÃO use Bonsai-27B-mlx-1bit — mlx vanilla não carrega 1bit.
# mlx-lm é externo ao uv.lock (genoma imutável) — instale no sistema:
#   pip install mlx-lm
# ou: uv tool install mlx-lm
set -euo pipefail

PORT="${HARNESS_MLX_PORT:-1235}"
DEFAULT_MODEL="${HOME}/.lmstudio/models/prism-ml/Ternary-Bonsai-27B-mlx-2bit"
MODEL="${HARNESS_MLX_MODEL:-$DEFAULT_MODEL}"

if [[ ! -d "$MODEL" ]]; then
  echo "modelo não encontrado: $MODEL" >&2
  echo "defina HARNESS_MLX_MODEL=/caminho/para/Ternary-Bonsai-27B-mlx-2bit" >&2
  exit 1
fi

if [[ "$MODEL" == *"-1bit"* ]] || [[ "$MODEL" == *"Bonsai-27B-mlx-1bit"* ]]; then
  echo "recusado: $MODEL é 1bit — use Ternary-Bonsai-27B-mlx-2bit" >&2
  exit 1
fi

if ! command -v mlx_lm.server >/dev/null 2>&1; then
  echo "mlx_lm.server não está no PATH — instale mlx-lm fora do repo (pip/uv tool)" >&2
  exit 1
fi

echo "mlx_lm.server --model $MODEL --port $PORT"
exec mlx_lm.server --model "$MODEL" --port "$PORT"
