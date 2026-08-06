"""Live device tools for RE mode: reads free, writes gated, coding mode blind."""

import pytest

import skippy_device
import skippy_dispatch
import tool_schemas
from skippy_sandbox import Sandbox
from tests.fake_bridge import RUN_CLIENT_ID, FakeBridge, bridged_service


@pytest.fixture
def box(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return Sandbox([str(root)])


@pytest.fixture
def service():
    svc = skippy_device.DeviceService()

    async def always_approve(payload):
        return {"status": "APPROVE"}

    svc._test_approver = always_approve
    return svc


@pytest.fixture
def denier():
    svc = skippy_device.DeviceService()

    async def always_deny(payload):
        return {"status": "DENY", "reason": "not today"}

    svc._test_approver = always_deny
    return svc


def test_device_tools_are_re_only_not_coding():
    re_names = {t["function"]["name"] for t in tool_schemas.re_tools()}
    coding = {t["function"]["name"] for t in tool_schemas.workspace_tools()}
    for name in skippy_device.DEVICE_TOOLS:
        assert name in re_names, name
        assert name not in coding, name
    assert "apply_patch" not in re_names


def test_payload_round_trip_encodings():
    raw = b"\x01\x02hello"
    assert skippy_device._decode_payload(
        skippy_device._encode_payload(raw, "hex"), "hex"
    ) == raw
    assert skippy_device._decode_payload(
        skippy_device._encode_payload(raw, "base64"), "base64"
    ) == raw
    assert skippy_device._decode_payload("hi", "utf8") == b"hi"


@pytest.mark.asyncio
async def test_coding_mode_cannot_reach_device_tools(box):
    result = await skippy_dispatch.dispatch(
        "list_devices", {}, box, mode="coding", devices=None,
    )
    assert not result.ok
    assert "device service" in result.summary.lower() or "reverse-engineering" in result.summary.lower()


@pytest.mark.asyncio
async def test_list_devices_local_returns_ok_even_with_no_hardware(service, box):
    result = await skippy_dispatch.dispatch(
        "list_devices", {"host": "studio"}, box, mode="re", devices=service,
    )
    assert result.ok
    assert "device" in result.summary.lower()
    assert isinstance(result.data.get("devices"), list)


@pytest.mark.asyncio
async def test_serial_write_requires_approval(denier, box):
    # Plant a fake open session so we never touch real hardware.
    denier._serial["ser_test"] = skippy_device._SerialSession(
        handle="ser_test", port="/dev/cu.fake", baud=115200, host="studio",
        connection=object(),
    )

    async def boom(*args, **kwargs):
        raise AssertionError("serial exchange must not run when denied")

    # Patch the local exchange so a bug that skips approval is loud.
    original = skippy_device._serial_exchange_local
    skippy_device._serial_exchange_local = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not exchange")
    )
    try:
        result = await skippy_device.serial_io(
            denier, handle="ser_test", write="0102", read_bytes=0,
        )
    finally:
        skippy_device._serial_exchange_local = original

    assert not result.ok
    assert "denied" in result.summary.lower()


@pytest.mark.asyncio
async def test_serial_read_without_write_skips_approval(denier, box):
    """A denier that is never asked is the proof a pure read is ungated."""
    called = {"n": 0}

    async def track(payload):
        called["n"] += 1
        return {"status": "DENY", "reason": "should not be asked"}

    denier._test_approver = track
    denier._serial["ser_test"] = skippy_device._SerialSession(
        handle="ser_test", port="/dev/cu.fake", baud=115200, host="studio",
        connection=object(),
    )

    def fake_exchange(conn, payload, read_n, idle, capture):
        assert payload == b""
        return b"\x00\x01"

    original = skippy_device._serial_exchange_local
    skippy_device._serial_exchange_local = fake_exchange
    try:
        result = await skippy_device.serial_io(
            denier, handle="ser_test", read_bytes=2,
        )
    finally:
        skippy_device._serial_exchange_local = original

    assert result.ok
    assert called["n"] == 0
    assert result.content == "0001"


@pytest.mark.asyncio
async def test_remote_host_without_bridge_fails_clearly(service, box):
    result = await skippy_device.list_devices(service, host="macbook")
    assert not result.ok
    # Either "no hub" (unit test) or "no device bridge" (hub with no client).
    assert "hub" in result.summary.lower() or "bridge" in result.summary.lower()


