import json
import time

from conftest import load_fixtures, wait_for

FW = load_fixtures()


# ---- the firmware's own lines, fed straight in (no serial involved) ------------------
def test_firmware_rx_ok_line_is_stored(idle_device, store):
    idle_device._on_line(FW["RX_OK"])
    (row,) = store.list_packets()
    assert row["ok"] == 1 and row["len"] == 90 and row["node"] == "SEN66-01"
    assert row["rssi"] == -4.0 and row["snr"] == 12.8 and row["up_ms"] == 12345
    assert (row["freq"], row["bw"], row["sf"], row["cr"], row["sync"]) == (868.1, 125.0, 7, 7, 0x12)
    assert row["fields"] == {"t": 19.92, "rh": 61.9, "co2": 733.0, "voc": 99.0, "nox": 1.0,
                             "pm25": 2.0, "pm10": 3.1, "pm1": 1.0}
    assert row["text"].startswith("<SEN66-01>{") and row["hex"].startswith("3C53454E3636")
    assert idle_device.rx_ok == 1


def test_firmware_rx_fail_line_is_stored_without_bytes(idle_device, store):
    idle_device._on_line(FW["RX_FAIL"])
    (row,) = store.list_packets()
    assert row["ok"] == 0 and row["err"] == -7 and row["rssi"] == -19.0 and row["snr"] == -0.5
    assert row["hex"] is None and row["len"] is None and row["text"] is None and row["node"] == ""
    assert idle_device.rx_fail == 1


def test_firmware_get_line_updates_config(idle_device):
    idle_device._on_line(FW["GET"])
    assert idle_device.config == {"freq": 868.1, "bw": 125.0, "sf": 7, "cr": 7, "sync": "0x12", "pre": 8,
                                  "state": "defaults"}


def test_replies_and_noise(idle_device):
    for key in ("SET_OK", "SET_ERR", "JSON_ON", "SAVE", "DEFAULTS", "UNKNOWN"):
        idle_device._on_line(FW[key])
    texts = [c["text"] for c in idle_device.console_after(0)]
    assert FW["SET_ERR"] in texts and FW["UNKNOWN"] in texts and FW["JSON_ON"] in texts


def test_heartbeat_is_liveness_not_console(idle_device):
    idle_device._on_line("[sc_gateway] listening...")
    assert idle_device.console_after(0) == [] and idle_device.last_heartbeat_ts is not None


def test_rx_event_with_inconsistent_len_is_rejected(idle_device, store):
    bad = json.loads(FW["RX_OK"])
    bad["len"] = 91                                         # claims more bytes than the hex holds
    idle_device._on_line(json.dumps(bad))
    assert store.count() == 0
    assert "inconsistent" in idle_device.console_after(0)[-1]["text"]


def test_rx_event_with_bad_hex_is_rejected(idle_device, store):
    bad = json.loads(FW["RX_OK"]); bad["hex"] = "ZZ" + bad["hex"][2:]
    idle_device._on_line(json.dumps(bad))
    assert store.count() == 0


def test_truncated_json_is_shown_not_swallowed(idle_device, store):
    idle_device._on_line(FW["RX_OK"][:120])                  # a line cut short mid-hex
    assert store.count() == 0
    assert idle_device.console_after(0)[-1]["text"] == FW["RX_OK"][:120]


def test_hostile_payloads_are_stored_inert(idle_device, store):
    evil = b'<script>alert(1)</script><img src=x onerror=alert(2)>'
    ev = {"ev": "rx", "up_ms": 1, "ok": True, "err": 0, "rssi": -1.0, "snr": 1.0, "len": len(evil),
          "hex": evil.hex().upper(), "freq": 868.1, "bw": 125, "sf": 7, "cr": 7, "sync": "0x12"}
    idle_device._on_line(json.dumps(ev))
    (row,) = store.list_packets()
    assert row["text"] == evil.decode()                              # kept verbatim; escaping is the UI's job
    assert row["node"] == "script"                                   # a hostile tag is just a harmless string


def test_split_lines_and_crlf_are_reassembled(idle_device, store):
    data = (FW["RX_OK"] + "\r\n").encode()
    for i in range(0, len(data), 37):                        # arrives in arbitrary chunks
        idle_device._feed(data[i:i + 37])
    assert store.count() == 1


def test_garbage_without_newline_does_not_grow_forever(idle_device):
    idle_device._feed(b"x" * 20000)
    assert len(idle_device._buf) == 0


