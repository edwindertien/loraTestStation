"""The serial link to the sc_gateway firmware.

A reader thread owns the port: it reconnects on its own (the ESP32-C6's USB
port vanishes and reappears on every reset), splits the byte stream into
lines, and classifies each one:

  {"ev":"rx",...}     a received packet      -> stored in the database
  {"freq":...,...}    the reply to `get`     -> cached as the device config
  ok ... / err ...    a command reply        -> hands the result to command()
  "listening..."      the idle heartbeat     -> only used as a liveness signal
  anything else       console text           -> kept in a ring buffer for the UI

command() sends a line and waits for its reply while RX events keep flowing
in the background; it must not be called from the reader thread itself.

`port` may be a device path (/dev/cu.usbmodem1101) or any pyserial URL, e.g.
socket://127.0.0.1:5555 for the built-in simulator or a remote ser2net port.
"""
import collections
import json
import logging
import logging.handlers
import os
import re
import threading
import time

import serial
import serial.tools.list_ports

from . import parse

HEARTBEAT_RE = re.compile(r"^\[\w+\]\s+listening\.\.\.$")
BOOT_MARK = "bring-up scaffold"            # printed once at the top of setup()
CFG_ORDER = ("freq", "bw", "sf", "cr", "sync", "pre")
ESPRESSIF_VID = 0x303A


def list_ports():
    """Serial ports for the UI. `likely` marks Espressif native-USB devices."""
    ports = list(serial.tools.list_ports.comports())
    # macOS lists every USB serial device twice: /dev/cu.X (the one to use - opening it
    # doesn't wait for carrier detect) and /dev/tty.X. Show only cu.X where both exist;
    # otherwise one attached board would look like two candidates and defeat autodetect.
    names = {p.device for p in ports}
    ports = [p for p in ports if not (p.device.startswith("/dev/tty.") and "/dev/cu." + p.device[9:] in names)]
    out = [{"device": p.device, "description": p.description or "",
            "vid": p.vid, "pid": p.pid, "likely": p.vid == ESPRESSIF_VID} for p in ports]
    return sorted(out, key=lambda d: (not d["likely"], d["device"]))


def autodetect_port():
    """The port to use if exactly one Espressif USB device is attached, else None."""
    likely = [p["device"] for p in list_ports() if p["likely"]]
    return likely[0] if len(likely) == 1 else None


def _hexint(v):
    try:
        return int(str(v), 16)
    except (TypeError, ValueError):
        return None


class _Pending:
    def __init__(self, kind):
        self.kind = kind                 # "reply" (next ok/err line) or "get" (the config JSON)
        self.event = threading.Event()
        self.text = ""
        self.data = None


