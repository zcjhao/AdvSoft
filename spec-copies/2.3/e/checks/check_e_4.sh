#!/usr/bin/env bash
# Check for E.4 (e-4.md): UI shell. Finishes in under 30 seconds.
# Usage: bash /spec/e/checks/check_e_4.sh      Prints its test results and a RESULT line. Exit 0 only if every rule passes.
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$D/lib.sh"
PY="${CHK_PY:-/opt/test-venv/bin/python}"; [ -x "$PY" ] || PY=python3
STEP=4 exec "$PY" "$D/e_ui.py"