def test_boot_banner_resets_state(idle_device):
    idle_device.config = {"sf": 7}; idle_device.json_mode = True
    idle_device._init_running = True                         # keep the init thread from starting
    idle_device._on_line("[sc_gateway] Unit-C6L bring-up scaffold")
    assert idle_device.boots == 1 and idle_device.config is None and not idle_device.json_mode


def test_device_log_is_written(idle_device, tmp_path):
    idle_device._on_line("hello from the board")
    idle_device._devlog_handler.flush()
    assert "hello from the board" in (tmp_path / "logs" / "device.log").read_text()


# ---- against the simulator, over a real (TCP) serial link --------------------------------
def test_handshake_and_packets_flow(live_device, store):
    assert live_device.config["sf"] == 7 and live_device.json_mode
    assert wait_for(lambda: store.count() >= 3), "no packets arrived from the simulator"
    rows = store.list_packets()
    assert {r["node"] for r in rows} <= {"SEN66-01", "SEN66-02"} and all(r["fields"] for r in rows if r["ok"])


def test_apply_config_changes_only_what_differs(live_device, sim):
    res = live_device.apply_config({"freq": 868.1, "bw": 125, "sf": 9, "cr": 7, "sync": "0x12", "pre": 8})
    assert [r["key"] for r in res] == ["sf"] and res[0]["ok"] and res[0]["reply"].startswith("ok ")
    assert live_device.config["sf"] == 9 and sim.cfg["sf"] == 9 and live_device.config["state"] == "modified"


def test_apply_config_stops_at_first_refusal_and_reports_why(live_device, sim):
    res = live_device.apply_config({"freq": 867.3, "sf": 99, "cr": 5})
    assert [r["key"] for r in res] == ["freq", "sf"]                 # cr was never sent
    assert res[0]["ok"] and not res[1]["ok"] and res[1]["reply"] == "err spreading factor must be 5-12"
    assert sim.cfg["freq"] == 867.3 and sim.cfg["cr"] == 7
    assert live_device.config["freq"] == 867.3                       # config re-read after a partial apply


def test_apply_config_rejects_junk_locally(live_device, sim):
    res = live_device.apply_config({"sf": "banana"})
    assert not res[0]["ok"] and "invalid value" in res[0]["reply"] and sim.cfg["sf"] == 7


def test_sync_word_input_forms(live_device, sim):
    live_device.apply_config({"sync": "0x34"})
    assert sim.cfg["sync"] == 0x34
    live_device.apply_config({"sync": "12"})                         # bare hex, like the CLI
    assert sim.cfg["sync"] == 0x12


def test_save_and_defaults(live_device, sim):
    live_device.apply_config({"sf": 8})
    ok, reply = live_device.save_config()
    assert ok and live_device.config["state"] == "saved" and sim.saved["sf"] == 8
    ok, reply = live_device.restore_defaults()
    assert ok and live_device.config["sf"] == 7 and live_device.config["state"] == "defaults" and sim.saved is None


def test_mismatched_settings_silence_reception_and_restoring_brings_it_back(live_device, store):
    assert wait_for(lambda: store.count() >= 2)
    live_device.apply_config({"sf": 9})                              # transmitter stays on SF7
    time.sleep(0.3)
    n = store.count()
    time.sleep(1.2)                                                  # ~5 packet intervals of silence
    assert store.count() == n
    live_device.apply_config({"sf": 7})
    assert wait_for(lambda: store.count() > n), "reception did not resume"


def test_packets_record_the_settings_they_were_received_under(live_device, store):
    live_device.apply_config({"freq": 868.1005})                     # still within what the sim hears
    assert wait_for(lambda: any(abs(r["freq"] - 868.1) < 0.01 for r in store.list_packets()))
    n = store.max_id()
    live_device.apply_config({"freq": 868.1, "bw": 125, "sf": 7})
    assert wait_for(lambda: store.max_id() > n)
    assert store.list_packets(limit=1)[0]["sf"] == 7


def test_command_when_disconnected(idle_device):
    ok, text, _ = idle_device.command("get", expect="get")
    assert (ok, text) == (False, "not connected")
    assert idle_device.send_raw("stats") == (False, "not connected")


def _silent_server():
    import socket
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)       # accepts but never answers
    return srv, f"socket://127.0.0.1:{srv.getsockname()[1]}"


