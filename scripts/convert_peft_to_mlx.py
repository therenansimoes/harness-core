#!/usr/bin/env python3
"""Convert a PEFT (HuggingFace) LoRA adapter into the layout mlx_lm expects.

mlx_lm's ``load_adapters`` (mlx_lm/tuner/utils.py) reads ``adapter_config.json``
into a ``SimpleNamespace`` and requires ``num_layers`` plus ``lora_parameters``
(rank/scale/dropout, optional ``keys``), then loads ``adapters.safetensors``
with ``model.load_weights(..., strict=False)``.  A PEFT adapter has none of
that, which is why the server dies with::

    'types.SimpleNamespace' object has no attribute 'num_layers'

Two structural differences beyond the config:

* names -- PEFT uses ``base_model.model.<hf path>.lora_{A,B}.weight``; mlx_lm
  uses the module path of the *loaded mlx model* plus ``.lora_a``/``.lora_b``.
  The mlx path is discovered by introspecting the base model (lazily loaded),
  never guessed, since wrappers differ per architecture (Qwen3.5 sits under
  ``language_model.model.layers.N``, Llama under ``model.layers.N``).
* layout -- ``LoRALinear`` computes ``(x @ lora_a) @ lora_b`` with
  ``lora_a: [in, r]`` and ``lora_b: [r, out]``; PEFT stores
  ``lora_A: [r, in]`` and ``lora_B: [out, r]``.  Both get transposed.

Usage:
    convert_peft_to_mlx.py <peft_dir> <out_dir> [--base-model REPO_OR_PATH]
"""

import argparse
import json
import math
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

import mlx.core as mx
import mlx.nn as nn
from mlx_lm.utils import _download, load_model

PEFT_KEY_RE = re.compile(r"^base_model\.model\.(?P<path>.+)\.lora_(?P<ab>[AB])\.weight$")
LAYER_RE = re.compile(r"^(?P<head>.*)\.layers\.(?P<idx>\d+)\.(?P<suffix>.+)$")


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def load_peft(peft_dir: Path):
    cfg_path = peft_dir / "adapter_config.json"
    if not cfg_path.exists():
        die(f"no adapter_config.json in {peft_dir}")
    cfg = json.loads(cfg_path.read_text())
    if cfg.get("peft_type") != "LORA":
        die(f"unsupported peft_type: {cfg.get('peft_type')}")
    for unsupported in ("use_dora", "use_qalora", "fan_in_fan_out", "lora_bias"):
        if cfg.get(unsupported):
            die(f"{unsupported}=True is not representable in the mlx_lm adapter format")
    if cfg.get("rank_pattern") or cfg.get("alpha_pattern"):
        die("per-layer rank_pattern/alpha_pattern have no mlx_lm equivalent")
    if cfg.get("modules_to_save"):
        die(f"modules_to_save={cfg['modules_to_save']} is not supported")
    weights_path = peft_dir / "adapter_model.safetensors"
    if not weights_path.exists():
        die(f"no adapter_model.safetensors in {peft_dir}")
    return cfg, mx.load(str(weights_path))


def mlx_module_index(model):
    """Map module path -> module, plus the full path of each transformer block."""
    modules = dict(model.named_modules())
    layer_paths = {}
    by_id = {id(layer): i for i, layer in enumerate(model.layers)}
    for path, mod in modules.items():
        i = by_id.get(id(mod))
        if i is not None:
            layer_paths[i] = path
    if len(layer_paths) != len(model.layers):
        die("could not locate every transformer block in the mlx model tree")
    return modules, layer_paths


def linear_dims(mod):
    """(output_dims, input_dims) for a (possibly quantized) linear layer."""
    out_dims, in_dims = mod.weight.shape
    if isinstance(mod, nn.QuantizedLinear):
        in_dims = in_dims * 32 // mod.bits
    return out_dims, in_dims


