#!/usr/bin/env bash
# Check for E.3 (e-3.md): IRC bridging. Finishes in under 30 seconds.
# Usage: bash /spec/e/checks/check_e_3.sh      Prints its test results and a RESULT line. Exit 0 only if every rule passes.
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$D/lib.sh"
STEP=3 exec python3 "$D/e_rules.py"
