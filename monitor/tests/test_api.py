import csv
import io
import json

import pytest

from conftest import wait_for
from c6lmon import device as devmod
from c6lmon.app import create_app

T0 = 1_700_000_000.0


@pytest.fixture
def client(store, live_device):
    return create_app(store, live_device).test_client()


def seed(store, n=10, node="A", step=30.0, start=T0):
    for i in range(n):
        store.insert_packet({"ts": start + i * step, "ok": 1, "err": 0, "len": 5, "rssi": -10.0 - i, "snr": 10.0,
                             "hex": "68656C6C6F", "text": f"<{node}>x", "node": node, "freq": 868.1, "bw": 125.0,
                             "sf": 7, "cr": 7, "sync": 18}, {"t": 20.0 + i})


# ---- status / connection ---------------------------------------------------------------
def test_status(client):
    j = client.get("/api/status").get_json()
    assert j["device"]["connected"] and j["device"]["config"]["sf"] == 7 and j["device"]["json_mode"]
    assert isinstance(j["db"]["packets"], int)


def test_ports_listing(client, monkeypatch):
    monkeypatch.setattr(devmod, "list_ports",
                        lambda: [{"device": "/dev/x", "description": "d", "vid": 0x303A, "pid": 1, "likely": True}])
    j = client.get("/api/ports").get_json()
    assert j["ports"][0]["device"] == "/dev/x" and j["current"]


def test_connect_needs_a_port(client):
    assert client.post("/api/connect", json={}).status_code == 400
    assert client.post("/api/connect", json={"port": "   "}).status_code == 400


def test_disconnect_then_reconnect(client, live_device, sim):
    assert client.post("/api/disconnect", json={}).get_json()["ok"]
    assert wait_for(lambda: not live_device.connected, 3)
    assert client.post("/api/config", json={"sf": 9}).status_code == 409           # needs a device
    assert client.post("/api/connect", json={"port": sim.url}).get_json()["ok"]
    assert wait_for(lambda: live_device.connected and live_device.config, 8)


# ---- settings -------------------------------------------------------------------------------
def test_apply_config_ok(client, sim):
    j = client.post("/api/config", json={"sf": 9, "freq": 868.1}).get_json()
    assert j["ok"] and [r["key"] for r in j["results"]] == ["sf"] and j["config"]["sf"] == 9 and sim.cfg["sf"] == 9


def test_apply_config_reports_the_devices_reason(client):
    j = client.post("/api/config", json={"sf": 99}).get_json()
    assert not j["ok"] and j["results"][0]["reply"] == "err spreading factor must be 5-12"
    assert j["config"]["sf"] == 7


def test_apply_config_ignores_unknown_keys(client, sim):
    j = client.post("/api/config", json={"evil": "rm -rf /", "sf": 8}).get_json()
    assert [r["key"] for r in j["results"]] == ["sf"] and sim.cfg["sf"] == 8


def test_save_and_defaults(client, sim):
    client.post("/api/config", json={"sf": 8})
    assert client.post("/api/config/save", json={}).get_json()["config"]["state"] == "saved" and sim.saved["sf"] == 8
    j = client.post("/api/config/defaults", json={}).get_json()
    assert j["ok"] and j["config"]["sf"] == 7 and sim.saved is None


def test_send_raw_line_shows_up_in_console(client):
    assert client.post("/api/send", json={"line": "stats"}).get_json()["ok"]
    assert wait_for(lambda: any("RX ok:" in c["text"] for c in client.get("/api/live").get_json()["console"]), 4)
    assert client.post("/api/send", json={"line": "  "}).status_code == 400


# ---- live ---------------------------------------------------------------------------------------
def test_live_first_load_then_incremental(client, store):
    assert wait_for(lambda: store.count() >= 3)
    first = client.get("/api/live?after=-1&limit=2").get_json()
    ids = [p["id"] for p in first["packets"]]
    assert len(ids) == 2 and ids == sorted(ids)                                     # oldest first
    nxt = client.get(f"/api/live?after={ids[-1]}").get_json()
    assert all(p["id"] > ids[-1] for p in nxt["packets"])
    assert first["device"]["connected"] and first["db"]["max_id"] >= ids[-1]


