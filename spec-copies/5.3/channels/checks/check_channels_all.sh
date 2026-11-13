#!/usr/bin/env bash
# Runs every rule of Component B in one go and prints one total.
# Usage: bash /spec/channels/checks/check_channels_all.sh      Prints its test results and a RESULT line. Exit 0 only if every rule passes.
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$D/lib.sh"
unset ONLY; exec bash "$D/channels_rules.sh"
