#!/usr/bin/env bash
# Check engine for Component B: runs check_channels.py (Python standard library only). Called by check_channels_N.sh; do not run or edit it directly.
[ -n "${FEEDBACK:-}" ] || . "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
export FEEDBACK
exec python3 "$(dirname "${BASH_SOURCE[0]}")/check_channels.py" "$@"
