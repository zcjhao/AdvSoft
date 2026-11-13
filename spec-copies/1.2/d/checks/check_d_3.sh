#!/usr/bin/env bash
# Check for D.3 (d-3.md): performance. Finishes in under 30 seconds.
# Usage: bash /spec/d/checks/check_d_3.sh      Prints its test results and a RESULT line. Exit 0 only if every rule passes.
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$D/lib.sh"
STEP=3 exec python3 "$D/d_rules.py"
