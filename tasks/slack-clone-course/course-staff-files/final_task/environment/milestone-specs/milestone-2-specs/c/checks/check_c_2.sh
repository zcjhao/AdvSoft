#!/usr/bin/env bash
# Check for C.2 (c-2.md): DMs and profiles. Finishes in under 30 seconds.
# Usage: bash /spec/c/checks/check_c_2.sh      Prints its test results and a RESULT line. Exit 0 only if every rule passes.
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$D/lib.sh"
STEP=2 exec python3 "$D/c_rules.py"
