#!/usr/bin/env bash
# Check for B.4 (channels-4.md): search, pins, mentions and read state.
# Usage: bash /spec/channels/checks/check_channels_4.sh      Prints its test results and a RESULT line. Exit 0 only if every rule passes.
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$D/lib.sh"
ONLY="5 6 7" exec bash "$D/channels_rules.sh"
