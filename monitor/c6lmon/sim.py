"""A fake sc_gateway, for trying the app without hardware (and for testing it).

It speaks the same line protocol as the real firmware - same commands, same
validation, same reply and JSON formats - but over TCP, which pyserial can
open as  socket://127.0.0.1:<port>.

It also behaves like a radio: a simulated transmitter sends sensor packets
every few seconds on 868.1 MHz / SF7 / BW125 / sync 0x12, and the simulated
receiver only "hears" them while its own settings match. Change the spreading
factor in the Settings tab and reception goes silent; change it back and it
returns - exactly the failure mode that cost real debugging time on the
hardware.

    python -m c6lmon.sim --port 5555 --interval 5
    python -m c6lmon --sim            # (starts one of these for you)
"""
import argparse
import json
import math
import random
import socket
import threading
import time

DEFAULTS = {"freq": 868.1, "bw": 125.0, "sf": 7, "cr": 7, "sync": 0x12, "pre": 8}
BANDWIDTHS = [7.8, 10.4, 15.6, 20.8, 31.25, 41.7, 62.5, 125.0, 250.0, 500.0]
EU868 = [868.1, 868.3, 868.5, 867.1, 867.3, 867.5, 867.7, 867.9]
BW_ERR = "err bandwidth must be one of 7.8 10.4 15.6 20.8 31.25 41.7 62.5 125 250 500 (kHz)"


def pairs(c):
    return "freq=%.3f bw=%g sf=%d cr=%d sync=0x%02X pre=%d" % (
        c["freq"], c["bw"], c["sf"], c["cr"], c["sync"], c["pre"])


def sane(c):
    """Same checks, same messages as the firmware's loraConfigSane(). -> error text or None."""
    if not (150.0 <= c["freq"] <= 960.0):
        return "frequency must be 150-960 MHz"
    if c["bw"] not in BANDWIDTHS:
        return BW_ERR[len("err "):]
    if not 5 <= c["sf"] <= 12:
        return "spreading factor must be 5-12"
    if not 5 <= c["cr"] <= 8:
        return "coding rate must be 5-8 (meaning 4/5 .. 4/8)"
    if not 0 <= c["sync"] <= 255:
        return "sync word must be 00-FF (hex)"
    if not 6 <= c["pre"] <= 65535:
        return "preamble must be 6-65535 symbols"
    return None


class _Node:
    """A simulated SEN66-like sensor with slowly drifting readings."""
    def __init__(self, name, rssi, rng):
        self.name, self.rssi, self.rng = name, rssi, rng
        self.v = {"t": 20.0 + rng.random() * 2, "rh": 55 + rng.random() * 10, "co2": 600 + rng.random() * 200,
                  "voc": 90 + rng.random() * 20, "nox": 1, "pm25": 2.0, "pm10": 3.0, "pm1": 1.0}

    def payload(self):
        r, v = self.rng, self.v
        v["t"] += r.gauss(0, 0.05); v["rh"] = min(95, max(20, v["rh"] + r.gauss(0, 0.2)))
        v["co2"] = max(400, v["co2"] + r.gauss(0, 12)); v["voc"] = max(1, v["voc"] + r.gauss(0, 2))
        v["pm25"] = max(0, v["pm25"] + r.gauss(0, 0.1)); v["pm10"] = max(v["pm25"], v["pm10"] + r.gauss(0, 0.1))
        v["pm1"] = max(0, min(v["pm25"], v["pm1"] + r.gauss(0, 0.05)))
        return ('<%s>{"t":%.2f,"rh":%.1f,"co2":%d,"voc":%d,"nox":%d,"pm25":%.1f,"pm10":%.1f,"pm1":%.1f}'
                % (self.name, v["t"], v["rh"], v["co2"], v["voc"], v["nox"], v["pm25"], v["pm10"], v["pm1"]))


class _Session:
    """One connected client: its own reader thread (commands) and emitter thread (heartbeat + packets)."""
    def __init__(self, sim, conn):
        self.sim, self.conn = sim, conn
        self.alive = True
        self.wlock = threading.Lock()

    def send(self, text):
        try:
            with self.wlock:
                self.conn.sendall(text.encode("utf-8"))
        except OSError:
            self.alive = False

    def close(self):
        self.alive = False
        try:
            # shutdown() first: close() alone doesn't send a FIN while another thread is
            # still blocked in recv() on the same socket, so the peer would never notice.
            self.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.conn.close()
        except OSError:
            pass


