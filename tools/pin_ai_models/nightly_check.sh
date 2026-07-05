#!/bin/sh
# Unprivileged nightly liveness gate. Does not use sudo.
set -eu
if python3 -m tools.pin_ai_models.pin_ai_models --check-target "$@"; then
  exit 0
fi
notify "pin-ai-models liveness failed on ACE-AI"
exit 1
