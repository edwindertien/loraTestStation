#!/usr/bin/env bash
# One-time setup: a virtualenv with Flask + pyserial. Same pattern as ../pi/setup.sh.
set -e
cd "$(dirname "$0")"

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo
echo "Done. Start it with ./run.sh   (add --sim to try it without any hardware)"
