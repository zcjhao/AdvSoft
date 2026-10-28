#!/usr/bin/env bash
# Self-check for Channels.1 (baseline and channel management). Prints PASS/FAIL per rule in /spec/channels/channels-1.md.
# Usage: bash /spec/channels/checks/check_channels_1.sh      Exit 0 only if every rule passes.
ONLY="1 2" exec bash "$(dirname "${BASH_SOURCE[0]}")/channels_rules.sh"
