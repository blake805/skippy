"""Live device tools for RE mode: reads free, writes gated, coding mode blind."""

import pytest

import skippy_device
import skippy_dispatch
import tool_schemas
from skippy_sandbox import Sandbox


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
