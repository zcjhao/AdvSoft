#!/usr/bin/env bash
# Check for B.1 (channels-1.md): baseline and channel management.
# Usage: bash /spec/channels/checks/check_channels_1.sh      Prints its test results and a RESULT line. Exit 0 only if every rule passes.
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$D/lib.sh"
ONLY="1 2" exec bash "$D/channels_rules.sh"