def test_live_console_is_incremental(client):
    all_ = client.get("/api/live").get_json()["console"]
    assert all_
    last = max(c["seq"] for c in all_)
    assert client.get(f"/api/live?console_after={last}").get_json()["console"] == []


# ---- stored packets -------------------------------------------------------------------------------
def test_packets_filters_and_pagination(client, store, live_device):
    live_device.disconnect()                                   # quiet: only our seeded rows
    wait_for(lambda: not live_device.connected, 3)
    base = store.count()
    seed(store, 10, "A"); seed(store, 5, "B", start=T0 + 1000)
    j = client.get("/api/packets?node=A&limit=3&offset=2").get_json()
    assert j["total"] == 10 and len(j["packets"]) == 3 and j["packets"][0]["ts"] == T0 + 7 * 30
    j = client.get(f"/api/packets?node=B&order=asc&from={T0 + 1000 + 30}&to={T0 + 1000 + 90}").get_json()
    assert j["total"] == 3 and [p["ts"] for p in j["packets"]] == [T0 + 1030, T0 + 1060, T0 + 1090]
    assert client.get("/api/packets?q=%3CB%3E").get_json()["total"] == 5          # text search "<B>"
    assert client.get("/api/packets?ok=fail&node=A").get_json()["total"] == 0
    assert client.get("/api/packets").get_json()["total"] == base + 15


def test_packets_junk_params_do_not_crash(client):
    for q in ("from=abc", "limit=-5", "limit=zzz", "offset=zzz", "to=", "ok=maybe", "order=sideways"):
        assert client.get("/api/packets?" + q).status_code == 200


def test_export_csv(client, store, live_device):
    live_device.disconnect(); wait_for(lambda: not live_device.connected, 3)
    seed(store, 4, "A")
    r = client.get("/api/packets/export.csv?node=A")
    assert r.status_code == 200 and r.mimetype == "text/csv" and "attachment" in r.headers["Content-Disposition"]
    rows = list(csv.reader(io.StringIO(r.get_data(as_text=True))))
    assert rows[0][-1] == "t" and len(rows) == 5


def test_export_json_and_bad_format(client, store, live_device):
    live_device.disconnect(); wait_for(lambda: not live_device.connected, 3)
    seed(store, 3, "A")
    data = json.loads(client.get("/api/packets/export.json?node=A").get_data(as_text=True))
    assert len(data) == 3 and data[0]["fields"]["t"] == 20.0
    assert client.get("/api/packets/export.xml").status_code == 400


def test_delete_needs_an_explicit_scope(client, store, live_device):
    live_device.disconnect(); wait_for(lambda: not live_device.connected, 3)
    seed(store, 10, "A")
    assert client.delete("/api/packets").status_code == 400                       # no accidental wipe
    n = store.count()
    j = client.delete(f"/api/packets?before={T0 + 90}").get_json()
    assert j["deleted"] >= 3 and store.count() == n - j["deleted"]
    remaining = store.count()
    assert remaining > 0
    assert client.delete("/api/packets?all=1").get_json()["deleted"] == remaining
    assert store.count() == 0


# ---- graphs / reports ---------------------------------------------------------------------------------
def test_metrics_series_and_report(client, store, live_device):
    live_device.disconnect(); wait_for(lambda: not live_device.connected, 3)
    store.delete_packets()
    seed(store, 12, "A")
    m = client.get("/api/metrics").get_json()
    assert "t" in m["fields"] and "A" in m["nodes"] and "rssi" in m["metrics"]
    s = client.get("/api/series?metric=t&node=A").get_json()
    assert len(s["points"]) == 12 and s["points"][0] == [T0 * 1000, 20.0]
    b = client.get("/api/series?metric=t&node=A&bucket=60&agg=max").get_json()
    assert b["bucket"] == 60 and b["points"][0][1] == 21.0
    assert client.get("/api/series").status_code == 400
    r = client.get("/api/report").get_json()
    assert r["overall"]["ok"] == 12 and r["nodes"][0]["interval_s"] == 30.0 and r["nodes"][0]["missed"] == 0
    csvtext = client.get("/api/report.csv").get_data(as_text=True)
    assert csvtext.splitlines()[0].startswith("node,decoded") and "A,12" in csvtext


