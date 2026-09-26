"""
c6l-hub dashboard.

Serves a small web UI to flash whichever firmware role's merged .bin
currently sits in firmware_bins/<role>/, over the Unit-C6L's USB-C
connection, using esptool.

Run with ./run.sh (after ./setup.sh once), or `python app.py` inside the venv.
"""
import os
import subprocess
from pathlib import Path

from flask import Flask, jsonify, render_template

FIRMWARE_DIR = Path(__file__).parent / "firmware_bins"
SERIAL_PORT = os.environ.get("C6L_PORT", "/dev/ttyACM0")
CHIP = "esp32c6"
FLASH_OFFSET = "0x0"  # merged binaries (bootloader+partitions+app) go at 0x0
BAUD = "921600"

app = Flask(__name__)


def list_roles():
    """One entry per subfolder of firmware_bins/, with whether a .bin is present."""
    roles = []
    if FIRMWARE_DIR.exists():
        for d in sorted(p for p in FIRMWARE_DIR.iterdir() if p.is_dir()):
            bins = sorted(d.glob("*.bin"))
            roles.append(
                {
                    "name": d.name,
                    "ready": bool(bins),
                    "bin": bins[0].name if bins else None,
                }
            )
    return roles


@app.route("/")
def index():
    return render_template("index.html", roles=list_roles(), port=SERIAL_PORT)


@app.route("/api/roles")
def api_roles():
    return jsonify(list_roles())


@app.route("/api/flash/<role>", methods=["POST"])
def api_flash(role):
    role_dir = FIRMWARE_DIR / role
    bins = sorted(role_dir.glob("*.bin")) if role_dir.exists() else []
    if not bins:
        return jsonify(
            {"ok": False, "error": f"No .bin in firmware_bins/{role}/"}
        ), 404

    cmd = [
        "esptool.py",
        "--chip", CHIP,
        "--port", SERIAL_PORT,
        "--baud", BAUD,
        "write_flash",
        FLASH_OFFSET,
        str(bins[0]),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        return jsonify(
            {
                "ok": result.returncode == 0,
                "returncode": result.returncode,
                "stdout": result.stdout[-4000:],
                "stderr": result.stderr[-4000:],
            }
        )
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "esptool timed out"}), 500
    except FileNotFoundError:
        return jsonify(
            {"ok": False, "error": "esptool.py not found - is the venv active?"}
        ), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
