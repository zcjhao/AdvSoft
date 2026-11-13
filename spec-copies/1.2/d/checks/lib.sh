#!/usr/bin/env bash
# Shared settings for the Component D checks. Sourced by the check_*.sh scripts in this folder, not run directly.
FEEDBACK=score_only
export FEEDBACK
echo "CHECK STARTED: a RESULT line follows when it finishes. No RESULT line means it did not finish: count it as FAILED."