def convert(peft_dir: Path, out_dir: Path, base_model: str):
    cfg, peft_weights = load_peft(peft_dir)
    base_model = base_model or cfg.get("base_model_name_or_path")
    if not base_model:
        die("no --base-model and no base_model_name_or_path in the PEFT config")

    print(f"base model: {base_model} (lazy load for naming/shape checks)")
    model, _ = load_model(_download(base_model), lazy=True)
    modules, layer_paths = mlx_module_index(model)
    n_blocks = len(layer_paths)

    r = cfg["r"]
    alpha = cfg["lora_alpha"]
    scale = alpha / math.sqrt(r) if cfg.get("use_rslora") else alpha / r

    # group PEFT tensors by (layer index, module suffix)
    pairs = defaultdict(dict)
    skipped = []
    for key, tensor in peft_weights.items():
        m = PEFT_KEY_RE.match(key)
        if not m:
            skipped.append((key, "unrecognized PEFT key shape"))
            continue
        lm = LAYER_RE.match(m["path"])
        if not lm:
            skipped.append((key, "not inside a transformer block"))
            continue
        pairs[(int(lm["idx"]), lm["suffix"])][m["ab"]] = tensor

    out_weights = {}
    suffixes = set()
    covered = set()
    for (idx, suffix), ab in sorted(pairs.items()):
        if set(ab) != {"A", "B"}:
            skipped.append((f"layers.{idx}.{suffix}", "missing lora_A or lora_B"))
            continue
        if idx not in layer_paths:
            skipped.append((f"layers.{idx}.{suffix}", "layer absent from the mlx model"))
            continue
        target = f"{layer_paths[idx]}.{suffix}"
        mod = modules.get(target)
        if mod is None:
            skipped.append((target, "module absent from the mlx model"))
            continue
        if not isinstance(mod, (nn.Linear, nn.QuantizedLinear)):
            skipped.append((target, f"{type(mod).__name__} is not a linear layer"))
            continue

        lora_a = ab["A"].T  # [r, in] -> [in, r]
        lora_b = ab["B"].T  # [out, r] -> [r, out]
        out_dims, in_dims = linear_dims(mod)
        if lora_a.shape != (in_dims, r) or lora_b.shape != (r, out_dims):
            die(
                f"{target}: shape mismatch, adapter gives "
                f"{lora_a.shape}/{lora_b.shape}, model wants "
                f"{(in_dims, r)}/{(r, out_dims)}"
            )
        out_weights[f"{target}.lora_a"] = lora_a.astype(mx.float32)
        out_weights[f"{target}.lora_b"] = lora_b.astype(mx.float32)
        suffixes.add(suffix)
        covered.add(idx)

    if not out_weights:
        die("nothing convertible was found")

    # mlx_lm applies LoRA to the LAST num_layers blocks; a block missing one of
    # the target modules is simply skipped there, so gaps are harmless, but a
    # covered block below the cut would silently lose its adapter.
    num_layers = n_blocks - min(covered)
    expected = set(range(min(covered), n_blocks))
    gaps = sorted(expected - covered)

    out_dir.mkdir(parents=True, exist_ok=True)
    mx.save_safetensors(str(out_dir / "adapters.safetensors"), out_weights)
    (out_dir / "adapter_config.json").write_text(
        json.dumps(
            {
                "fine_tune_type": "lora",
                "num_layers": num_layers,
                "lora_parameters": {
                    "rank": r,
                    "dropout": 0.0,
                    "scale": scale,
                    "keys": sorted(suffixes),
                },
                "model": base_model,
                "converted_from": str(peft_dir),
            },
            indent=4,
        )
        + "\n"
    )
    for extra in ("chat_template.jinja", "tokenizer_config.json"):
        src = peft_dir / extra
        if src.exists():
            shutil.copyfile(src, out_dir / extra)

    print(f"tensors: {len(out_weights)} ({len(out_weights) // 2} modules)")
    print(f"rank={r} alpha={alpha} scale={scale}")
    print(f"num_layers={num_layers} of {n_blocks} blocks, covered={min(covered)}..{max(covered)}")
    print(f"keys={sorted(suffixes)}")
    if gaps:
        print(f"WARNING: blocks in range without adapter weights: {gaps}")
        print("  -> mlx_lm will attach zero-initialized (no-op) LoRA there")
    for key, why in skipped:
        print(f"skipped {key}: {why}")
    print(f"wrote {out_dir}")


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("peft_dir", type=Path)
    p.add_argument("out_dir", type=Path)
    p.add_argument(
        "--base-model",
        help="mlx base model repo/path; defaults to the PEFT base_model_name_or_path",
    )
    args = p.parse_args()
    convert(args.peft_dir, args.out_dir, args.base_model)


if __name__ == "__main__":
    main()
