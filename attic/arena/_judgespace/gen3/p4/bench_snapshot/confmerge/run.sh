#!/usr/bin/env bash
# Success criterion for the confmerge bancada: all pytest tests pass.
# Exit code 0 = "consertado". Any nonzero = still broken.
set -uo pipefail
cd "$(dirname "$0")"
python3 -m pytest test_confmerge.py -q
exit $?
