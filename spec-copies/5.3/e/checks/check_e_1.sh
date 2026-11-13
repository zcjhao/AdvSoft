#!/usr/bin/env bash
# Check for E.1 (e-1.md): base, edit and delete. Finishes in under 30 seconds.
# Usage: bash /spec/e/checks/check_e_1.sh      Prints its test results and a RESULT line. Exit 0 only if every rule passes.
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$D/lib.sh"
STEP=1 exec python3 "$D/e_rules.py"
