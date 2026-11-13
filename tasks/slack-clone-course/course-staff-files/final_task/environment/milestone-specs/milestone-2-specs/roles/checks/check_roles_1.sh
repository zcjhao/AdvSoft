#!/usr/bin/env bash
# Check for A.1 (roles-1.md): cluster baseline and auth.
# Usage: bash /spec/roles/checks/check_roles_1.sh      Prints its test results and a RESULT line. Exit 0 only if every rule passes.
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$D/lib.sh"
ONLY="1 2" exec bash "$D/roles_rules.sh"
