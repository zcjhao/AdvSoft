#!/usr/bin/env bash
# Check for A.3 (roles-3.md): roles, ownership transfer and channel permissions.
# Usage: bash /spec/roles/checks/check_roles_3.sh      Prints its test results and a RESULT line. Exit 0 only if every rule passes.
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$D/lib.sh"
ONLY="5 6 7" exec bash "$D/roles_rules.sh"
