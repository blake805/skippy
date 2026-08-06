"""The BLE bridge's protocol layer: framing, annotation, and the hello dance.

The relay itself needs a radio and a hub; what needs coverage here is the part
that can silently corrupt a session if it is wrong — reassembling lines from
arbitrary BLE chunks, stamping transport onto telemetry without touching
replies, and recognizing the node's hello. All of it is pure, which is why
skippy_ble_bridge keeps bleak imports out of module scope.
"""

import json

from skippy_ble_bridge import (
    LineAssembler,
    annotate_for_hub,
    chunk_for_ble,
    hello_line,
    node_name_from_hello,
)


# --- line reassembly ---------------------------------------------------------

def test_assembler_reassembles_a_line_split_across_chunks():
    assembler = LineAssembler()
    assert assembler.feed(b'{"task_id": "t1", ') == []
    assert assembler.feed(b'"ok": true}') == []
    assert assembler.feed(b"\n") == ['{"task_id": "t1", "ok": true}']


def test_assembler_returns_every_line_in_one_chunk():
    assembler = LineAssembler()
    lines = assembler.feed(b'{"a": 1}\n{"b": 2}\n{"c": 3}\n')
    assert lines == ['{"a": 1}', '{"b": 2}', '{"c": 3}']


def test_assembler_keeps_a_trailing_partial_for_the_next_chunk():
    assembler = LineAssembler()
    assert assembler.feed(b'{"a": 1}\n{"b"') == ['{"a": 1}']
    assert assembler.feed(b": 2}\n") == ['{"b": 2}']


def test_assembler_skips_blank_lines():
    assembler = LineAssembler()
    assert assembler.feed(b"\n\n{\"a\": 1}\n\n") == ['{"a": 1}']


def test_assembler_drops_endless_garbage_instead_of_hoarding_it():
    # A peer that never sends a newline is not speaking the protocol; the
    # buffer resets rather than growing until the laptop notices.
    assembler = LineAssembler()
    assert assembler.feed(b"x" * (LineAssembler.MAX_LINE + 10)) == []
    # And the stream recovers once real frames resume.
    assembler.feed(b"\n")
    assert assembler.feed(b'{"ok": true}\n') == ['{"ok": true}']


def test_chunks_roundtrip_through_the_assembler():
    line = json.dumps({"task_id": "t9", "ok": True, "result": {"data_hex": "ab" * 2048}})
    assembler = LineAssembler()
    out = []
    for piece in chunk_for_ble(line, chunk_size=180):
        assert len(piece) <= 180
        out.extend(assembler.feed(piece))
    assert out == [line]


# --- telemetry annotation ------------------------------------------------------

def test_node_status_gets_the_transport_stamp():
    line = json.dumps({"type": "node_status", "node": "bench", "battery": 88})
    out = json.loads(annotate_for_hub(line))
    assert out["transport"] == "ble"
    assert out["battery"] == 88


def test_replies_pass_through_byte_for_byte():
    # Task replies must never be rewritten by the relay, whitespace included:
    # forwarding the original text is the property under test.
    line = '{"task_id": "t1",  "ok": true,   "result": {"mv": 3300}}'
    assert annotate_for_hub(line) is line


def test_non_json_passes_through_untouched():
    assert annotate_for_hub("not json at all") == "not json at all"


# --- the hello dance -----------------------------------------------------------

def test_hello_line_carries_the_token():
    data = json.loads(hello_line("sekrit"))
    assert data == {"type": "hello", "token": "sekrit"}


def test_node_hello_yields_the_name_the_hub_routes_by():
    line = json.dumps(
        {"type": "hello", "role": "devices", "node": "bench", "firmware": "io-node 1.3"}
    )
    assert node_name_from_hello(line) == "bench"


def test_everything_else_is_not_a_hello():
    assert node_name_from_hello(json.dumps({"type": "node_status", "node": "bench"})) is None
    assert node_name_from_hello(json.dumps({"type": "hello"})) is None  # nameless
    assert node_name_from_hello("garbage") is None
