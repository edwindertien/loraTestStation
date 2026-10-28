# Tests

```
cd monitor
python -m venv venv && source venv/bin/activate && pip install -r requirements.txt pytest
python -m pytest tests -q                      # ~45 s; backend, serial link, API
```

| File | Covers |
|---|---|
| `test_parse.py` | payload decoding: node tags, JSON fields, binary, hostile input |
| `test_store.py` | SQLite layer: filters, series/bucketing, reports (interval/missed), CSV/JSON export, presets |
| `test_device.py` | the serial link against the firmware's own captured output lines (`fixtures/`) and against the simulator over TCP: handshake, settings, reconnect, timeouts, port listing |
| `test_serial_node.py` | the same over a real serial device node (a pty), i.e. pyserial's POSIX path |
| `test_api.py` | every HTTP endpoint, input validation, security headers |
| `ui/` | the real page driven in jsdom against the real server + simulator (below) |

## UI smoke test (optional, needs Node)

```
cd monitor/tests/ui && npm install && cd ../.. && python tests/ui/run_smoke.py
```

Starts the app with `--sim`, plants hostile packets in its database, loads the page in jsdom and
exercises every tab. It also proves received text can't become markup: the planted payloads contain
`<script>` and `<img onerror>`. The chart canvas is mocked, so it checks what the app hands to
Chart.js, not the pixels.

## Fixtures

`fixtures/firmware_lines.txt` is output captured from the firmware's own code by the host test in
`firmware/test/host/` (`./run.sh --write-fixtures ../../../monitor/tests/fixtures/firmware_lines.txt`).
If the firmware's serial format changes, regenerate it and these tests show exactly what moved.
