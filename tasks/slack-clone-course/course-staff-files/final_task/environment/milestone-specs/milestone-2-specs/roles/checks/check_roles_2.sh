#!/usr/bin/env bash
# Check for A.2 (roles-2.md): workspaces, joining and join mode.
# Usage: bash /spec/roles/checks/check_roles_2.sh      Prints its test results and a RESULT line. Exit 0 only if every rule passes.
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$D/lib.sh"
ONLY="3 4" exec bash "$D/roles_rules.sh"
