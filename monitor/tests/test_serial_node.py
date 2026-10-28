"""The same device logic over a REAL serial device node (pyserial's serialposix path,
which is what /dev/cu.usbmodem* or /dev/ttyACM0 use), not the TCP stand-in. A pty plays the
board; the simulator supplies its behaviour. Skipped where ptys don't exist (Windows)."""
import os
import select
import threading
import time

import pytest

from conftest import wait_for
from c6lmon.device import Device
from c6lmon.sim import SimDevice

pytestmark = pytest.mark.skipif(not hasattr(os, "openpty"), reason="needs a pty")


class PtyBoard:
    """Feeds a pty master the way a board feeds its USB serial port."""

    def __init__(self, sim):
        self.sim = sim
        sim._running = True                   # the simulator's emitter loop only runs while "serving"
        self.master, self.slave = os.openpty()
        self.name = os.ttyname(self.slave)
        self.alive = True
        self.buf = b""
        self.threads = [threading.Thread(target=self._read, daemon=True),
                        threading.Thread(target=self._announce, daemon=True),
                        threading.Thread(target=sim._emit, args=(self,), daemon=True)]
        for t in self.threads:
            t.start()

    # the interface the simulator's emitter expects from a session
    def send(self, text):
        try:
            os.write(self.master, text.encode())
        except OSError:
            self.alive = False

    def _announce(self):
        time.sleep(0.4)                       # boot time, after the port has been opened
        if self.alive:
            self.sim._banner(self)

    def _read(self):
        while self.alive:
            # Poll rather than block in read(): closing an fd that another thread is blocked
            # on does NOT hang up a pty, so the reader has to be able to leave first.
            ready, _, _ = select.select([self.master], [], [], 0.1)
            if not ready:
                continue
            try:
                chunk = os.read(self.master, 1024)
            except OSError:
                return
            self.buf += chunk
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                text = line.decode("utf-8", "replace").strip()
                if text:
                    self.send(self.sim.handle(text))

    def close(self):
        self.alive = False
        for t in self.threads:
            t.join(timeout=1.0)
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass


def test_full_session_over_a_device_node(store):
    sim = SimDevice(interval=0.25, seed=1, fail_pct=0)
    board = PtyBoard(sim)
    d = Device(store)
    d.start(); d.connect(board.name)
    try:
        assert wait_for(lambda: d.connected and d.config and d.json_mode, 10), "handshake over the device node failed"
        assert d.config["sf"] == 7
        assert wait_for(lambda: store.count() >= 3, 10), "no packets over the device node"
        res = d.apply_config({"sf": 9})
        assert res[0]["ok"] and d.config["sf"] == 9 and sim.cfg["sf"] == 9
        assert d.boots >= 1                                              # the banner after open was seen
    finally:
        d.stop(); board.close()


def test_unplugging_the_device_is_noticed(store):
    sim = SimDevice(interval=0.25, seed=1, fail_pct=0)
    board = PtyBoard(sim)
    d = Device(store)
    d.start(); d.connect(board.name)
    try:
        assert wait_for(lambda: d.connected and d.config, 10)
        board.close()                                                     # like pulling the USB cable
        assert wait_for(lambda: not d.connected, 5), "disappearance not noticed"
        assert d.error                                                    # and the UI has something to say about it
    finally:
        d.stop()
