#!/usr/bin/env bash
# Check for E.2 (e-2.md): IRC registration and commands. Finishes in under 30 seconds.
# Usage: bash /spec/e/checks/check_e_2.sh      Prints its test results and a RESULT line. Exit 0 only if every rule passes.
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$D/lib.sh"
STEP=2 exec python3 "$D/e_rules.py"