def test_command_timeout_when_device_is_silent(store):
    from c6lmon.device import Device
    srv, url = _silent_server()
    d = Device(store); d._start_init = lambda: None                         # no handshake in the way
    d.start(); d.connect(url)
    try:
        assert wait_for(lambda: d.connected)
        t = time.time()
        ok, text, _ = d.command("get", expect="get", timeout=0.4)
        assert (ok, text) == (False, "timeout") and time.time() - t < 1.0
    finally:
        d.stop(); srv.close()


def test_command_reports_busy_instead_of_hanging_behind_the_handshake(store):
    from c6lmon.device import Device
    srv, url = _silent_server()
    d = Device(store); d.start(); d.connect(url)                            # the init handshake now holds the lock for ~2s per try
    try:
        assert wait_for(lambda: d.connected and d._init_running)
        time.sleep(0.2)
        t = time.time()
        ok, text, _ = d.command("get", expect="get", timeout=0.4)
        assert (ok, text) == (False, "busy") and time.time() - t < 1.0      # NOT ~2s+
    finally:
        d.stop(); srv.close()


def test_reconnects_after_the_link_drops(live_device, sim):
    boots = live_device.boots
    sim._session.close()                                             # like the board resetting / USB re-enumerating
    assert wait_for(lambda: not live_device.connected, 3), "drop not noticed"
    assert wait_for(lambda: live_device.connected and live_device.config and live_device.json_mode, 10), "no reconnect"
    assert live_device.boots >= boots + 1                           # the sim prints a boot banner on connect


def test_disconnect_and_switch(live_device, store):
    live_device.disconnect()
    assert wait_for(lambda: not live_device.connected, 3)
    time.sleep(0.5)
    assert not live_device.connected                                  # and it stays down (no auto-reconnect)


def test_firmware_without_json_command_is_reported(store):
    from c6lmon.device import Device
    from c6lmon.sim import SimDevice

    class Old(SimDevice):
        def handle(self, line):
            if line.startswith("json"):
                return "unknown command 'json' - try 'help'\n"
            return super().handle(line)

    s = Old(interval=5).start()
    d = Device(store); d.start(); d.connect(s.url)
    try:
        assert wait_for(lambda: d.error and "no 'json' command" in d.error, 6)
    finally:
        d.stop(); s.stop()


def test_wrong_port_keeps_retrying_instead_of_dying(store):
    from c6lmon.device import Device
    d = Device(store); d.start(); d.connect("socket://127.0.0.1:1")
    try:
        assert wait_for(lambda: d.error and "cannot open" in d.error, 5)
        assert not d.connected
    finally:
        d.stop()


# ---- port listing / autodetect ---------------------------------------------------------------
class _P:
    def __init__(self, device, vid=None, desc="d"):
        self.device, self.vid, self.pid, self.description = device, vid, 1, desc


def test_macos_duplicate_tty_cu_entries_collapse_to_cu(monkeypatch):
    from c6lmon import device as devmod
    monkeypatch.setattr(devmod.serial.tools.list_ports, "comports", lambda: [
        _P("/dev/tty.usbmodem1101", 0x303A), _P("/dev/cu.usbmodem1101", 0x303A),
        _P("/dev/tty.Bluetooth-Incoming-Port"), _P("/dev/cu.Bluetooth-Incoming-Port")])
    got = [p["device"] for p in devmod.list_ports()]
    assert "/dev/cu.usbmodem1101" in got and "/dev/tty.usbmodem1101" not in got
    assert devmod.autodetect_port() == "/dev/cu.usbmodem1101"            # was None while it looked like two boards


def test_autodetect_refuses_to_guess_between_two_boards(monkeypatch):
    from c6lmon import device as devmod
    monkeypatch.setattr(devmod.serial.tools.list_ports, "comports",
                        lambda: [_P("/dev/ttyACM0", 0x303A), _P("/dev/ttyACM1", 0x303A)])
    assert devmod.autodetect_port() is None
    monkeypatch.setattr(devmod.serial.tools.list_ports, "comports", lambda: [_P("/dev/ttyUSB0", 0x10C4)])
    assert devmod.autodetect_port() is None                              # not an Espressif device
    assert devmod.list_ports()[0]["likely"] is False


def test_linux_ports_are_not_filtered(monkeypatch):
    from c6lmon import device as devmod
    monkeypatch.setattr(devmod.serial.tools.list_ports, "comports",
                        lambda: [_P("/dev/ttyACM0", 0x303A), _P("/dev/ttyS0")])
    assert [p["device"] for p in devmod.list_ports()] == ["/dev/ttyACM0", "/dev/ttyS0"]
