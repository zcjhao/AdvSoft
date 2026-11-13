#!/usr/bin/env bash
# Check for A.4 (roles-4.md): invitations.
# Usage: bash /spec/roles/checks/check_roles_4.sh      Prints its test results and a RESULT line. Exit 0 only if every rule passes.
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$D/lib.sh"
ONLY="8" exec bash "$D/roles_rules.sh"
