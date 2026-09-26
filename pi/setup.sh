#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo
echo "Done. Activate later with: source venv/bin/activate"
