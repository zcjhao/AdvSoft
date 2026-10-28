#!/usr/bin/env bash
# Runs every rule of Roles.1 to Roles.4 in one go and prints one total.
# Usage: bash /spec/roles/checks/check_roles_all.sh      Exit 0 only if every rule passes.
unset ONLY; exec bash "$(dirname "${BASH_SOURCE[0]}")/roles_rules.sh"
