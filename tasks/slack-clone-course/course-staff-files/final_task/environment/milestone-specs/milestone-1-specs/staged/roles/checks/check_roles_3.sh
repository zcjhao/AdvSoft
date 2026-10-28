#!/usr/bin/env bash
# Self-check for Roles.3 (roles, ownership transfer and channel permissions). Prints PASS/FAIL per rule in /spec/roles/roles-3.md.
# Usage: bash /spec/roles/checks/check_roles_3.sh      Exit 0 only if every rule passes.
ONLY="5 6 7" exec bash "$(dirname "${BASH_SOURCE[0]}")/roles_rules.sh"