@pytest.mark.asyncio
async def test_remote_host_names_the_missing_bridge_when_hub_is_present(box):
    class FakeHub:
        active_connections = {}

        async def execute_tool_on_client(self, *a, **k):
            raise AssertionError("must not RPC when bridge is offline")

    service = skippy_device.DeviceService(hub=FakeHub(), client_id="ui")
    result = await skippy_device.list_devices(service, host="macbook")
    assert not result.ok
    assert "device bridge" in result.summary.lower()


@pytest.mark.asyncio
async def test_net_scan_parses_ranges_and_caps(service, monkeypatch):
    seen = {}

    def fake_scan(address, ports, timeout):
        seen["ports"] = list(ports)
        seen["address"] = address
        return [80]

    monkeypatch.setattr(skippy_device, "_net_scan_local", fake_scan)
    result = await skippy_device.net_scan(
        service, address="127.0.0.1", ports="22,80,100-102",
    )
    assert result.ok
    assert seen["ports"] == [22, 80, 100, 101, 102]
    assert result.data["open"] == [80]


@pytest.mark.asyncio
async def test_net_scan_rejects_huge_range(service):
    result = await skippy_device.net_scan(
        service, address="127.0.0.1", ports="1-1000",
    )
    assert not result.ok
    assert "256" in result.summary


# --- named bridges (ADR 0020) ----------------------------------------------

def test_a_host_name_becomes_a_bridge_client_id():
    assert skippy_device.bridge_client_id("bench") == "devices:bench"
    assert skippy_device.bridge_client_id("BENCH") == "devices:bench"
    # Back-compat: the unnamed Mac bridge, and a caller that names the id itself.
    assert skippy_device.bridge_client_id("") == "devices"
    assert skippy_device.bridge_client_id("devices") == "devices"
    assert skippy_device.bridge_client_id("devices:bench") == "devices:bench"


def test_bridge_client_ids_are_recognised_for_the_rpc_only_rule():
    assert skippy_device.is_bridge_client_id("devices")
    assert skippy_device.is_bridge_client_id("devices:bench")
    assert not skippy_device.is_bridge_client_id("cursor")
    assert not skippy_device.is_bridge_client_id("swiftui")


@pytest.mark.asyncio
async def test_host_bench_routes_to_the_named_bridge():
    bridge = FakeBridge("devices:bench").answer("device_list", {"devices": []})
    result = await skippy_device.list_devices(bridged_service(bridge), host="bench")
    assert result.ok
    assert bridge.target("device_list") == "devices:bench"


@pytest.mark.asyncio
async def test_a_host_with_no_named_bridge_falls_back_to_the_mac_bridge():
    """host='macbook' predates named bridges and must keep reaching 'devices'."""
    bridge = FakeBridge("devices").answer("device_list", {"devices": []})
    result = await skippy_device.list_devices(bridged_service(bridge), host="macbook")
    assert result.ok
    assert bridge.target("device_list") == "devices"


@pytest.mark.asyncio
async def test_the_bench_node_is_not_used_for_another_host():
    """Two bridges connected: a host must not be served by the wrong one."""
    bridge = FakeBridge("devices", "devices:bench").answer("device_list", {"devices": []})
    service = bridged_service(bridge)
    await skippy_device.list_devices(service, host="bench")
    assert bridge.target("device_list") == "devices:bench"
    await skippy_device.list_devices(service, host="macbook")
    assert bridge.target("device_list") == "devices"


@pytest.mark.asyncio
async def test_an_offline_bench_node_names_both_ids_it_looked_for():
    bridge = FakeBridge("devices:other")
    result = await skippy_device.i2c_scan(bridged_service(bridge), host="bench")
    assert not result.ok
    assert "devices:bench" in result.summary
    assert "bench" in result.summary


@pytest.mark.asyncio
async def test_a_serial_session_on_the_bench_stays_on_the_bench():
    """The handle carries the host, so io and close follow the open."""
    bridge = (
        FakeBridge("devices", "devices:bench")
        .answer("device_serial_open", lambda req: {"handle": req["handle"]})
        .answer("device_serial_io", {"data_hex": "4f4b"})
        .answer("device_serial_close", {})
    )
    service = bridged_service(bridge)
    opened = await skippy_device.serial_open(service, port="port-c", host="bench")
    assert opened.ok
    handle = opened.data["handle"]

    read = await skippy_device.serial_io(service, handle=handle, read_bytes=2)
    assert read.ok
    assert read.content == "4f4b"
    assert bridge.target("device_serial_io") == "devices:bench"

    closed = await skippy_device.serial_close(service, handle=handle)
    assert closed.ok
    assert bridge.target("device_serial_close") == "devices:bench"


