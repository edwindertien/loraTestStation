"""python -m c6lmon  -  run the monitor."""
import argparse
import os
import sys
import time
from pathlib import Path

from . import __version__
from . import device as devmod
from .app import create_app
from .store import Store

DEFAULT_DATA = Path(__file__).resolve().parent.parent / "data"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="c6lmon", description="Monitor and control the c6l-hub sc_gateway over serial.")
    ap.add_argument("--port", help="serial port or pyserial URL, e.g. /dev/cu.usbmodem1101 or "
                                   "socket://host:port (default: $C6L_PORT, else the one attached ESP32 if unambiguous)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--host", default="127.0.0.1",
                    help="address to serve on; use 0.0.0.0 to reach it from other machines (there is NO login)")
    ap.add_argument("--http-port", type=int, default=5001, help="default 5001 so it can run beside the flasher on 5000")
    ap.add_argument("--data-dir", default=os.environ.get("C6L_MONITOR_DATA", str(DEFAULT_DATA)),
                    help="where the database and device log live")
    ap.add_argument("--sim", action="store_true", help="run against a built-in simulated device (no hardware needed)")
    ap.add_argument("--sim-interval", type=float, default=5.0, help="seconds between simulated packets")
    ap.add_argument("--no-autoconnect", action="store_true", help="start disconnected; pick a port in the UI")
    args = ap.parse_args(argv)

    data = Path(args.data_dir)
    store = Store(str(data / "c6l.db"))
    device = devmod.Device(store, log_dir=str(data / "logs"), baud=args.baud)
    device.start()

    sim = None
    port = None
    if args.sim:
        from .sim import SimDevice
        sim = SimDevice(interval=args.sim_interval).start()
        port = sim.url
        print(f"simulated device at {port}")
    elif not args.no_autoconnect:
        port = args.port or os.environ.get("C6L_PORT") or devmod.autodetect_port()
    if port:
        print(f"connecting to {port}")
        device.connect(port)
    else:
        print("no serial port selected - choose one in the UI")

    app = create_app(store, device)
    print(f"c6lmon {__version__}: http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.http_port}/"
          f"   data: {data}")
    if args.host not in ("127.0.0.1", "localhost"):
        print("WARNING: serving beyond this machine with no authentication - anyone who can reach it can change "
              "the radio settings and delete data.", file=sys.stderr)
    try:
        app.run(host=args.host, port=args.http_port, threaded=True, debug=False)
    finally:
        device.stop()
        if sim:
            sim.stop()


if __name__ == "__main__":
    main()
