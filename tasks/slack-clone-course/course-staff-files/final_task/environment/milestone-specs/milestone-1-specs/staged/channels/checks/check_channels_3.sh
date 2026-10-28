#!/usr/bin/env bash
# Self-check for Channels.3 (events over WebSocket). Prints PASS/FAIL per rule in /spec/channels/channels-3.md.
# Usage: bash /spec/channels/checks/check_channels_3.sh      Exit 0 only if every rule passes.
ONLY="8" exec bash "$(dirname "${BASH_SOURCE[0]}")/channels_rules.sh"
