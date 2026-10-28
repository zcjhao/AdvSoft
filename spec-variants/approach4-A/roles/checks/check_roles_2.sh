#!/usr/bin/env bash
# Self-check for Roles.2 (workspaces, joining and join mode). Prints PASS/FAIL per rule in /spec/roles/roles-2.md.
# Usage: bash /spec/roles/checks/check_roles_2.sh      Exit 0 only if every rule passes.
ONLY="3 4" exec bash "$(dirname "${BASH_SOURCE[0]}")/roles_rules.sh"
