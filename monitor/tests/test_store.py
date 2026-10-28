import csv
import io
import json

from c6lmon.store import Store

T0 = 1_700_000_000.0


def pkt(ts, ok=1, node="SEN66-01", rssi=-5.0, snr=10.0, text="x", **kw):
    rec = {"ts": ts, "ok": ok, "err": 0 if ok else -7, "rssi": rssi, "snr": snr, "node": node, "text": text,
           "hex": "AA", "len": 1, "freq": 868.1, "bw": 125.0, "sf": 7, "cr": 7, "sync": 0x12}
    rec.update(kw)
    return rec


def fill(store, n=10, step=30.0, node="SEN66-01"):
    for i in range(n):
        store.insert_packet(pkt(T0 + i * step, node=node, rssi=-10.0 - i, snr=10.0 + i),
                            {"t": 20.0 + i, "co2": 600.0 + 10 * i})


def test_insert_and_fields_roundtrip(store):
    pid = store.insert_packet(pkt(T0), {"t": 21.5, "rh": 50.0})
    (row,) = store.list_packets()
    assert row["id"] == pid and row["fields"] == {"t": 21.5, "rh": 50.0}
    assert row["sf"] == 7 and row["sync"] == 0x12 and row["node"] == "SEN66-01"


def test_failed_packet_without_payload(store):
    store.insert_packet({"ts": T0, "ok": 0, "err": -7, "rssi": -19.0, "snr": -0.5})
    (row,) = store.list_packets()
    assert row["ok"] == 0 and row["hex"] is None and row["node"] == "" and row["fields"] == {}


def test_filters(store):
    fill(store, 6, node="A"); fill(store, 4, node="B")
    store.insert_packet(pkt(T0 + 500, ok=0, node="", text=None))
    assert store.count() == 11
    assert store.count({"node": "A"}) == 6 and store.count({"node": "B"}) == 4
    assert store.count({"node": ""}) == 1                                   # "" = untagged/undecoded
    assert store.count({"ok": False}) == 1 and store.count({"ok": True}) == 10
    assert store.count({"t_from": T0 + 60, "t_to": T0 + 90}) == 4           # inclusive bounds, both nodes
    assert store.count({"node": "A", "ok": True, "t_from": T0 + 30}) == 5


def test_text_search_escapes_like_wildcards(store):
    store.insert_packet(pkt(T0, text="100% sure"))
    store.insert_packet(pkt(T0 + 1, text="1000 sure"))
    store.insert_packet(pkt(T0 + 2, text="a_b"))
    store.insert_packet(pkt(T0 + 3, text="axb"))
    assert store.count({"q": "100%"}) == 1            # % is literal, not "anything"
    assert store.count({"q": "a_b"}) == 1             # _ is literal, not "any char"
    assert store.count({"q": "sure"}) == 2


def test_pagination_and_order(store):
    fill(store, 10)
    newest = store.list_packets(limit=3)
    assert [r["ts"] for r in newest] == [T0 + 270, T0 + 240, T0 + 210]
    oldest = store.list_packets(limit=3, offset=2, newest_first=False)
    assert [r["ts"] for r in oldest] == [T0 + 60, T0 + 90, T0 + 120]


def test_live_helpers(store):
    fill(store, 5)
    assert [r["id"] for r in store.latest_packets(3)] == [3, 4, 5]          # oldest first
    assert [r["id"] for r in store.packets_after(3)] == [4, 5]
    assert store.max_id() == 5 and store.packets_after(5) == []


def test_delete_cascades_to_readings(store):
    fill(store, 5)
    assert store.delete_packets(before_ts=T0 + 60) == 2
    assert store.count() == 3
    with store._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 6   # 3 packets x 2 fields
    assert store.delete_packets() == 3 and store.count() == 0
    with store._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0


def test_metrics_lists_fields_and_nodes(store):
    fill(store, 3, node="A")
    m = store.metrics()
    assert m["fields"] == ["co2", "t"] and m["nodes"] == ["A"]
    assert m["metrics"][:5] == ["rssi", "snr", "packets", "packets_ok", "packets_fail"]


def test_series_raw_field_and_rssi(store):
    fill(store, 5)
    s = store.series("t")
    assert s["bucket"] is None and s["points"] == [[(T0 + 30 * i) * 1000, 20.0 + i] for i in range(5)]
    r = store.series("rssi")
    assert [p[1] for p in r["points"]] == [-10.0, -11.0, -12.0, -13.0, -14.0]


def test_series_rssi_ok_filter(store):
    store.insert_packet(pkt(T0, rssi=-5)); store.insert_packet(pkt(T0 + 1, ok=0, rssi=-20))
    assert len(store.series("rssi")["points"]) == 2
    assert [p[1] for p in store.series("rssi", ok=True)["points"]] == [-5]


def test_series_bucketed_aggregates(store):
    fill(store, 10)                                        # t = 20..29, one per 30 s
    s = store.series("t", bucket=60)                       # two packets per bucket
    assert s["bucket"] == 60 and [p[1] for p in s["points"]] == [20.5, 22.5, 24.5, 26.5, 28.5]
    assert [p[1] for p in store.series("t", bucket=60, agg="max")["points"]] == [21, 23, 25, 27, 29]
    assert [p[1] for p in store.series("t", bucket=60, agg="min")["points"]] == [20, 22, 24, 26, 28]
    assert [p[1] for p in store.series("t", bucket=60, agg="count")["points"]] == [2] * 5
    assert store.series("t", bucket=60, agg="bogus")["points"] == s["points"]   # unknown agg -> avg