# --- pins and buses ---------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["studio", "local", ""])
@pytest.mark.parametrize("tool,kwargs", [
    ("i2c_scan", {}),
    ("i2c_io", {"addr": "0x3c", "read_len": 1}),
    ("gpio_io", {"pin": 26}),
    ("adc_read", {"pin": 36}),
])
async def test_pins_and_buses_refuse_a_local_host_and_name_the_node(host, tool, kwargs):
    """The Studio has no header. Saying so beats a driver error the model cannot read."""
    bridge = FakeBridge("devices:bench")
    result = await getattr(skippy_device, tool)(bridged_service(bridge), host=host, **kwargs)
    assert not result.ok
    assert "bridge" in result.summary.lower()
    assert "bench" in result.summary
    assert bridge.sent == [], "a local host must not reach the wire at all"


@pytest.mark.asyncio
async def test_i2c_scan_reports_what_answered_and_asks_no_one():
    bridge = FakeBridge("devices:bench").answer(
        "device_i2c_scan", {"addresses": ["0x3c", 0x68]},
    )
    result = await skippy_device.i2c_scan(bridged_service(bridge), host="bench", bus=0)
    assert result.ok
    assert result.data["addresses"] == ["0x3c", "0x68"]
    assert bridge.request("device_i2c_scan") == {"bus": 0}
    assert bridge.cards == [], "a scan is a read and must not raise a card"


@pytest.mark.asyncio
async def test_an_i2c_register_read_is_free_and_sends_the_pinned_shape():
    bridge = FakeBridge("devices:bench").answer("device_i2c_io", {"data_hex": "71"})
    result = await skippy_device.i2c_io(
        bridged_service(bridge), addr="0x68", host="bench", register=0x75, read_len=1,
    )
    assert result.ok
    assert result.content == "71"
    assert bridge.request("device_i2c_io") == {
        "bus": 0, "addr": "0x68", "register": 0x75, "write_hex": "", "read_len": 1,
    }
    assert bridge.cards == []


@pytest.mark.asyncio
async def test_an_i2c_write_waits_for_the_card_and_shows_it_on_the_run_client():
    bridge = FakeBridge("devices:bench").answer("device_i2c_io", {"data_hex": ""})
    result = await skippy_device.i2c_io(
        bridged_service(bridge), addr="0x68", host="bench", register=0x6b, write="01",
    )
    assert result.ok
    assert bridge.request("device_i2c_io")["write_hex"] == "01"
    # The card belongs to the machine the human is sitting at, never to the node.
    assert [client for client, _ in bridge.cards] == [RUN_CLIENT_ID]
    assert bridge.cards[0][1]["type"] == "device_auth"
    assert bridge.cards[0][1]["preview_hex"] == "01"


@pytest.mark.asyncio
async def test_a_denied_i2c_write_never_reaches_the_bus():
    bridge = FakeBridge("devices:bench", approve=False).answer("device_i2c_io", {})
    result = await skippy_device.i2c_io(
        bridged_service(bridge), addr="0x68", host="bench", write="ff",
    )
    assert not result.ok
    assert "denied" in result.summary.lower()
    assert bridge.sent == []


@pytest.mark.asyncio
async def test_i2c_io_rejects_an_address_that_is_not_seven_bit():
    bridge = FakeBridge("devices:bench")
    result = await skippy_device.i2c_io(
        bridged_service(bridge), addr="0x88", host="bench", read_len=1,
    )
    assert not result.ok
    assert "7-bit" in result.summary
    assert bridge.sent == []


@pytest.mark.asyncio
async def test_i2c_io_needs_something_to_do():
    bridge = FakeBridge("devices:bench")
    result = await skippy_device.i2c_io(bridged_service(bridge), addr="0x3c", host="bench")
    assert not result.ok
    assert "read_len" in result.summary


