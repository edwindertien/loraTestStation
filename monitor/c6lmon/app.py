"""Flask app: a JSON API plus the single-page UI in static/.

Deliberately thin - validation and plumbing only; the work is in store.py
(SQLite) and device.py (the serial link).
"""
import csv
import io
import os
import re
import time

from flask import Flask, Response, jsonify, request, send_file

from . import device as devmod
from .store import SAVED_KINDS, iso

_NAME_RE = re.compile(r"^[^\x00-\x1f]{1,64}$")


def _fnum(name):
    v = request.args.get(name)
    if v in (None, ""):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _filters():
    ok = request.args.get("ok")
    return {
        "t_from": _fnum("from"), "t_to": _fnum("to"),
        "node": request.args.get("node"),                 # absent = any node; "" = untagged
        "ok": True if ok in ("1", "ok", "true") else False if ok in ("0", "fail", "false") else None,
        "q": (request.args.get("q") or "").strip() or None,
    }


def _clamp(v, lo, hi, default):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def _download(gen, filename, mime):
    return Response(gen, mimetype=mime, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def create_app(store, device):
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.json.sort_keys = False

    # ---- hardening: this app has state-changing endpoints and shows text that arrives
    # over the air from anyone's transmitter, so be conservative ---------------------
    @app.before_request
    def require_json_for_writes():
        # A cross-site <form> can POST but cannot set Content-Type: application/json, so
        # requiring it stops a malicious web page from blindly poking e.g.
        # /api/config/defaults on a locally running monitor. (DELETE needs a CORS
        # preflight anyway, which this app never grants.)
        if request.method in ("POST", "PUT") and request.path.startswith("/api/") and not request.is_json:
            return jsonify(error="send Content-Type: application/json"), 415

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        if not request.path.startswith("/api/"):
            # All scripts/styles are served from this app (Chart.js is vendored); a payload
            # that slipped markup past the UI's escaping still couldn't load or run anything.
            resp.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        return resp

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.errorhandler(404)
    def not_found(_e):
        if request.path.startswith("/api/"):
            return jsonify(error="not found"), 404
        return "not found", 404

    # ---- connection / status -------------------------------------------------
    @app.get("/api/status")
    def status():
        return jsonify(device=device.status(), db={"packets": store.count(), "max_id": store.max_id()})

    @app.get("/api/ports")
    def ports():
        return jsonify(ports=devmod.list_ports(), current=device.port, want=device.want_port)

    @app.post("/api/connect")
    def connect():
        port = ((request.get_json(silent=True) or {}).get("port") or "").strip()
        if not port:
            return jsonify(error="port is required"), 400
        device.connect(port)
        return jsonify(ok=True)

    @app.post("/api/disconnect")
    def disconnect():
        device.disconnect()
        return jsonify(ok=True)

    @app.post("/api/send")
    def send():
        line = ((request.get_json(silent=True) or {}).get("line") or "")
        ok, msg = device.send_raw(line)
        return jsonify(ok=ok, message=msg), (200 if ok else 400)

    # ---- radio settings ----------------------------------------------------------
    def _need_device():
        if not device.connected:
            return jsonify(error="not connected to a device"), 409
        return None

    @app.post("/api/config")
    def set_config():
        bad = _need_device()
        if bad:
            return bad
        want = request.get_json(silent=True) or {}
        results = device.apply_config({k: want[k] for k in devmod.CFG_ORDER if k in want})
        return jsonify(ok=all(r["ok"] for r in results), results=results, config=device.config)

    @app.post("/api/config/save")
    def save_config():
        bad = _need_device()
        if bad:
            return bad
        ok, reply = device.save_config()
        return jsonify(ok=ok, reply=reply, config=device.config)

    @app.post("/api/config/defaults")
    def defaults_config():
        bad = _need_device()
        if bad:
            return bad
        ok, reply = device.restore_defaults()
        return jsonify(ok=ok, reply=reply, config=device.config)

    # ---- live view -----------------------------------------------------------------------
    @app.get("/api/live")
    def live():
        after = _clamp(request.args.get("after"), -1, 1 << 62, 0)
        pk = store.latest_packets(_clamp(request.args.get("limit"), 1, 200, 50)) if after < 0 \
            else store.packets_after(after, 200)
        cafter = _clamp(request.args.get("console_after"), 0, 1 << 62, 0)
        return jsonify(device=device.status(), packets=pk, console=device.console_after(cafter),
                       db={"packets": store.count(), "max_id": store.max_id()})

    # ---- stored packets --------------------------------------------------------------------
    @app.get("/api/packets")
    def packets():
        f = _filters()
        limit = _clamp(request.args.get("limit"), 1, 1000, 100)
        offset = _clamp(request.args.get("offset"), 0, 1 << 40, 0)
        newest = request.args.get("order", "desc") != "asc"
        return jsonify(total=store.count(f), limit=limit, offset=offset,
                       packets=store.list_packets(f, limit, offset, newest))

    @app.get("/api/packets/export.<fmt>")
    def export(fmt):
        f = _filters()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        if fmt == "csv":
            return _download(store.export_csv(f), f"c6l-packets-{stamp}.csv", "text/csv")
        if fmt == "json":
            return _download(store.export_json_lines(f), f"c6l-packets-{stamp}.json", "application/json")
        return jsonify(error="format must be csv or json"), 400

    @app.delete("/api/packets")
    def delete_packets():
        before = _fnum("before")
        if before is None and request.args.get("all") != "1":
            return jsonify(error="give ?before=<epoch seconds>, or ?all=1 to delete everything"), 400
        return jsonify(deleted=store.delete_packets(before))

    # ---- graphs / reports -------------------------------------------------------------------
    @app.get("/api/metrics")
    def metrics():
        return jsonify(store.metrics())

    @app.get("/api/series")
    def series():
        metric = request.args.get("metric")
        if not metric:
            return jsonify(error="metric is required"), 400
        f = _filters()
        return jsonify(store.series(metric, node=f["node"], t_from=f["t_from"], t_to=f["t_to"],
                                    bucket=_clamp(request.args.get("bucket"), 0, 31 * 86400, 0) or None,
                                    agg=request.args.get("agg", "avg"), ok=f["ok"]))

    @app.get("/api/report")
    def report():
        return jsonify(store.report(_fnum("from"), _fnum("to")))

    @app.get("/api/report.csv")
    def report_csv():
        rep = store.report(_fnum("from"), _fnum("to"))
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["node", "decoded", "first_utc", "last_utc", "interval_s", "missed", "completeness_pct",
                    "rssi_min", "rssi_avg", "rssi_max", "snr_min", "snr_avg", "snr_max"])
        for n in rep["nodes"]:
            r, s = n["rssi"] or {}, n["snr"] or {}
            w.writerow([n["label"], n["count"], iso(n["first"]), iso(n["last"]), n["interval_s"], n["missed"],
                        n["completeness_pct"], r.get("min"), r.get("avg"), r.get("max"),
                        s.get("min"), s.get("avg"), s.get("max")])
        return _download(buf.getvalue(), f"c6l-report-{time.strftime('%Y%m%d-%H%M%S')}.csv", "text/csv")

    # ---- saved presets / graph configs --------------------------------------------------------
    def _kind(kind):
        return kind if kind in SAVED_KINDS else None

    @app.get("/api/saved/<kind>")
    def saved_list(kind):
        if not _kind(kind):
            return jsonify(error="unknown kind"), 404
        return jsonify(items=store.list_saved(kind))

    # The name travels in the body / query string, never in the URL path: a "/" (or %2F, ?, #)
    # in a user-chosen name would otherwise break routing.
    @app.put("/api/saved/<kind>")
    def saved_put(kind):
        if not _kind(kind):
            return jsonify(error="unknown kind"), 404
        body = request.get_json(silent=True)
        name = body.get("name") if isinstance(body, dict) else None
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(name, str) or not _NAME_RE.match(name) or not isinstance(data, dict):
            return jsonify(error='need {"name": 1-64 chars, "data": {...}}'), 400
        store.put_saved(kind, name, data)
        return jsonify(ok=True)

    @app.delete("/api/saved/<kind>")
    def saved_delete(kind):
        if not _kind(kind):
            return jsonify(error="unknown kind"), 404
        return jsonify(ok=store.delete_saved(kind, request.args.get("name", "")))

    # ---- raw device log -------------------------------------------------------------------------
    @app.get("/api/log/device")
    def device_log():
        path = device.devlog_path
        if not path or not os.path.exists(path):
            return jsonify(error="no device log yet"), 404
        return send_file(path, mimetype="text/plain", as_attachment=True, download_name="c6l-device.log")

    return app