def test_series_auto_downsamples_big_ranges(store):
    for i in range(500):
        store.insert_packet(pkt(T0 + i), {"v": float(i)})
    s = store.series("v", max_points=100)
    assert s["bucket"] is not None and len(s["points"]) <= 101
    assert store.series("v", max_points=1000)["bucket"] is None             # small enough -> raw


def test_series_node_and_time_filters(store):
    fill(store, 5, node="A"); fill(store, 5, node="B")
    assert len(store.series("t", node="A")["points"]) == 5
    assert len(store.series("t", node="A", t_from=T0 + 60)["points"]) == 3
    assert store.series("nosuchfield")["points"] == []


def test_count_metrics(store):
    for i in range(6):
        store.insert_packet(pkt(T0 + i * 10, ok=1 if i % 3 else 0))         # i=0,3 fail
    assert sum(p[1] for p in store.series("packets", bucket=60)["points"]) == 6
    assert sum(p[1] for p in store.series("packets_ok", bucket=60)["points"]) == 4
    assert sum(p[1] for p in store.series("packets_fail", bucket=60)["points"]) == 2
    assert store.series("packets")["bucket"] == 60                          # default bucket


def test_report_overall_and_nodes(store):
    fill(store, 10, node="A")
    store.insert_packet(pkt(T0 + 5, ok=0, node="", rssi=-20.0, snr=-1.0))
    store.insert_packet(pkt(T0 + 6, node="", text="hello"))                  # decoded but untagged
    rep = store.report()
    assert rep["overall"]["total"] == 12 and rep["overall"]["ok"] == 11 and rep["overall"]["fail"] == 1
    assert rep["overall"]["success_pct"] == 91.7
    labels = {n["label"]: n for n in rep["nodes"]}
    assert set(labels) == {"A", "(untagged)"}
    assert labels["A"]["count"] == 10 and labels["A"]["interval_s"] == 30.0 and labels["A"]["missed"] == 0
    assert labels["A"]["rssi"] == {"min": -19.0, "avg": -14.5, "max": -10.0}
    assert rep["failed"]["count"] == 1 and rep["failed"]["rssi"]["avg"] == -20.0


def test_report_detects_missed_packets(store):
    for t in (0, 30, 60, 90, 180, 210, 240):                    # gap 90->180 = 3 intervals = 2 missing
        store.insert_packet(pkt(T0 + t))
    (n,) = store.report()["nodes"]
    assert n["interval_s"] == 30.0 and n["missed"] == 2
    assert n["completeness_pct"] == round(100 * 7 / 9, 1)


def test_report_needs_enough_packets_to_estimate(store):
    store.insert_packet(pkt(T0)); store.insert_packet(pkt(T0 + 30))
    (n,) = store.report()["nodes"]
    assert n["interval_s"] is None and n["missed"] is None


def test_report_time_range_and_empty(store):
    assert store.report()["overall"]["total"] == 0 and store.report()["timeline"]["points"] == []
    fill(store, 10)
    assert store.report(t_from=T0 + 120, t_to=T0 + 180)["overall"]["total"] == 3


def test_timeline_buckets_include_gaps(store):
    for t in (0, 10, 7200):                                       # two hours apart
        store.insert_packet(pkt(T0 + t))
    tl = store.report()["timeline"]
    assert tl["bucket_s"] in (60, 300, 900, 3600) and sum(p["ok"] for p in tl["points"]) == 3
    assert any(p["ok"] == 0 and p["fail"] == 0 for p in tl["points"])     # empty buckets are kept


def test_export_csv_is_wide(store):
    fill(store, 3)
    store.insert_packet(pkt(T0 + 99, text='with "quotes", commas\nand a newline'))
    rows = list(csv.reader(io.StringIO("".join(store.export_csv()))))
    head = rows[0]
    assert head[:3] == ["id", "time_utc", "ts"] and head[-2:] == ["co2", "t"]
    assert len(rows) == 5 and rows[1][head.index("t")] == "20.0" and rows[4][head.index("t")] == ""
    assert rows[4][head.index("text")] == 'with "quotes", commas\nand a newline'      # survives CSV quoting
    assert rows[1][1].endswith("+00:00")


def test_export_json_is_valid_and_filtered(store):
    fill(store, 4, node="A"); fill(store, 4, node="B")
    data = json.loads("".join(store.export_json_lines({"node": "B"})))
    assert len(data) == 4 and all(r["node"] == "B" and "time_utc" in r and "t" in r["fields"] for r in data)
    assert json.loads("".join(store.export_json_lines({"node": "none"}))) == []


def test_export_streams_more_than_one_batch(store):
    for i in range(1200):
        store.insert_packet(pkt(T0 + i), {"v": float(i)})
    assert len(json.loads("".join(store.export_json_lines()))) == 1200
    assert len(list(csv.reader(io.StringIO("".join(store.export_csv()))))) == 1201


def test_saved_items(store):
    store.put_saved("preset", "SEN66", {"sf": 7})
    store.put_saved("preset", "SEN66", {"sf": 9})                  # same name overwrites
    store.put_saved("graph", "g", {"series": []})
    assert [i["data"] for i in store.list_saved("preset")] == [{"sf": 9}]
    assert len(store.list_saved("graph")) == 1
    assert store.delete_saved("preset", "SEN66") is True and store.delete_saved("preset", "SEN66") is False


def test_reopen_keeps_data(tmp_path):
    p = str(tmp_path / "x.db")
    Store(p).insert_packet(pkt(T0), {"t": 1.0})
    assert Store(p).count() == 1
