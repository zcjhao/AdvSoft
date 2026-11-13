#!/usr/bin/env bash
# Check for E.5 (e-5.md): UI sign-up, workspace, channels. Finishes in under 30 seconds.
# Usage: bash /spec/e/checks/check_e_5.sh      Prints its test results and a RESULT line. Exit 0 only if every rule passes.
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$D/lib.sh"
PY="${CHK_PY:-/opt/test-venv/bin/python}"; [ -x "$PY" ] || PY=python3
STEP=5 exec "$PY" "$D/e_ui.py"