class SimDevice:
    def __init__(self, host="127.0.0.1", port=0, interval=5.0, reset_on_connect=True, seed=None,
                 fail_pct=6.0, nodes=2):
        self.interval = interval
        self.reset_on_connect = reset_on_connect
        self.fail_pct = fail_pct
        self.rng = random.Random(seed)
        # What the simulated transmitters use - the receiver must match these to hear anything.
        self.tx = {"freq": 868.1, "bw": 125.0, "sf": 7, "sync": 0x12}
        self.cfg = dict(DEFAULTS)
        self.saved = None
        self.state = "defaults"
        self.json_mode = False
        self.raw = False
        self.boot = time.time()
        self.ok = self.fail = 0
        self.last = None
        self.nodes = [_Node("SEN66-01", -4.0, self.rng)]
        if nodes > 1:
            self.nodes.append(_Node("SEN66-02", -82.0, self.rng))
        self._session = None
        self._running = False
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((host, port))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self.url = f"socket://{host}:{self.port}"

    # ---- lifecycle -----------------------------------------------------------------
    def start(self):
        self._running = True
        threading.Thread(target=self._accept_loop, name="sim-accept", daemon=True).start()
        return self

    def stop(self):
        self._running = False
        if self._session:
            self._session.close()
        try:
            self._srv.close()
        except OSError:
            pass

    def _accept_loop(self):
        while self._running:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            if self._session:                      # one "serial port": a new open replaces the old one
                self._session.close()
            s = _Session(self, conn)
            self._session = s
            threading.Thread(target=self._serve, args=(s,), name="sim-serve", daemon=True).start()
            threading.Thread(target=self._emit, args=(s,), name="sim-emit", daemon=True).start()

    # ---- the "firmware" ----------------------------------------------------------------
    def _banner(self, s):
        self.boot = time.time()
        self.json_mode = False
        self.raw = False
        self.ok = self.fail = 0
        self.last = None
        if self.saved:
            self.cfg, self.state = dict(self.saved), "saved"
        else:
            self.cfg, self.state = dict(DEFAULTS), "defaults"
        s.send("\n[sc_gateway] Unit-C6L bring-up scaffold\n")
        s.send("[sc_gateway] listening at %.3f MHz, SF%d, BW%g kHz (%s)\n"
               % (self.cfg["freq"], self.cfg["sf"], self.cfg["bw"], self.state))
        s.send("Commands:\n  help ... (simulator)\n")

    def _serve(self, s):
        if self.reset_on_connect:
            # A real board needs a moment to boot before it prints its banner. This also
            # matters technically: pyserial's open() ends by DISCARDING any input already
            # waiting, so a banner sent the instant the connection is accepted would be
            # randomly eaten - something a real device never does.
            time.sleep(0.3)
            self._banner(s)
        buf = b""
        while s.alive and self._running:
            try:
                chunk = s.conn.recv(1024)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode("utf-8", "replace").strip()
                if text:
                    s.send(self.handle(text))
        s.close()

    def _num(self, args, cast, base=None):
        try:
            if base is not None:
                return cast(args, base)
            return cast(args)
        except ValueError:
            return None

    def handle(self, line):
        """One command line -> the text the firmware would print (ends with a newline)."""
        sp = line.find(" ")
        cmd = (line if sp < 0 else line[:sp]).lower()
        args = "" if sp < 0 else line[sp + 1:].strip()
        c = self.cfg

        if cmd == "help":
            return "Commands:\n  help stats info last raw json get freq ch bw sf cr sync pre save defaults\n"
        if cmd == "stats":
            return "RX ok: %d   RX fail: %d   uptime: %ds\n" % (self.ok, self.fail, time.time() - self.boot)
        if cmd == "info":
            return ("radio init: ok\n%.3f MHz  SF%d  BW%g kHz  CR4/%d  sync 0x%02X  preamble %d\nsettings: %s\n"
                    % (c["freq"], c["sf"], c["bw"], c["cr"], c["sync"], c["pre"], self.state))
        if cmd == "last":
            return "No RX attempts yet.\n" if not self.last else self.last + "\n"
        if cmd == "raw":
            if args in ("on", "off"):
                self.raw = args == "on"
                return "raw mode: %s\n" % args
            return "usage: raw on|off\n"
        if cmd == "json":
            if args in ("on", "off"):
                self.json_mode = args == "on"
                return "ok json=%s\n" % args
            return "err usage: json on|off\n"
        if cmd == "get":
            return ('{"freq":%.3f,"bw":%g,"sf":%d,"cr":%d,"sync":"0x%02X","pre":%d,"state":"%s"}\n'
                    % (c["freq"], c["bw"], c["sf"], c["cr"], c["sync"], c["pre"], self.state))
        if cmd in ("freq", "ch", "bw", "sf", "cr", "sync", "pre"):
            return self._set(cmd, args)
        if cmd == "save":
            self.saved, self.state = dict(self.cfg), "saved"
            return "ok saved\n"
        if cmd == "defaults":
            self.cfg, self.saved, self.state = dict(DEFAULTS), None, "defaults"
            return "ok defaults restored, saved settings cleared: %s\n" % pairs(self.cfg)
        return "unknown command '%s' - try 'help'\n" % cmd

    def _set(self, cmd, args):
        n = dict(self.cfg)
        if cmd == "freq":
            v = self._num(args, float)
            if v is None:
                return "err usage: freq <MHz>\n"
            n["freq"] = v
        elif cmd == "ch":
            v = self._num(args, int)
            if v is None or not 0 <= v < len(EU868):
                return "err usage: ch <0-7>\n"
            n["freq"] = EU868[v]
        elif cmd == "bw":
            v = self._num(args, float)
            snapped = next((b for b in BANDWIDTHS if v is not None and abs(v - b) < 0.1), None)
            if snapped is None:
                return BW_ERR + "\n"
            n["bw"] = snapped
        elif cmd == "sf":
            v = self._num(args, int)
            if v is None:
                return "err usage: sf <5-12>\n"
            n["sf"] = v
        elif cmd == "cr":
            v = self._num(args, int)
            if v is None:
                return "err usage: cr <5-8>\n"
            n["cr"] = v
        elif cmd == "sync":
            v = self._num(args, int, 16)
            if v is None:
                return "err usage: sync <hex>, e.g. sync 12\n"
            n["sync"] = v
        elif cmd == "pre":
            v = self._num(args, int)
            if v is None:
                return "err usage: pre <6-65535>\n"
            n["pre"] = v
        if isinstance(n["freq"], float) and math.isnan(n["freq"]):
            return "err frequency must be 150-960 MHz\n"
        why = sane(n)
        if why:
            return "err %s\n" % why
        self.cfg, self.state = n, "modified"
        return "ok %s\n" % pairs(n)

    # ---- the "radio" -----------------------------------------------------------------------
    def hears(self):
        c, t = self.cfg, self.tx
        return (abs(c["freq"] - t["freq"]) < 0.02 and c["bw"] == t["bw"]
                and c["sf"] == t["sf"] and c["sync"] == t["sync"])

    def _emit(self, s):
        now = time.time()
        next_hb = now + 2.0
        n = len(self.nodes)
        due = [now + self.interval * (i + 1) / n for i in range(n)]
        while s.alive and self._running:
            time.sleep(0.05)
            now = time.time()
            if now >= next_hb:
                s.send("[sc_gateway] listening...\n")
                next_hb += 2.0
            for i, node in enumerate(self.nodes):
                if now >= due[i]:
                    due[i] += self.interval
                    if self.hears():
                        s.send(self._rx_line(node))

    def _rx_line(self, node):
        c, r = self.cfg, self.rng
        up_ms = int((time.time() - self.boot) * 1000)
        failed = r.random() * 100 < self.fail_pct
        rssi = round(node.rssi + r.gauss(0, 1.5), 1)
        snr = round(r.gauss(10.5, 1.5) if node.rssi > -50 else r.gauss(6.0, 2.0), 1)
        if failed:
            rssi, snr = round(node.rssi - 6 + r.gauss(0, 1), 1), round(r.gauss(-1.0, 1.0), 1)
            self.fail += 1
            if self.json_mode:
                return ('{"ev":"rx","up_ms":%d,"ok":false,"err":-7,"rssi":%.1f,"snr":%.1f,'
                        '"freq":%.3f,"bw":%g,"sf":%d,"cr":%d,"sync":"0x%02X"}\n'
                        % (up_ms, rssi, snr, c["freq"], c["bw"], c["sf"], c["cr"], c["sync"]))
            self.last = "[sc_gateway] %.1fs  RX fail  err=-7  RSSI %.1f dBm  SNR %.1f dB" % (up_ms / 1000, rssi, snr)
            return self.last + "\n"
        data = node.payload().encode("utf-8")
        self.ok += 1
        if self.json_mode:
            return ('{"ev":"rx","up_ms":%d,"ok":true,"err":0,"rssi":%.1f,"snr":%.1f,"len":%d,"hex":"%s",'
                    '"freq":%.3f,"bw":%g,"sf":%d,"cr":%d,"sync":"0x%02X"}\n'
                    % (up_ms, rssi, snr, len(data), data.hex().upper(), c["freq"], c["bw"], c["sf"], c["cr"], c["sync"]))
        self.last = '[sc_gateway] %.1fs  RX %d bytes  RSSI %.1f dBm  SNR %.1f dB  "%s"' % (
            up_ms / 1000, len(data), rssi, snr, data.decode())
        return self.last + "\n"


def main():
    ap = argparse.ArgumentParser(description="Fake sc_gateway for trying the monitor without hardware")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5555)
    ap.add_argument("--interval", type=float, default=5.0, help="seconds between packets per node (real nodes: 30)")
    ap.add_argument("--nodes", type=int, default=2, choices=(1, 2))
    ap.add_argument("--no-reset", action="store_true", help="don't print a boot banner on connect")
    a = ap.parse_args()
    sim = SimDevice(a.host, a.port, a.interval, not a.no_reset, nodes=a.nodes).start()
    print(f"simulator listening: {sim.url}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        sim.stop()


if __name__ == "__main__":
    main()
