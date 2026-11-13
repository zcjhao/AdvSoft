#!/usr/bin/env bash
# Check for E.6 (e-6.md): UI messages. Finishes in under 30 seconds.
# Usage: bash /spec/e/checks/check_e_6.sh      Prints its test results and a RESULT line. Exit 0 only if every rule passes.
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$D/lib.sh"
PY="${CHK_PY:-/opt/test-venv/bin/python}"; [ -x "$PY" ] || PY=python3
STEP=6 exec "$PY" "$D/e_ui.py"
