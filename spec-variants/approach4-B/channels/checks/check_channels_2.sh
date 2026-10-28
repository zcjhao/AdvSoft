#!/usr/bin/env bash
# Self-check for Channels.2 (messages, threads and paging). Prints PASS/FAIL per rule in /spec/channels/channels-2.md.
# Usage: bash /spec/channels/checks/check_channels_2.sh      Exit 0 only if every rule passes.
ONLY="3 4" exec bash "$(dirname "${BASH_SOURCE[0]}")/channels_rules.sh"
