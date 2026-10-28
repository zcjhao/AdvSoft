#!/usr/bin/env bash
# Runs every rule of Channels.1 to Channels.4 in one go and prints one total.
# Usage: bash /spec/channels/checks/check_channels_all.sh      Exit 0 only if every rule passes.
unset ONLY; exec bash "$(dirname "${BASH_SOURCE[0]}")/channels_rules.sh"
