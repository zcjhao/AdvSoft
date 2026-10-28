#!/usr/bin/env bash
# Self-check for Roles.1 (cluster baseline and auth). Prints PASS/FAIL per rule in /spec/roles/roles-1.md.
# Usage: bash /spec/roles/checks/check_roles_1.sh      Exit 0 only if every rule passes.
ONLY="1 2" exec bash "$(dirname "${BASH_SOURCE[0]}")/roles_rules.sh"
