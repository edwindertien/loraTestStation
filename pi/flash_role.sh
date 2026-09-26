#!/usr/bin/env bash
# Flash a role's merged .bin from firmware_bins/<role>/ over USB.
# Usage: ./flash_role.sh lora_node
# Run inside the venv (source venv/bin/activate) or after ./setup.sh.
set -e
cd "$(dirname "$0")"

ROLE="$1"
PORT="${C6L_PORT:-/dev/ttyACM0}"
BIN_DIR="firmware_bins/$ROLE"
BIN_FILE=$(ls "$BIN_DIR"/*.bin 2>/dev/null | head -n1 || true)

if [ -z "$ROLE" ] || [ -z "$BIN_FILE" ]; then
  echo "Usage: $0 <role>   (looked in $BIN_DIR)"
  echo "Available roles:"
  ls firmware_bins 2>/dev/null || echo "  (firmware_bins/ is empty)"
  exit 1
fi

echo "Flashing '$ROLE' from $BIN_FILE to $PORT ..."
esptool.py --chip esp32c6 --port "$PORT" --baud 921600 write_flash 0x0 "$BIN_FILE"
