#!/usr/bin/env bash
# Build an env with PlatformIO, then flash it with a known-working esptool
# binary instead of pioarduino's currently-broken one.
#
# Why this exists: pioarduino's bundled esptool (5.4.0) crashes on every
# upload with an AttributeError in its progress-bar code
# (pioarduino/platform-espressif32#539). PlatformIO's own build still
# succeeds and produces a single pre-merged, ready-to-flash file -
# .pio/build/<env>/firmware.factory.bin - as a normal part of compiling,
# regardless of whether the (broken) upload step runs afterward. So: let
# PlatformIO compile, flash with a working esptool from elsewhere - by
# default, the one M5Stack's own Arduino board package installs.
#
# Usage: ./flash_manual.sh <env> [port]
#   ./flash_manual.sh sc_gateway
#   ./flash_manual.sh sc_gateway /dev/cu.usbmodem213101
#
# Override the esptool binary if yours lives elsewhere:
#   ESPTOOL_BIN=/path/to/esptool ./flash_manual.sh sc_gateway
set -e
cd "$(dirname "$0")"

ENV="${1:?Usage: $0 <env> (lora_node|sc_gateway) [port]}"
PORT="${2:-/dev/cu.usbmodem213101}"
ESPTOOL="${ESPTOOL_BIN:-$HOME/Library/Arduino15/packages/m5stack/tools/esptool_py/5.3.0/esptool}"
MERGED_BIN=".pio/build/$ENV/firmware.factory.bin"

if [ ! -x "$ESPTOOL" ]; then
    echo "esptool not found (or not executable) at: $ESPTOOL"
    echo "Install the M5Stack board package in Arduino IDE once to get it there,"
    echo "or set ESPTOOL_BIN to point at any other working esptool binary."
    exit 1
fi

echo "Building $ENV with PlatformIO..."
pio run -e "$ENV"

if [ ! -f "$MERGED_BIN" ]; then
    echo "Expected merged binary not found at $MERGED_BIN"
    echo "Check the build output above for the actual merged .bin filename/path."
    exit 1
fi

echo "Flashing $MERGED_BIN via $ESPTOOL on $PORT ..."
"$ESPTOOL" --chip esp32c6 --port "$PORT" --baud 1500000 \
    --before default-reset --after hard-reset \
    write-flash -z --flash-mode keep --flash-freq keep --flash-size keep \
    0x0 "$MERGED_BIN"