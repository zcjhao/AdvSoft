#!/usr/bin/env bash
# Check for B.2 (channels-2.md): messages, threads and paging.
# Usage: bash /spec/channels/checks/check_channels_2.sh      Prints its test results and a RESULT line. Exit 0 only if every rule passes.
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$D/lib.sh"
ONLY="3 4" exec bash "$D/channels_rules.sh"