# ---- saved presets / graph configs --------------------------------------------------------------------
def test_saved_crud(client):
    assert client.put("/api/saved/preset", json={"name": "SEN66", "data": {"sf": 7, "freq": 868.1}}).get_json()["ok"]
    items = client.get("/api/saved/preset").get_json()["items"]
    assert items[0]["name"] == "SEN66" and items[0]["data"]["sf"] == 7
    assert client.delete("/api/saved/preset?name=SEN66").get_json()["ok"]
    assert client.get("/api/saved/preset").get_json()["items"] == []
    assert client.delete("/api/saved/preset?name=SEN66").get_json()["ok"] is False


def test_saved_names_may_contain_anything_printable(client):
    from urllib.parse import quote
    for name in ("my <b>preset</b>", "a/b", "a//b/../c", "100% #1 ?x=y", "caf\u00e9 \u2615"):
        assert client.put("/api/saved/graph", json={"name": name, "data": {"k": 1}}).get_json()["ok"], name
    names = [i["name"] for i in client.get("/api/saved/graph").get_json()["items"]]
    assert sorted(names) == sorted(["my <b>preset</b>", "a/b", "a//b/../c", "100% #1 ?x=y", "caf\u00e9 \u2615"])
    assert client.delete("/api/saved/graph?name=" + quote("a/b", safe="")).get_json()["ok"]
    assert "a/b" not in [i["name"] for i in client.get("/api/saved/graph").get_json()["items"]]


def test_saved_validation(client):
    assert client.get("/api/saved/nonsense").status_code == 404
    assert client.put("/api/saved/nonsense", json={"name": "x", "data": {}}).status_code == 404
    assert client.put("/api/saved/preset", json=[1, 2]).status_code == 400                      # body must be an object
    assert client.put("/api/saved/preset", json={"name": "x", "data": [1]}).status_code == 400  # data must be an object
    assert client.put("/api/saved/preset", json={"name": "n" * 100, "data": {}}).status_code == 400
    assert client.put("/api/saved/preset", json={"name": "", "data": {}}).status_code == 400
    assert client.put("/api/saved/preset", json={"name": "bad\nname", "data": {}}).status_code == 400


# ---- misc ------------------------------------------------------------------------------------------------
def test_device_log_download(client):
    r = client.get("/api/log/device")
    assert r.status_code == 200 and b"json=on" in r.data


def test_unknown_api_route_is_json_404(client):
    r = client.get("/api/nope")
    assert r.status_code == 404 and r.get_json() == {"error": "not found"}


def test_writes_require_json_content_type(client):
    # What a cross-site HTML form could send: no JSON content type.
    for path in ("/api/disconnect", "/api/config/defaults", "/api/config/save", "/api/config", "/api/connect"):
        r = client.post(path, data="x=1", content_type="application/x-www-form-urlencoded")
        assert r.status_code == 415, path
    assert client.put("/api/saved/preset", data="{}", content_type="text/plain").status_code == 415


def test_security_headers(client):
    r = client.get("/api/status")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" not in r.headers                                  # JSON API: not needed
    r = client.get("/nonexistent-page")
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]


def test_hostile_payload_is_returned_as_inert_json_text(client, store, live_device):
    live_device.disconnect(); wait_for(lambda: not live_device.connected, 3)
    evil = "<script>alert(1)</script>"
    store.insert_packet({"ts": T0, "ok": 1, "err": 0, "len": 1, "text": evil, "node": "script", "hex": "AA"})
    r = client.get("/api/packets?q=script")
    assert r.mimetype == "application/json" and r.get_json()["packets"][0]["text"] == evil
