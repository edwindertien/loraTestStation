"""Starts the real app against the built-in simulator, plants hostile packets in its
database, runs the jsdom smoke test, and tears everything down.

    cd monitor && python tests/ui/run_smoke.py      (needs node and `npm i jsdom` in tests/ui)
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
from c6lmon.store import Store  # noqa: E402

PORT = int(os.environ.get("SMOKE_PORT", "5099"))
data = tempfile.mkdtemp(prefix="c6lmon-ui-")
srv = subprocess.Popen([sys.executable, "-m", "c6lmon", "--sim", "--sim-interval", "0.4", "--http-port", str(PORT),
                        "--data-dir", data], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
try:
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", PORT), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    else:
        raise SystemExit("server did not start")

    store = Store(os.path.join(data, "c6l.db"))             # same file the server reads
    now = time.time()
    for i, (text, node) in enumerate([("<script>window.__pwned=1</script>", "script"),
                                      ('<img src=x onerror="window.__pwned=2">', ""),
                                      ('"><svg onload=window.__pwned=3>', "")]):
        store.insert_packet({"ts": now + i * 0.001, "ok": 1, "err": 0, "len": len(text), "rssi": -50.0, "snr": 5.0,
                             "hex": text.encode().hex().upper(), "text": text, "node": node,
                             "freq": 868.1, "bw": 125.0, "sf": 7, "cr": 7, "sync": 18})
    rc = subprocess.call(["node", str(HERE / "smoke.js"), f"http://127.0.0.1:{PORT}"], cwd=HERE)
finally:
    srv.terminate()
    try:
        srv.wait(timeout=5)
    except subprocess.TimeoutExpired:
        srv.kill()
    shutil.rmtree(data, ignore_errors=True)
sys.exit(rc)
