#!/usr/bin/env bash
# Check for D.1 (d-1.md): base, supervisor and a killed Redis. Finishes in under 30 seconds.
# Usage: bash /spec/d/checks/check_d_1.sh      Prints its test results and a RESULT line. Exit 0 only if every rule passes.
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$D/lib.sh"
STEP=1 exec python3 "$D/d_rules.py"
