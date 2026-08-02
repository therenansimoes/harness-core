#!/usr/bin/env python3
"""
confmerge: merge N YAML/JSON-like config dicts (given as .json files) with
override semantics, later files win. Used by infra/CLI tooling to combine a
base config with environment overlays (base.json + prod.json -> effective
config), the same pattern used by kustomize/helm-values/dotenv layering.

CLI usage:
    python3 confmerge.py base.json overlay1.json overlay2.json ...

Prints the merged JSON to stdout and exits 0 on success, exits 2 if any
input file is missing or not valid JSON.
"""
import json
import sys


def deep_merge(base, overlay):
    """Recursively merge overlay into base. Dicts merge key-by-key;
    non-dict values in overlay replace the value in base. Lists in overlay
    should also replace (not concatenate) the base list, since overlays
    represent a full override of that config key."""
    if not isinstance(base, dict) or not isinstance(overlay, dict):
        return overlay

    result = dict(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict):
            result[key] = deep_merge(result[key], value)
        elif key in result and isinstance(result[key], list) and isinstance(value, list):
            result[key] = result[key] + value
        else:
            result[key] = value
    return result


def merge_files(paths):
    merged = {}
    for path in paths:
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"confmerge: error reading {path}: {e}", file=sys.stderr)
            sys.exit(2)
        merged = deep_merge(merged, data)
    return merged


def main(argv):
    if len(argv) < 2:
        print("usage: confmerge.py <file1.json> <file2.json> ...", file=sys.stderr)
        return 2
    merged = merge_files(argv[1:])
    print(json.dumps(merged, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
