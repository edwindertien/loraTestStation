"""Turn a received LoRa payload into (text, node, numeric fields).

The SEN66 nodes send  <NODE-ID>{"t":19.92,"rh":61.9,...}  as plain text. This
stays generic on purpose: any "<tag>" prefix becomes the node name, and any
JSON object after it contributes its numeric values as graphable fields.

Payloads come from the air, so they are untrusted: nothing here assumes they
are text, valid JSON, or sane sizes.
"""
import json
import math
import re

# Node IDs look like SEN66-01: letters, digits and a few separators. Anything wilder
# (quotes, brackets, unicode...) is not treated as a node name, so hostile or garbled
# payloads can't put odd strings into the node filter list and the database.
_NODE_RE = re.compile(r"^<([A-Za-z0-9_.:\-]{1,32})>(.*)$", re.S)
_MAX_FIELDS = 64        # a hostile or garbled payload can't create unbounded keys
_MAX_KEY_LEN = 48


def decode_payload(data):
    """bytes -> {"text": str|None, "node": str, "fields": {key: float}}.

    text is None for anything that isn't printable UTF-8 (binary payloads such
    as encrypted LoRaWAN frames) - those are kept as hex only.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {"text": None, "node": "", "fields": {}}
    if not all(c.isprintable() or c in "\r\n\t" for c in text):
        return {"text": None, "node": "", "fields": {}}

    node, body = "", text
    m = _NODE_RE.match(text)
    if m:
        node, body = m.group(1), m.group(2)
    return {"text": text, "node": node, "fields": numeric_fields(body)}


def numeric_fields(body):
    """Flatten the numeric leaves of a JSON object: {"a":{"b":1}} -> {"a.b":1.0}."""
    try:
        obj = json.loads(body)
    except ValueError:
        return {}
    if not isinstance(obj, dict):
        return {}

    out = {}

    def walk(prefix, value):
        if len(out) >= _MAX_FIELDS:
            return
        if isinstance(value, bool):            # bool is an int subclass - not a measurement
            return
        if isinstance(value, (int, float)):
            f = float(value)
            if math.isfinite(f) and prefix:
                out[prefix[:_MAX_KEY_LEN]] = f
        elif isinstance(value, dict):
            for k, v in value.items():
                walk(f"{prefix}.{k}" if prefix else str(k), v)

    walk("", obj)
    return out


def hex_to_bytes(h):
    """Strict hex -> bytes; None if it isn't clean even-length hex."""
    if not isinstance(h, str) or len(h) % 2 or not re.fullmatch(r"[0-9A-Fa-f]*", h):
        return None
    return bytes.fromhex(h)
