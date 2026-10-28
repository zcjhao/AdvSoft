#!/usr/bin/env bash
# Self-check for Component B (channels and message history). Prints PASS/FAIL per rule in /spec/channels/channels.md.
# Usage: bash /spec/channels/checks/check_channels.sh      Exit 0 only if every rule passes. Finishes in under 30 seconds.
# The work is done by check_channels.py next to this file (Python stdlib only, no install needed).
exec python3 "$(dirname "${BASH_SOURCE[0]}")/check_channels.py" "$@"
