import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
SCRIPT = HERE / "confmerge.py"


def run_cli(*files):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *[str(f) for f in files]],
        capture_output=True, text=True,
    )
    return result


def write_json(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


def test_scalar_override(tmp_path):
    base = write_json(tmp_path, "base.json", {"log_level": "info", "port": 8080})
    overlay = write_json(tmp_path, "prod.json", {"log_level": "warn"})
    result = run_cli(base, overlay)
    assert result.returncode == 0
    merged = json.loads(result.stdout)
    assert merged == {"log_level": "warn", "port": 8080}


def test_nested_dict_deep_merge(tmp_path):
    base = write_json(tmp_path, "base.json", {"db": {"host": "localhost", "port": 5432}})
    overlay = write_json(tmp_path, "prod.json", {"db": {"host": "prod-db"}})
    result = run_cli(base, overlay)
    merged = json.loads(result.stdout)
    assert merged == {"db": {"host": "prod-db", "port": 5432}}


def test_list_is_replaced_not_concatenated(tmp_path):
    """An overlay's list is a full override of that config key (e.g. an
    allowlist), not an accumulation. Setting prod.json's allowed_ips to a
    shorter list must shrink the effective list, matching a helm-values /
    kustomize style overlay contract."""
    base = write_json(tmp_path, "base.json", {"allowed_ips": ["10.0.0.1", "10.0.0.2", "10.0.0.3"]})
    overlay = write_json(tmp_path, "prod.json", {"allowed_ips": ["203.0.113.5"]})
    result = run_cli(base, overlay)
    merged = json.loads(result.stdout)
    assert merged == {"allowed_ips": ["203.0.113.5"]}


def test_missing_file_exits_nonzero(tmp_path):
    result = run_cli(tmp_path / "does_not_exist.json")
    assert result.returncode == 2


def test_three_way_merge(tmp_path):
    base = write_json(tmp_path, "base.json", {"a": 1, "b": {"x": 1, "y": 1}})
    mid = write_json(tmp_path, "mid.json", {"b": {"y": 2}, "c": 3})
    top = write_json(tmp_path, "top.json", {"a": 9})
    result = run_cli(base, mid, top)
    merged = json.loads(result.stdout)
    assert merged == {"a": 9, "b": {"x": 1, "y": 2}, "c": 3}
