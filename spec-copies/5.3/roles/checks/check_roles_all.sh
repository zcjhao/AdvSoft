#!/usr/bin/env bash
# Runs every rule of Component A in one go and prints one total.
# Usage: bash /spec/roles/checks/check_roles_all.sh      Prints its test results and a RESULT line. Exit 0 only if every rule passes.
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$D/lib.sh"
unset ONLY; exec bash "$D/roles_rules.sh"
