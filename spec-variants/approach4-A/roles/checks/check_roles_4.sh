#!/usr/bin/env bash
# Self-check for Roles.4 (invitations). Prints PASS/FAIL per rule in /spec/roles/roles-4.md.
# Usage: bash /spec/roles/checks/check_roles_4.sh      Exit 0 only if every rule passes.
ONLY="8" exec bash "$(dirname "${BASH_SOURCE[0]}")/roles_rules.sh"
