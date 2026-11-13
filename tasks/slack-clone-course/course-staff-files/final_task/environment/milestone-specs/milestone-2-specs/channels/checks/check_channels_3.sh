#!/usr/bin/env bash
# Check for B.3 (channels-3.md): events over WebSocket.
# Usage: bash /spec/channels/checks/check_channels_3.sh      Prints its test results and a RESULT line. Exit 0 only if every rule passes.
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$D/lib.sh"
ONLY="8" exec bash "$D/channels_rules.sh"
