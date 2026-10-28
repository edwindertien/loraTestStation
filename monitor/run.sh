#!/usr/bin/env bash
# Start the monitor. Extra arguments go straight to `python -m c6lmon`, e.g.
#   ./run.sh                                  autodetect the board, serve on http://localhost:5001
#   ./run.sh --port /dev/cu.usbmodem1101      pick the port yourself
#   ./run.sh --sim                            built-in simulated device, no hardware needed
#   ./run.sh --host 0.0.0.0                   reachable from other machines (there is NO login!)
#   ./run.sh --help                           all options
set -e
cd "$(dirname "$0")"
[ -d venv ] || ./setup.sh
source venv/bin/activate
exec python -m c6lmon "$@"