@pytest.mark.asyncio
async def test_reading_a_pin_is_free_and_driving_one_is_not():
    bridge = (
        FakeBridge("devices:bench")
        .answer("device_gpio", lambda req: {"pin": req["pin"], "value": req["value"] or 0})
    )
    service = bridged_service(bridge)

    read = await skippy_device.gpio_io(service, pin=26, host="bench", pull="up")
    assert read.ok
    assert read.data["value"] == 0
    assert bridge.request("device_gpio") == {
        "pin": 26, "direction": "read", "value": None, "pull": "up",
    }
    assert bridge.cards == []

    drive = await skippy_device.gpio_io(
        service, pin=26, host="bench", direction="write", value=1,
    )
    assert drive.ok
    assert bridge.request("device_gpio")["value"] == 1
    assert len(bridge.cards) == 1
    assert bridge.cards[0][1]["pin"] == 26


@pytest.mark.asyncio
async def test_a_denied_pin_drive_never_reaches_the_pin():
    bridge = FakeBridge("devices:bench", approve=False).answer("device_gpio", {"value": 1})
    result = await skippy_device.gpio_io(
        bridged_service(bridge), pin=26, host="bench", direction="write", value=1,
    )
    assert not result.ok
    assert bridge.sent == []


@pytest.mark.asyncio
async def test_driving_a_pin_needs_a_level():
    bridge = FakeBridge("devices:bench")
    result = await skippy_device.gpio_io(
        bridged_service(bridge), pin=26, host="bench", direction="write",
    )
    assert not result.ok
    assert "value" in result.summary
    assert bridge.sent == []


@pytest.mark.asyncio
async def test_a_pin_drive_survives_the_dispatcher(box):
    """`mode` is stripped on the way through dispatch, so the tool says
    `direction`. Called the old way, a drive would silently become a read."""
    bridge = FakeBridge("devices:bench").answer("device_gpio", {"pin": 26, "value": 1})
    result = await skippy_dispatch.dispatch(
        "gpio_io", {"pin": 26, "host": "bench", "direction": "write", "value": 1},
        box, mode="re", devices=bridged_service(bridge),
    )
    assert result.ok
    assert bridge.request("device_gpio")["direction"] == "write"


@pytest.mark.asyncio
async def test_adc_read_returns_millivolts_and_the_raw_count():
    bridge = FakeBridge("devices:bench").answer(
        "device_adc", {"pin": 36, "raw": 2048, "mv": 1650},
    )
    result = await skippy_device.adc_read(
        bridged_service(bridge), pin=36, host="bench", samples=4,
    )
    assert result.ok
    assert result.data["mv"] == 1650
    assert result.data["raw"] == 2048
    assert bridge.request("device_adc") == {"pin": 36, "samples": 4}
    assert bridge.cards == []


@pytest.mark.asyncio
async def test_a_node_refusing_an_action_reports_its_reason():
    """USB on the Core2, or a pin it will not touch: the model needs the reason."""
    bridge = FakeBridge("devices:bench").fail(
        "device_adc", "Only G36 (Grove Port B in) is an analog input on this node.",
    )
    result = await skippy_device.adc_read(bridged_service(bridge), pin=26, host="bench")
    assert not result.ok
    assert "G36" in result.summary


@pytest.mark.asyncio
async def test_the_new_tools_are_dispatchable_in_re_mode_only(box):
    bridge = FakeBridge("devices:bench").answer("device_i2c_scan", {"addresses": []})
    ok = await skippy_dispatch.dispatch(
        "i2c_scan", {"host": "bench"}, box, mode="re", devices=bridged_service(bridge),
    )
    assert ok.ok
    blind = await skippy_dispatch.dispatch(
        "gpio_io", {"pin": 26, "host": "bench"}, box, mode="coding", devices=None,
    )
    assert not blind.ok
    assert "device service" in blind.summary.lower()


@pytest.mark.asyncio
async def test_approved_serial_write_reaches_the_device(service, monkeypatch):
    service._serial["ser_test"] = skippy_device._SerialSession(
        handle="ser_test", port="/dev/cu.fake", baud=9600, host="studio",
        connection=object(),
    )
    seen = {}

    def fake_exchange(conn, payload, read_n, idle, capture):
        seen["payload"] = payload
        return b"OK"

    monkeypatch.setattr(skippy_device, "_serial_exchange_local", fake_exchange)
    result = await skippy_device.serial_io(
        service, handle="ser_test", write="4142", write_encoding="hex",
        read_bytes=2, encoding="utf8",
    )
    assert result.ok
    assert seen["payload"] == b"AB"
    assert result.content == "OK"