class Device:
    def __init__(self, store, log_dir=None, baud=115200, on_packet=None):
        self.store = store
        self.baud = baud
        self.on_packet = on_packet

        self._ser = None
        self._want_port = None
        self._running = False
        self._thread = None
        self._buf = bytearray()
        self._lock = threading.RLock()       # guards connection state
        self._wlock = threading.Lock()       # serialises writes
        self._cmd_lock = threading.Lock()    # one command in flight at a time
        self._pending = None
        self._init_running = False
        self._resync_at = 0.0

        self.connected = False
        self.port = None
        self.error = None
        self.since = None
        self.config = None
        self.json_mode = False
        self.last_line_ts = None
        self.last_heartbeat_ts = None
        self.boots = 0
        self.rx_ok = 0
        self.rx_fail = 0
        self.started = time.time()

        self._console = collections.deque(maxlen=1000)
        self._console_lock = threading.Lock()
        self._seq = 0

        self._devlog = None
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
            lg = logging.getLogger(f"c6lmon.device.{id(self)}")
            lg.setLevel(logging.INFO)
            lg.propagate = False
            self._devlog_handler = logging.handlers.RotatingFileHandler(
                os.path.join(log_dir, "device.log"), maxBytes=2_000_000, backupCount=3, encoding="utf-8")
            self._devlog_handler.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d %(message)s",
                                                                "%Y-%m-%d %H:%M:%S"))
            lg.addHandler(self._devlog_handler)
            self._devlog = lg
            self.devlog_path = os.path.join(log_dir, "device.log")
        else:
            self.devlog_path = None

    # ---- lifecycle --------------------------------------------------------------
    def start(self):
        if self._thread:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name="c6l-serial", daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        self._want_port = None
        self._close(None)
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._devlog:
            self._devlog.removeHandler(self._devlog_handler)
            self._devlog_handler.close()

    @property
    def want_port(self):
        """The port we're trying to stay connected to (None = deliberately disconnected)."""
        return self._want_port

    def connect(self, port):
        """Connect (or switch) to `port`; the reader thread does the opening and keeps retrying."""
        self._want_port = port
        self._close(None)

    def disconnect(self):
        self._want_port = None
        self._close(None)

    # ---- reader thread -------------------------------------------------------------
    def _run(self):
        backoff = 0.5
        while self._running:
            ser = self._ser
            if ser is None:
                port = self._want_port
                if not port:
                    time.sleep(0.15)
                    continue
                try:
                    self._open(port)
                    backoff = 0.5
                except Exception as e:                      # port busy / not there yet / being re-enumerated
                    self.error = f"cannot open {port}: {e}"
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 5.0)
                continue
            try:
                chunk = ser.read(max(1, getattr(ser, "in_waiting", 0) or 1))
            except Exception as e:
                if self._ser is ser:                        # not just us closing it on purpose
                    self._close(f"connection lost: {e}")
                continue
            if chunk:
                self._feed(chunk)

    def _open(self, port):
        ser = serial.serial_for_url(port, baudrate=self.baud, timeout=0.3, do_not_open=True)
        try:                                                # keep DTR/RTS low: some adapters/OSes reset the board on a toggle
            ser.dtr = False
            ser.rts = False
        except Exception:
            pass
        ser.open()
        with self._lock:
            self._ser = ser
            self.connected = True
            self.port = port
            self.since = time.time()
            self.error = None
            self.config = None
            self.json_mode = False
            self._buf.clear()
        self._note(f"connected to {port}")
        self._start_init()

    def _close(self, reason):
        with self._lock:
            ser, self._ser = self._ser, None
            if reason:
                self.error = reason                         # set together with `connected`, so a status
            self.connected = False                          # poll never sees "disconnected, no reason"
            self.config = None
            self.json_mode = False
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        p = self._pending
        if p is not None:                                   # wake anyone waiting on a reply
            p.text = "err disconnected"
            p.event.set()
        if reason:
            self._note(reason)

    def _feed(self, chunk):
        self._buf += chunk
        while True:
            i = self._buf.find(b"\n")
            if i < 0:
                break
            line = bytes(self._buf[:i])
            del self._buf[:i + 1]
            try:
                self._on_line(line.decode("utf-8", "replace").rstrip("\r"))
            except Exception as e:                          # one bad line must never kill the reader
                self._note(f"error handling a line: {e}")
        if len(self._buf) > 8192:                           # garbage with no newline - don't grow forever
            self._buf.clear()

    # ---- line handling ---------------------------------------------------------------
    def _on_line(self, text):
        ts = time.time()
        self.last_line_ts = ts
        if self._devlog:
            self._devlog.info(text)
        text = text.strip()
        if not text:
            return

        if text.startswith("{"):
            try:
                obj = json.loads(text)
            except ValueError:
                obj = None
            if isinstance(obj, dict):
                if obj.get("ev") == "rx":
                    self._on_rx(obj, ts)
                    return
                if "freq" in obj and "sf" in obj:
                    self._on_cfg(obj)
                    return
            self._add_console(ts, text)                     # malformed / truncated JSON: show it, don't swallow it
            return

        if HEARTBEAT_RE.match(text):
            self.last_heartbeat_ts = ts
            return

        if text.startswith(("ok ", "err ")) or text in ("ok", "err") or text.startswith("unknown command"):
            self._add_console(ts, text)
            p = self._pending
            if p is not None and p.kind == "reply":
                p.text = text
                p.event.set()
            return

        if BOOT_MARK in text:
            self.boots += 1
            self.config = None
            self.json_mode = False
            self._start_init()                              # json mode doesn't survive a reboot
        self._add_console(ts, text)

    def _on_cfg(self, obj):
        cfg = {"freq": obj.get("freq"), "bw": obj.get("bw"), "sf": obj.get("sf"), "cr": obj.get("cr"),
               "sync": obj.get("sync"), "pre": obj.get("pre"), "state": obj.get("state")}
        self.config = cfg
        p = self._pending
        if p is not None and p.kind == "get":
            p.data = cfg
            p.text = "ok"
            p.event.set()

    def _on_rx(self, obj, ts):
        rec = {"ts": ts, "up_ms": obj.get("up_ms"), "ok": 1 if obj.get("ok") else 0,
               "err": int(obj.get("err") or 0), "rssi": obj.get("rssi"), "snr": obj.get("snr"),
               "freq": obj.get("freq"), "bw": obj.get("bw"), "sf": obj.get("sf"), "cr": obj.get("cr"),
               "sync": _hexint(obj.get("sync")), "node": ""}
        fields = {}
        if rec["ok"]:
            data = parse.hex_to_bytes(obj.get("hex"))
            if data is None or obj.get("len") != len(data):
                self._add_console(ts, "ignored an rx event with inconsistent hex/len: " + json.dumps(obj)[:200])
                return
            dec = parse.decode_payload(data)
            rec.update(len=len(data), hex=obj["hex"].upper(), text=dec["text"], node=dec["node"])
            fields = dec["fields"]
            self.rx_ok += 1
        else:
            self.rx_fail += 1
        try:
            pid = self.store.insert_packet(rec, fields)
        except Exception as e:
            self._add_console(ts, f"could not store a packet: {e}")
            return
        self._check_resync(rec)
        if self.on_packet:
            try:
                self.on_packet(pid, rec, fields)
            except Exception:
                pass

    def _check_resync(self, rec):
        """If an event reports settings different from what we think the device has
        (e.g. it rebooted into saved settings behind our back), re-read the config."""
        cfg = self.config
        if cfg is None or self._init_running or time.time() < self._resync_at:
            return
        same = (cfg.get("sf") == rec["sf"] and cfg.get("cr") == rec["cr"]
                and abs((cfg.get("freq") or 0) - (rec["freq"] or 0)) < 0.0005
                and abs((cfg.get("bw") or 0) - (rec["bw"] or 0)) < 0.005
                and _hexint(cfg.get("sync")) == rec["sync"])
        if not same:
            self._resync_at = time.time() + 5.0
            threading.Thread(target=self.refresh_config, daemon=True).start()

    # ---- console ring buffer -----------------------------------------------------------
    def _add_console(self, ts, text):
        with self._console_lock:
            self._seq += 1
            self._console.append((self._seq, ts, text))

    def _note(self, text):
        """App-side notes into the same console, marked so they're not mistaken for device output."""
        self._add_console(time.time(), "[monitor] " + text)

    def console_after(self, seq, limit=500):
        with self._console_lock:
            items = [c for c in self._console if c[0] > seq]
        return [{"seq": s, "ts": t, "text": x} for s, t, x in items[-limit:]]

    # ---- commands --------------------------------------------------------------------------
    def _write(self, line):
        data = (line.strip() + "\n").encode("utf-8")
        with self._wlock:
            ser = self._ser
            if ser is None:
                raise OSError("not connected")
            ser.write(data)
            ser.flush()

    def send_raw(self, line):
        """Fire-and-forget; whatever the device answers shows up in the console."""
        line = line.replace("\r", " ").replace("\n", " ").strip()[:200]
        if not line:
            return False, "empty line"
        try:
            self._write(line)
            return True, "sent"
        except Exception as e:
            return False, str(e)

    def command(self, line, expect="reply", timeout=3.0):
        """Send `line` and wait for its reply. -> (ok, text, data).

        expect="reply": the next ok/err line (ok == it started with "ok")
        expect="get":   the config JSON; data is the parsed config
        Never call this from the reader thread - it would wait on itself.
        """
        if not self.connected:
            return False, "not connected", None
        # `timeout` is the TOTAL budget, including time spent waiting for another
        # command (e.g. the init handshake against a slow board) to finish - otherwise
        # a UI request could hang for as long as those retries take.
        deadline = time.time() + timeout
        if not self._cmd_lock.acquire(timeout=timeout):
            return False, "busy", None
        try:
            p = _Pending(expect)
            self._pending = p
            try:
                self._write(line)
            except Exception as e:
                self._pending = None
                return False, f"write failed: {e}", None
            done = p.event.wait(max(0.05, deadline - time.time()))
            self._pending = None
            if not done:
                return False, "timeout", None
            if p.kind == "get":
                return p.data is not None, p.text, p.data
            return p.text.startswith("ok"), p.text, None
        finally:
            self._cmd_lock.release()

    def refresh_config(self):
        ok, _, data = self.command("get", expect="get", timeout=2.0)
        return data if ok else None

    def _start_init(self):
        if self._init_running:
            return
        self._init_running = True
        threading.Thread(target=self._init_session, name="c6l-init", daemon=True).start()

    def _init_session(self):
        """Switch the firmware into json mode and read its settings. Retries because
        right after a (re)connect the board may still be in setup()."""
        try:
            for _ in range(8):
                if not self.connected:
                    return
                ok, text, _ = self.command("json on", timeout=2.0)
                if ok:
                    self.json_mode = True
                    break
                if text.startswith("unknown command"):
                    self.error = "this firmware has no 'json' command - flash the latest sc_gateway"
                    self._note(self.error)
                    break
                time.sleep(0.7)
            self.refresh_config()
        finally:
            self._init_running = False

    # ---- applying settings --------------------------------------------------------------------
    @staticmethod
    def _norm(key, value):
        """-> (comparable value, text to send). Raises ValueError for junk."""
        if key in ("freq", "bw"):
            f = float(value)
            return f, repr(f)
        if key == "sync":
            n = int(str(value).strip().lower().replace("0x", ""), 16)
            return n, f"{n:X}"
        n = int(float(value))
        return n, str(n)

    @staticmethod
    def _same(key, current, new):
        if current is None:
            return False
        if key == "freq":
            return abs(float(current) - new) < 0.0005
        if key == "bw":
            return abs(float(current) - new) < 0.005
        if key == "sync":
            return _hexint(current) == new
        return int(current) == new

    def apply_config(self, want):
        """Send only the settings that differ from the device's current ones, one at a
        time (stopping at the first the device refuses), then re-read the config.
        -> list of {"key", "ok", "reply"}"""
        cur = dict(self.config or {})
        results = []
        for key in CFG_ORDER:
            if key not in want or want[key] in (None, ""):
                continue
            try:
                new, text = self._norm(key, want[key])
            except (ValueError, TypeError):
                results.append({"key": key, "ok": False, "reply": f"err invalid value for {key}"})
                break
            if self._same(key, cur.get(key), new):
                continue
            ok, reply, _ = self.command(f"{key} {text}")
            results.append({"key": key, "ok": ok, "reply": reply})
            if not ok:
                break
        self.refresh_config()
        return results

    def save_config(self):
        ok, reply, _ = self.command("save")
        self.refresh_config()
        return ok, reply

    def restore_defaults(self):
        ok, reply, _ = self.command("defaults")
        self.refresh_config()
        return ok, reply

    # ---- status ------------------------------------------------------------------------------------
    def status(self):
        return {"connected": self.connected, "port": self.port, "want_port": self.want_port,
                "error": self.error, "config": self.config, "json_mode": self.json_mode,
                "since": self.since, "last_line_ts": self.last_line_ts,
                "last_heartbeat_ts": self.last_heartbeat_ts, "boots": self.boots,
                "session": {"rx_ok": self.rx_ok, "rx_fail": self.rx_fail, "since": self.started},
                "now": time.time()}
