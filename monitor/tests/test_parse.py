import json

from c6lmon import parse


def test_sen66_payload():
    d = parse.decode_payload(b'<SEN66-01>{"t":19.92,"rh":61.9,"co2":733,"voc":99,"nox":1,"pm25":2.0,"pm10":3.1,"pm1":1.0}')
    assert d["node"] == "SEN66-01"
    assert d["fields"] == {"t": 19.92, "rh": 61.9, "co2": 733.0, "voc": 99.0, "nox": 1.0,
                           "pm25": 2.0, "pm10": 3.1, "pm1": 1.0}
    assert d["text"].startswith("<SEN66-01>{")


def test_plain_text_has_no_node_or_fields():
    d = parse.decode_payload(b"hello world")
    assert (d["text"], d["node"], d["fields"]) == ("hello world", "", {})


def test_tag_with_non_json_body():
    d = parse.decode_payload(b"<SEN66-01>hello world")
    assert (d["node"], d["fields"]) == ("SEN66-01", {})


def test_binary_payload_is_hex_only():
    assert parse.decode_payload(bytes([0x40, 0x09, 0xE8, 0x00, 0xFE, 0x80]))["text"] is None
    assert parse.decode_payload(b"\x00\x01\x02")["text"] is None      # control chars are not text


def test_tag_must_look_like_a_node_id():
    assert parse.decode_payload(b"<not a tag>{}")["node"] == ""
    assert parse.decode_payload(b"<" + b"x" * 40 + b">{}")["node"] == ""   # absurdly long -> not a node name
    for odd in (b'<a"b>{}', b"<a'b>{}", b"<a/b>{}", b"<a;b>{}", "<näme>{}".encode()):
        assert parse.decode_payload(odd)["node"] == ""                       # no quotes/slashes/unicode in node names
    for good in (b"<SEN66-01>{}", b"<node_2.a:b>{}", b"<7>{}"):
        assert parse.decode_payload(good)["node"] == good[1:good.index(b">")].decode()


def test_nested_json_is_flattened_and_bools_skipped():
    f = parse.numeric_fields('{"a":{"b":1,"c":{"d":2.5}},"ok":true,"name":"x","list":[1,2]}')
    assert f == {"a.b": 1.0, "a.c.d": 2.5}


def test_non_finite_numbers_are_dropped():
    assert parse.numeric_fields('{"x":NaN,"y":Infinity,"z":1}') == {"z": 1.0}


def test_top_level_scalars_and_arrays_ignored():
    assert parse.numeric_fields("5") == {}
    assert parse.numeric_fields("[1,2,3]") == {}
    assert parse.numeric_fields("not json") == {}


def test_field_count_is_capped():
    body = json.dumps({f"k{i}": i for i in range(500)})
    assert len(parse.numeric_fields(body)) == 64


def test_hex_to_bytes_is_strict():
    assert parse.hex_to_bytes("68656C6C6F") == b"hello"
    assert parse.hex_to_bytes("") == b""
    for bad in ("6", "6G", "zz", None, 12, "68 65"):
        assert parse.hex_to_bytes(bad) is None
