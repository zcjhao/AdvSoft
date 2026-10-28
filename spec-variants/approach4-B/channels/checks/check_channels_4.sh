#!/usr/bin/env bash
# Self-check for Channels.4 (search, pins, mentions and read state). Prints PASS/FAIL per rule in /spec/channels/channels-4.md.
# Usage: bash /spec/channels/checks/check_channels_4.sh      Exit 0 only if every rule passes.
ONLY="5 6 7" exec bash "$(dirname "${BASH_SOURCE[0]}")/channels_rules.sh"
