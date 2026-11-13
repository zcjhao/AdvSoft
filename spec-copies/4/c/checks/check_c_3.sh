#!/usr/bin/env bash
# Check for C.3 (c-3.md): files and slash commands. Finishes in under 30 seconds.
# Usage: bash /spec/c/checks/check_c_3.sh      Prints its test results and a RESULT line. Exit 0 only if every rule passes.
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$D/lib.sh"
STEP=3 exec python3 "$D/c_rules.py"
