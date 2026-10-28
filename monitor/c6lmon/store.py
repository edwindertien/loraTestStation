"""SQLite storage for received packets, plus the queries behind the Log,
Reports and Graphs tabs.

One file, stdlib only. Connections are opened per call (cheap for SQLite, and
safe across the serial thread and Flask's request threads); WAL mode lets
readers and the writer coexist.

Layout:
  packets   one row per RX attempt (decoded or not), with the radio settings
            it was received under
  readings  one row per numeric payload field (packet_id, key, value) - so a
            graph of any field is a plain indexed query, and new fields in a
            payload need no schema change
  saved     named presets / graph configs kept by the UI
"""
import csv
import io
import json
import math
import os
import sqlite3
import statistics
import time
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS packets (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL    NOT NULL,              -- host receive time, epoch seconds (UTC)
    up_ms  INTEGER,                       -- device uptime when received
    ok     INTEGER NOT NULL,              -- 1 = decoded, 0 = failed (CRC etc.)
    err    INTEGER NOT NULL DEFAULT 0,    -- RadioLib status code
    len    INTEGER,
    rssi   REAL,
    snr    REAL,
    hex    TEXT,
    text   TEXT,
    node   TEXT    NOT NULL DEFAULT '',
    freq   REAL, bw REAL, sf INTEGER, cr INTEGER, sync INTEGER
);
CREATE INDEX IF NOT EXISTS idx_packets_ts      ON packets(ts);
CREATE INDEX IF NOT EXISTS idx_packets_node_ts ON packets(node, ts);
CREATE TABLE IF NOT EXISTS readings (
    packet_id INTEGER NOT NULL REFERENCES packets(id) ON DELETE CASCADE,
    key       TEXT    NOT NULL,
    value     REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_readings_key ON readings(key, packet_id);
CREATE TABLE IF NOT EXISTS saved (
    kind    TEXT NOT NULL,
    name    TEXT NOT NULL,
    data    TEXT NOT NULL,
    updated REAL NOT NULL,
    PRIMARY KEY (kind, name)
);
"""

PACKET_COLS = ("ts", "up_ms", "ok", "err", "len", "rssi", "snr", "hex", "text",
               "node", "freq", "bw", "sf", "cr", "sync")
AGGS = {"avg": "AVG", "min": "MIN", "max": "MAX", "sum": "SUM", "count": "COUNT"}
COUNT_METRICS = ("packets", "packets_ok", "packets_fail")
ROW_METRICS = ("rssi", "snr")
SAVED_KINDS = ("preset", "graph")


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds")


def _like(q):
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _where(f, p=""):
    """Filters dict -> (" WHERE ...", params). `p` is a table alias prefix like "p."."""
    clauses, params = [], []
    f = f or {}
    if f.get("t_from") is not None:
        clauses.append(f"{p}ts >= ?"); params.append(f["t_from"])
    if f.get("t_to") is not None:
        clauses.append(f"{p}ts <= ?"); params.append(f["t_to"])
    if f.get("node") is not None:                      # "" means "untagged"
        clauses.append(f"{p}node = ?"); params.append(f["node"])
    if f.get("ok") is not None:
        clauses.append(f"{p}ok = ?"); params.append(1 if f["ok"] else 0)
    if f.get("q"):
        like = _like(f["q"])
        clauses.append(f"({p}text LIKE ? ESCAPE '\\' OR {p}hex LIKE ? ESCAPE '\\' OR {p}node LIKE ? ESCAPE '\\')")
        params += [like, like, like]
    return ((" WHERE " + " AND ".join(clauses)) if clauses else ""), params


def _stats(values):
    values = [v for v in values if v is not None]
    if not values:
        return None
    return {"min": round(min(values), 2), "avg": round(sum(values) / len(values), 2),
            "max": round(max(values), 2)}


class Store:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- writing ----------------------------------------------------------
    def insert_packet(self, rec, fields=None):
        cols = [c for c in PACKET_COLS if c in rec]
        with self._conn() as c:
            cur = c.execute(
                f"INSERT INTO packets ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [rec[k] for k in cols])
            pid = cur.lastrowid
            if fields:
                c.executemany("INSERT INTO readings (packet_id, key, value) VALUES (?,?,?)",
                              [(pid, k, v) for k, v in fields.items()])
        return pid

    def delete_packets(self, before_ts=None):
        """Delete everything, or only packets older than before_ts. Returns the count."""
        with self._conn() as c:
            if before_ts is None:
                n = c.execute("SELECT COUNT(*) FROM packets").fetchone()[0]
                c.execute("DELETE FROM packets")
            else:
                n = c.execute("SELECT COUNT(*) FROM packets WHERE ts < ?", (before_ts,)).fetchone()[0]
                c.execute("DELETE FROM packets WHERE ts < ?", (before_ts,))
            return n

    # ---- reading packets ----------------------------------------------------
    @staticmethod
    def _attach_fields(conn, rows):
        out = [dict(r) for r in rows]
        if not out:
            return out
        by_id = {r["id"]: r for r in out}
        for r in out:
            r["fields"] = {}
        ids = list(by_id)
        for i in range(0, len(ids), 500):                 # stay under SQLite's variable limit
            chunk = ids[i:i + 500]
            q = "SELECT packet_id, key, value FROM readings WHERE packet_id IN (%s)" % ",".join("?" * len(chunk))
            for rd in conn.execute(q, chunk):
                by_id[rd["packet_id"]]["fields"][rd["key"]] = rd["value"]
        return out

    def list_packets(self, filters=None, limit=100, offset=0, newest_first=True):
        where, params = _where(filters)
        order = "DESC" if newest_first else "ASC"
        with self._conn() as c:
            rows = c.execute(f"SELECT * FROM packets{where} ORDER BY ts {order}, id {order} LIMIT ? OFFSET ?",
                             params + [int(limit), int(offset)]).fetchall()
            return self._attach_fields(c, rows)

    def count(self, filters=None):
        where, params = _where(filters)
        with self._conn() as c:
            return c.execute(f"SELECT COUNT(*) FROM packets{where}", params).fetchone()[0]

    def packets_after(self, after_id, limit=500):
        with self._conn() as c:
            rows = c.execute("SELECT * FROM packets WHERE id > ? ORDER BY id ASC LIMIT ?",
                             (int(after_id), int(limit))).fetchall()
            return self._attach_fields(c, rows)

    def latest_packets(self, n=50):
        with self._conn() as c:
            rows = c.execute("SELECT * FROM packets ORDER BY id DESC LIMIT ?", (int(n),)).fetchall()
            return list(reversed(self._attach_fields(c, rows)))      # oldest first, like packets_after

    def max_id(self):
        with self._conn() as c:
            return c.execute("SELECT COALESCE(MAX(id), 0) FROM packets").fetchone()[0]

    # ---- export ---------------------------------------------------------------
    def _iter_all(self, filters):
        where, params = _where(filters)
        last_id = 0
        with self._conn() as c:
            while True:
                clause = (where + " AND" if where else " WHERE") + " id > ?"
                rows = c.execute(f"SELECT * FROM packets{clause} ORDER BY id ASC LIMIT 500",
                                 params + [last_id]).fetchall()
                if not rows:
                    return
                for r in self._attach_fields(c, rows):
                    yield r
                last_id = rows[-1]["id"]

    def export_json_lines(self, filters=None):
        """Yields a JSON array incrementally (so big exports don't sit in memory)."""
        yield "["
        first = True
        for r in self._iter_all(filters):
            r["time_utc"] = iso(r["ts"])
            yield ("" if first else ",") + "\n" + json.dumps(r, separators=(",", ":"))
            first = False
        yield "\n]\n"

    def export_csv(self, filters=None):
        """Wide CSV: base columns, then one column per numeric payload field."""
        where, params = _where(filters)
        with self._conn() as c:
            sub = f"SELECT id FROM packets{where}"
            keys = [r[0] for r in c.execute(
                f"SELECT DISTINCT key FROM readings WHERE packet_id IN ({sub}) ORDER BY key", params)]
        base = ["id", "time_utc", "ts", "up_ms", "ok", "err", "len", "rssi", "snr", "node",
                "text", "hex", "freq", "bw", "sf", "cr", "sync"]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(base + keys)
        yield buf.getvalue(); buf.seek(0); buf.truncate()
        for r in self._iter_all(filters):
            r["time_utc"] = iso(r["ts"])
            w.writerow([r.get(k) for k in base] + [r["fields"].get(k) for k in keys])
            yield buf.getvalue(); buf.seek(0); buf.truncate()

    # ---- metrics / series (Graphs tab) -------------------------------------------
    def metrics(self):
        with self._conn() as c:
            keys = [r[0] for r in c.execute("SELECT DISTINCT key FROM readings ORDER BY key")]
            nodes = [r[0] for r in c.execute("SELECT DISTINCT node FROM packets ORDER BY node")]
        return {"metrics": list(ROW_METRICS) + list(COUNT_METRICS) + keys, "fields": keys, "nodes": nodes}

    def series(self, metric, node=None, t_from=None, t_to=None, bucket=None, agg="avg",
               ok=None, max_points=4000):
        """-> {"metric", "node", "bucket", "points": [[epoch_ms, value], ...]}.

        Raw points when there are at most max_points of them; otherwise (or when
        a bucket is given) values are aggregated per time bucket of `bucket`
        seconds. Counting metrics (packets / packets_ok / packets_fail) are
        always bucketed - 60 s unless told otherwise.
        """
        agg = agg if agg in AGGS else "avg"
        try:
            bucket = int(bucket) if bucket else None
        except (TypeError, ValueError):
            bucket = None
        if bucket is not None and bucket < 1:
            bucket = None
        f = {"t_from": t_from, "t_to": t_to, "node": node}

        with self._conn() as c:
            if metric in COUNT_METRICS:
                f["ok"] = True if metric == "packets_ok" else False if metric == "packets_fail" else ok
                where, params = _where(f)
                b = bucket or 60
                rows = c.execute(
                    f"SELECT CAST(ts / {b} AS INTEGER) * {b} AS b, COUNT(*) AS v FROM packets{where} "
                    f"GROUP BY b ORDER BY b", params).fetchall()
                return {"metric": metric, "node": node, "bucket": b,
                        "points": [[r["b"] * 1000, r["v"]] for r in rows]}

            f["ok"] = ok
            where, params = _where(f, "p.")
            if metric in ROW_METRICS:
                from_sql, value = "packets p", f"p.{metric}"
                where = (where + " AND" if where else " WHERE") + f" p.{metric} IS NOT NULL"
            else:
                from_sql, value = "readings r JOIN packets p ON p.id = r.packet_id", "r.value"
                where = (where + " AND" if where else " WHERE") + " r.key = ?"
                params = params + [metric]

            n, tmin, tmax = c.execute(
                f"SELECT COUNT(*), MIN(p.ts), MAX(p.ts) FROM {from_sql}{where}", params).fetchone()
            if not n:
                return {"metric": metric, "node": node, "bucket": bucket, "points": []}
            if bucket is None and n > max_points:
                bucket = max(1, math.ceil((tmax - tmin) / max_points))
            if bucket:
                fn = AGGS[agg]
                rows = c.execute(
                    f"SELECT CAST(p.ts / {bucket} AS INTEGER) * {bucket} AS b, {fn}({value}) AS v "
                    f"FROM {from_sql}{where} GROUP BY b ORDER BY b", params).fetchall()
                pts = [[r["b"] * 1000, r["v"]] for r in rows]
            else:
                rows = c.execute(f"SELECT p.ts AS t, {value} AS v FROM {from_sql}{where} ORDER BY p.ts, p.id",
                                 params).fetchall()
                pts = [[r["t"] * 1000, r["v"]] for r in rows]
        return {"metric": metric, "node": node, "bucket": bucket, "points": pts}

    # ---- reports ----------------------------------------------------------------------
    def report(self, t_from=None, t_to=None):
        where, params = _where({"t_from": t_from, "t_to": t_to})
        with self._conn() as c:
            rows = c.execute(f"SELECT ts, ok, node, rssi, snr, sf FROM packets{where} ORDER BY ts, id",
                             params).fetchall()

        ok_rows = [r for r in rows if r["ok"]]
        fail_rows = [r for r in rows if not r["ok"]]
        total = len(rows)
        overall = {
            "total": total, "ok": len(ok_rows), "fail": len(fail_rows),
            "success_pct": round(100.0 * len(ok_rows) / total, 1) if total else None,
            "first": rows[0]["ts"] if rows else None, "last": rows[-1]["ts"] if rows else None,
            "rssi": _stats([r["rssi"] for r in rows]), "snr": _stats([r["snr"] for r in rows]),
        }

        by_node = {}
        for r in ok_rows:
            by_node.setdefault(r["node"], []).append(r)
        nodes = []
        for node, rs in sorted(by_node.items()):
            ts = [r["ts"] for r in rs]
            iv = self._interval_report(ts)
            nodes.append({"node": node, "label": node or "(untagged)", "count": len(rs),
                          "first": ts[0], "last": ts[-1],
                          "rssi": _stats([r["rssi"] for r in rs]), "snr": _stats([r["snr"] for r in rs]),
                          **iv})

        failed = {"count": len(fail_rows), "rssi": _stats([r["rssi"] for r in fail_rows]),
                  "snr": _stats([r["snr"] for r in fail_rows])}
        return {"overall": overall, "nodes": nodes, "failed": failed,
                "timeline": self._timeline(rows)}

    @staticmethod
    def _interval_report(ts):
        """Typical spacing between decoded packets, and how many look missing.

        Uses the median gap as the expected interval; any gap over 1.5x that is
        counted as round(gap / interval) - 1 missing packets. Needs 3+ packets
        to say anything. These are gaps in DECODED packets, so a packet that was
        heard but failed its CRC counts as missing here.
        """
        if len(ts) < 3:
            return {"interval_s": None, "missed": None, "completeness_pct": None}
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        interval = statistics.median(gaps)
        if interval <= 0:
            return {"interval_s": None, "missed": None, "completeness_pct": None}
        missed = sum(max(0, round(g / interval) - 1) for g in gaps if g > 1.5 * interval)
        return {"interval_s": round(interval, 1), "missed": missed,
                "completeness_pct": round(100.0 * len(ts) / (len(ts) + missed), 1)}

    @staticmethod
    def _timeline(rows):
        """Counts of decoded / failed packets per time bucket, empty buckets included."""
        if not rows:
            return {"bucket_s": 3600, "points": []}
        t0, t1 = rows[0]["ts"], rows[-1]["ts"]
        span = max(t1 - t0, 1.0)
        bucket = next((b for b in (60, 300, 900, 3600, 6 * 3600, 86400) if span / b <= 72), 86400)
        start = int(t0 // bucket) * bucket
        n = int((t1 - start) // bucket) + 1
        points = [{"t": start + i * bucket, "ok": 0, "fail": 0} for i in range(min(n, 2000))]
        for r in rows:
            i = int((r["ts"] - start) // bucket)
            if 0 <= i < len(points):
                points[i]["ok" if r["ok"] else "fail"] += 1
        return {"bucket_s": bucket, "points": points}

    # ---- saved presets / graph configs ------------------------------------------------------
    def list_saved(self, kind):
        with self._conn() as c:
            return [{"name": r["name"], "data": json.loads(r["data"]), "updated": r["updated"]}
                    for r in c.execute("SELECT * FROM saved WHERE kind = ? ORDER BY name", (kind,))]

    def put_saved(self, kind, name, data):
        with self._conn() as c:
            c.execute("INSERT INTO saved (kind, name, data, updated) VALUES (?,?,?,?) "
                      "ON CONFLICT(kind, name) DO UPDATE SET data = excluded.data, updated = excluded.updated",
                      (kind, name, json.dumps(data), time.time()))

    def delete_saved(self, kind, name):
        with self._conn() as c:
            return c.execute("DELETE FROM saved WHERE kind = ? AND name = ?", (kind, name)).rowcount > 0
