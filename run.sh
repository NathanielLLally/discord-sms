#!/usr/bin/env bash
# Launch the Discord <-> Twilio bridge using the project venv.
set -euo pipefail
cd "$(dirname "$0")"
exec .venv/bin/python bot.py
