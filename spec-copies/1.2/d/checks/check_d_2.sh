#!/usr/bin/env bash
# Check for D.2 (d-2.md): a killed HTTP node. Finishes in under 30 seconds.
# Usage: bash /spec/d/checks/check_d_2.sh      Prints its test results and a RESULT line. Exit 0 only if every rule passes.
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$D/lib.sh"
STEP=2 exec python3 "$D/d_rules.py"
