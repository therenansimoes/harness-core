#!/usr/bin/env bash
# Objective success criterion for the lead-reconciliation bug.
# Exit code 0 = all tests pass (bug fixed). Non-zero = bug still present.
set -euo pipefail
cd "$(dirname "$0")"
python3 -m pytest test_reconcile.py -q
