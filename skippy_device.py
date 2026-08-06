"""Live device I/O for reverse-engineering mode.

RE mode used to be static inspection only: `file`, `otool`, `strings`. That is
the right posture for someone else's binary. Probing a part that is sitting on
the bench — serial, USB, a network socket — is a different activity, and it gets
its own tools rather than a loosened `run_command` allowlist. ADR 0015 records
the boundary.

Three decisions worth stating up front.

**Reads are free; writes ask.** Enumeration, serial reads, USB IN transfers and
network receives go through without a human in the loop. Anything that sends
bytes to hardware waits on an approve/deny card first. Bricking a part is the
failure mode these tools exist to prevent.

**Devices are not sandboxed by path.** `/dev/cu.*` lives outside every workspace
root, so `Sandbox.resolve` would refuse it. The boundary here is enumeration
plus an explicit session handle — you can only talk to something `list_devices`
has already named, and only through a handle `serial_open` (etc.) issued.

**The host can be local or remote.** A part plugged into the Mac Studio runs
through pyserial/pyusb/sockets in-process. A part plugged into the MacBook
routes through the same client-RPC channel the Cursor extension already uses
(`hub.execute_tool_on_client`). The tool surface the model sees is identical.
The host name picks the bridge — `host="bench"` looks for the client id
`devices:bench`, the Core2 node wired to the bench — and falls back to the bare
`devices` id a lone Mac bridge connects with.

**Pins and buses only exist on a bridge.** `i2c_scan`, `i2c_io`, `gpio_io` and
`adc_read` have no local backend: the Studio has no GPIO header, so they refuse
a local host rather than pretending. ADR 0020 pins their wire shapes.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import socket
import struct
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set

from skippy_sandbox import ToolResult

logger = logging.getLogger("skippy_device")

# Bounded exchanges only. Read-until-idle with no cap is how a stuck device
# burns a whole agent step; a capture longer than this belongs in a dedicated
# analyzer, not a tool call.
MAX_IO_BYTES = 16_384
MAX_CAPTURE_SECONDS = 30.0
DEFAULT_READ_TIMEOUT = 2.0
DEFAULT_WRITE_APPROVAL_TIMEOUT = 600.0

# Client id a Mac app uses when it offers its local hardware over the factory
# websocket. Distinct from "cursor" so both can be connected at once.
DEVICE_BRIDGE_CLIENT_ID = "devices"

LOCAL_HOSTS = frozenset({"studio", "local", "hub", ""})


def bridge_client_id(host: str) -> str:
    """The named-bridge client id for a host: 'bench' -> 'devices:bench'.

    One hub can have several bridges on it at once — a Mac sharing its USB
    ports and the Core2 on the bench — so the bridge a host names has to be
    part of the client id rather than a single well-known string.
    """
    name = str(host or "").strip().lower()
    if not name or name == DEVICE_BRIDGE_CLIENT_ID:
        return DEVICE_BRIDGE_CLIENT_ID
    if name.startswith(DEVICE_BRIDGE_CLIENT_ID + ":"):
        return name
    return f"{DEVICE_BRIDGE_CLIENT_ID}:{name}"


def is_bridge_client_id(client_id: str) -> bool:
    """True for the ids a device bridge connects with.

    Used by the factory endpoint to keep bridges RPC-only: a bridge answers
    tool calls, and must never be able to start an agent run.
    """
    name = str(client_id or "").strip().lower()
    return name == DEVICE_BRIDGE_CLIENT_ID or name.startswith(DEVICE_BRIDGE_CLIENT_ID + ":")


@dataclass
class _SerialSession:
    handle: str
    port: str
    baud: int
    host: str
    connection: Any = None  # serial.Serial when local


@dataclass
class _NetSession:
    handle: str
    host: str
    address: str
    port: int
    proto: str
    sock: Any = None


@dataclass
class DeviceService:
    """Per-run device state: open sessions, the hub for remote RPC, the approver."""

    hub: Any = None
    client_id: str = ""
    bridge_client_id: str = DEVICE_BRIDGE_CLIENT_ID
    approval_timeout: float = DEFAULT_WRITE_APPROVAL_TIMEOUT
    _serial: Dict[str, _SerialSession] = field(default_factory=dict)
    _net: Dict[str, _NetSession] = field(default_factory=dict)

    # -- approval ---------------------------------------------------------

    async def approve_write(self, explanation: str, details: dict) -> ToolResult | None:
        """Ask the human. Returns a failed ToolResult on deny, None on approve."""
        payload = {
            "type": "device_auth",
            "explanation": explanation,
            **details,
        }
        reply = await self._request_auth(payload)
        if reply.get("status") == "APPROVE":
            return None
        reason = reply.get("reason") or "the human declined"
        return ToolResult(False, f"Write denied: {reason}.")

    async def _request_auth(self, payload: dict) -> dict:
        if self.hub is None or not self.client_id:
            # Tests and headless runs: fail closed unless an override is installed.
            override = getattr(self, "_test_approver", None)
            if override is not None:
                return await override(payload)
            return {"status": "DENY", "reason": "no client available to approve the write"}
        socket_ = getattr(self.hub, "active_connections", {}).get(self.client_id)
        if socket_ is None:
            return {"status": "DENY", "reason": "the client that started this run is offline"}
        return await self.hub.request_authorization(
            socket_, payload, timeout=self.approval_timeout
        )

    # -- routing ----------------------------------------------------------

    def _is_local(self, host: str) -> bool:
        return str(host or "studio").strip().lower() in LOCAL_HOSTS

    def _bridge_for(self, host: str) -> str:
        """Which connected bridge serves this host.

        A named bridge wins when it is there: the Core2 on the bench registers
        as `devices:bench`, so `host="bench"` reaches it and nothing else. The
        fall-back to the bare `devices` id is what keeps `host="macbook"`
        working against a Mac bridge that predates named bridges.
        """
        named = bridge_client_id(host)
        if named == self.bridge_client_id:
            return self.bridge_client_id
        if named in (getattr(self.hub, "active_connections", None) or {}):
            return named
        return self.bridge_client_id

    async def _remote(
        self, action: str, payload: dict, timeout: float = 30.0, host: str = "",
    ) -> dict:
        if self.hub is None:
            return {"ok": False, "error": "No hub available for remote device access."}
        target = self._bridge_for(host)
        if target not in getattr(self.hub, "active_connections", {}):
            named = bridge_client_id(host)
            expected = (
                f"'{named}' or '{self.bridge_client_id}'"
                if named != target else f"'{target}'"
            )
            return {
                "ok": False,
                "error": (
                    f"No device bridge is connected for host '{host or 'studio'}' "
                    f"(expected client_id={expected}). Power up the bench node, or "
                    "open SkippyMac on the machine that has the hardware and enable "
                    "device sharing."
                ),
            }
        request = dict(payload)
        request["action"] = action
        response = await self.hub.execute_tool_on_client(
            target, request, timeout=timeout
        )
        if not isinstance(response, dict):
            return {"ok": False, "error": f"Malformed bridge reply: {response!r}"}
        if response.get("error") and response.get("ok") is not True:
            return {"ok": False, "error": str(response["error"])}
        if response.get("ok") is False:
            return {"ok": False, "error": str(response.get("error") or "bridge reported failure")}
        result = response.get("result")
        if result is None:
            result = {
                k: v for k, v in response.items()
                if k not in ("task_id", "ok", "action", "error")
            }
        return {"ok": True, "result": result}


# ---------------------------------------------------------------------------
# Encoding helpers — binary on the wire, hex in the transcript
# ---------------------------------------------------------------------------

def _decode_payload(data: Optional[str], encoding: str = "hex") -> bytes:
    if not data:
        return b""
    encoding = (encoding or "hex").lower()
    if encoding == "hex":
        cleaned = "".join(str(data).split())
        if len(cleaned) % 2:
            raise ValueError("hex payload has an odd number of digits")
        return binascii.unhexlify(cleaned)
    if encoding == "base64":
        return base64.b64decode(str(data), validate=False)
    if encoding == "utf8":
        return str(data).encode("utf-8")
    raise ValueError(f"Unknown encoding {encoding!r}; use hex, base64, or utf8.")


def _encode_payload(data: bytes, encoding: str = "hex") -> str:
    encoding = (encoding or "hex").lower()
    if encoding == "hex":
        return binascii.hexlify(data).decode("ascii")
    if encoding == "base64":
        return base64.b64encode(data).decode("ascii")
    if encoding == "utf8":
        return data.decode("utf-8", errors="replace")
    raise ValueError(f"Unknown encoding {encoding!r}.")


def _clip(data: bytes, limit: int = MAX_IO_BYTES) -> tuple[bytes, bool]:
    if len(data) <= limit:
        return data, False
    return data[:limit], True


# ---------------------------------------------------------------------------
# Local backends
# ---------------------------------------------------------------------------

def _list_serial_local() -> List[dict]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    out = []
    for info in list_ports.comports():
        out.append({
            "kind": "serial",
            "host": "studio",
            "port": info.device,
            "description": info.description or "",
            "manufacturer": info.manufacturer or "",
            "vid": f"{info.vid:04x}" if info.vid is not None else "",
            "pid": f"{info.pid:04x}" if info.pid is not None else "",
            "serial_number": info.serial_number or "",
        })
    return out


def _list_usb_local() -> List[dict]:
    try:
        import usb.core
        import usb.util
    except ImportError:
        return []
    out = []
    try:
        devices = list(usb.core.find(find_all=True) or [])
    except Exception as exc:
        logger.info("USB enumeration failed: %s", exc)
        return []
    for dev in devices:
        try:
            manufacturer = usb.util.get_string(dev, dev.iManufacturer) if dev.iManufacturer else ""
        except Exception:
            manufacturer = ""
        try:
            product = usb.util.get_string(dev, dev.iProduct) if dev.iProduct else ""
        except Exception:
            product = ""
        out.append({
            "kind": "usb",
            "host": "studio",
            "vid": f"{dev.idVendor:04x}",
            "pid": f"{dev.idProduct:04x}",
            "bus": getattr(dev, "bus", None),
            "address": getattr(dev, "address", None),
            "manufacturer": manufacturer or "",
            "product": product or "",
        })
    return out


def _open_serial_local(port: str, baud: int, timeout: float, bytesize: int,
                       parity: str, stopbits: float):
    import serial

    parity_map = {
        "N": serial.PARITY_NONE, "E": serial.PARITY_EVEN,
        "O": serial.PARITY_ODD, "M": serial.PARITY_MARK, "S": serial.PARITY_SPACE,
    }
    stop_map = {
        1: serial.STOPBITS_ONE, 1.5: serial.STOPBITS_ONE_POINT_FIVE,
        2: serial.STOPBITS_TWO,
    }
    return serial.Serial(
        port=port,
        baudrate=baud,
        bytesize=bytesize,
        parity=parity_map.get(parity.upper(), serial.PARITY_NONE),
        stopbits=stop_map.get(stopbits, serial.STOPBITS_ONE),
        timeout=timeout,
        write_timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Tool handlers — first argument is always the DeviceService
# ---------------------------------------------------------------------------

async def list_devices(
    service: DeviceService,
    host: str = "studio",
    kinds: Optional[Sequence[str]] = None,
) -> ToolResult:
    """Enumerate serial ports, USB devices, or both on the named host."""
    wanted: Set[str] = set()
    for kind in kinds or ("serial", "usb"):
        wanted.add(str(kind).strip().lower())
    if not wanted:
        wanted = {"serial", "usb"}

    if service._is_local(host):
        devices: List[dict] = []
        if "serial" in wanted:
            devices.extend(await asyncio.to_thread(_list_serial_local))
        if "usb" in wanted:
            devices.extend(await asyncio.to_thread(_list_usb_local))
    else:
        remote = await service._remote(
            "device_list", {"host": host, "kinds": sorted(wanted)}, timeout=20.0,
            host=host,
        )
        if not remote["ok"]:
            return ToolResult(False, remote["error"])
        devices = list(remote["result"].get("devices") or [])

    lines = []
    for d in devices:
        if d.get("kind") == "serial":
            lines.append(
                f"serial  {d.get('port')}  {d.get('description', '')}  "
                f"vid={d.get('vid')} pid={d.get('pid')}"
            )
        else:
            lines.append(
                f"usb     {d.get('vid')}:{d.get('pid')}  "
                f"{d.get('manufacturer', '')} {d.get('product', '')}".rstrip()
            )
    summary = f"{len(devices)} device(s) on {host or 'studio'}"
    return ToolResult(True, summary, "\n".join(lines), {"devices": devices, "host": host or "studio"})


async def serial_open(
    service: DeviceService,
    port: str,
    baud: int = 115200,
    host: str = "studio",
    timeout: float = DEFAULT_READ_TIMEOUT,
    bytesize: int = 8,
    parity: str = "N",
    stopbits: float = 1,
) -> ToolResult:
    if not port or not str(port).strip():
        return ToolResult(False, "serial_open needs a port (from list_devices).")
    baud = int(baud)
    timeout = max(0.05, min(float(timeout), MAX_CAPTURE_SECONDS))
    handle = f"ser_{uuid.uuid4().hex[:10]}"

    if service._is_local(host):
        try:
            conn = await asyncio.to_thread(
                _open_serial_local, str(port), baud, timeout, int(bytesize),
                str(parity), float(stopbits),
            )
        except Exception as exc:
            return ToolResult(False, f"Could not open {port}: {exc}")
        service._serial[handle] = _SerialSession(
            handle=handle, port=str(port), baud=baud, host="studio", connection=conn,
        )
    else:
        remote = await service._remote(
            "device_serial_open",
            {
                "port": str(port), "baud": baud, "timeout": timeout,
                "bytesize": int(bytesize), "parity": str(parity),
                "stopbits": float(stopbits), "handle": handle,
            },
            timeout=15.0,
            host=host,
        )
        if not remote["ok"]:
            return ToolResult(False, remote["error"])
        handle = str(remote["result"].get("handle") or handle)
        service._serial[handle] = _SerialSession(
            handle=handle, port=str(port), baud=baud, host=str(host), connection=None,
        )

    return ToolResult(
        True,
        f"Opened {port} at {baud} baud (handle {handle}).",
        data={"handle": handle, "port": str(port), "baud": baud, "host": host or "studio"},
    )


async def serial_io(
    service: DeviceService,
    handle: str,
    write: Optional[str] = None,
    write_encoding: str = "hex",
    read_bytes: int = 0,
    read_until_idle: float = 0.0,
    capture_seconds: float = 0.0,
    encoding: str = "hex",
) -> ToolResult:
    """Bounded write and/or read on an open serial handle.

    A non-empty `write` is a device write and requires human approval. Pure
    reads (read_bytes / read_until_idle / capture_seconds with no write) do not.
    """
    session = service._serial.get(handle)
    if session is None:
        return ToolResult(False, f"Unknown serial handle '{handle}'. Call serial_open first.")

    try:
        payload = _decode_payload(write, write_encoding) if write else b""
    except (ValueError, binascii.Error) as exc:
        return ToolResult(False, f"Bad write payload: {exc}")

    if len(payload) > MAX_IO_BYTES:
        return ToolResult(False, f"Write payload exceeds {MAX_IO_BYTES} bytes; split it.")

    if payload:
        denied = await service.approve_write(
            f"Write {len(payload)} byte(s) to serial {session.port} @ {session.baud}",
            {
                "device": session.port,
                "host": session.host,
                "baud": session.baud,
                "bytes": len(payload),
                "preview_hex": _encode_payload(payload[:64], "hex"),
            },
        )
        if denied is not None:
            return denied

    read_n = max(0, min(int(read_bytes or 0), MAX_IO_BYTES))
    idle = max(0.0, min(float(read_until_idle or 0.0), MAX_CAPTURE_SECONDS))
    capture = max(0.0, min(float(capture_seconds or 0.0), MAX_CAPTURE_SECONDS))

    if service._is_local(session.host):
        received = await asyncio.to_thread(
            _serial_exchange_local, session.connection, payload, read_n, idle, capture,
        )
    else:
        remote = await service._remote(
            "device_serial_io",
            {
                "handle": handle,
                "write_hex": _encode_payload(payload, "hex") if payload else "",
                "read_bytes": read_n,
                "read_until_idle": idle,
                "capture_seconds": capture,
            },
            timeout=max(10.0, capture + idle + 5.0),
            host=session.host,
        )
        if not remote["ok"]:
            return ToolResult(False, remote["error"])
        try:
            received = _decode_payload(remote["result"].get("data_hex") or "", "hex")
        except (ValueError, binascii.Error) as exc:
            return ToolResult(False, f"Bridge returned bad data: {exc}")

    received, clipped = _clip(received)
    body = _encode_payload(received, encoding)
    summary = f"{'Wrote ' + str(len(payload)) + 'B, ' if payload else ''}read {len(received)}B"
    if clipped:
        summary += f" (truncated at {MAX_IO_BYTES})"
    return ToolResult(
        True, summary, body,
        {"handle": handle, "bytes": len(received), "encoding": encoding, "truncated": clipped},
    )


def _serial_exchange_local(conn, payload: bytes, read_n: int, idle: float, capture: float) -> bytes:
    if payload:
        conn.write(payload)
        conn.flush()
    chunks: List[bytes] = []
    if capture > 0:
        deadline = time.monotonic() + capture
        while time.monotonic() < deadline:
            waiting = min(0.1, max(0.0, deadline - time.monotonic()))
            conn.timeout = waiting
            piece = conn.read(1024)
            if piece:
                chunks.append(piece)
                if sum(len(c) for c in chunks) >= MAX_IO_BYTES:
                    break
    elif read_n > 0:
        conn.timeout = max(conn.timeout or DEFAULT_READ_TIMEOUT, DEFAULT_READ_TIMEOUT)
        chunks.append(conn.read(read_n))
    elif idle > 0:
        deadline = time.monotonic() + idle
        conn.timeout = 0.05
        while time.monotonic() < deadline:
            piece = conn.read(1024)
            if piece:
                chunks.append(piece)
                deadline = time.monotonic() + idle  # idle resets on activity
                if sum(len(c) for c in chunks) >= MAX_IO_BYTES:
                    break
            elif chunks:
                break
    return b"".join(chunks)


async def serial_close(service: DeviceService, handle: str) -> ToolResult:
    session = service._serial.pop(handle, None)
    if session is None:
        return ToolResult(False, f"Unknown serial handle '{handle}'.")
    if service._is_local(session.host) and session.connection is not None:
        try:
            await asyncio.to_thread(session.connection.close)
        except Exception as exc:
            return ToolResult(False, f"Closed with error: {exc}", data={"handle": handle})
    elif not service._is_local(session.host):
        await service._remote(
            "device_serial_close", {"handle": handle}, timeout=10.0, host=session.host,
        )
    return ToolResult(True, f"Closed {session.port}.", data={"handle": handle})


async def usb_transfer(
    service: DeviceService,
    vid: str,
    pid: str,
    direction: str = "in",
    endpoint: int = 0x81,
    data: Optional[str] = None,
    data_encoding: str = "hex",
    length: int = 64,
    host: str = "studio",
    timeout_ms: int = 2000,
) -> ToolResult:
    """Bulk/interrupt transfer. OUT requires approval; IN does not."""
    direction = str(direction or "in").lower()
    if direction not in ("in", "out"):
        return ToolResult(False, "direction must be 'in' or 'out'.")
    try:
        vid_i = int(str(vid), 0)
        pid_i = int(str(pid), 0)
    except ValueError:
        return ToolResult(False, "vid and pid must be integers (hex like 0x1234 is fine).")

    payload = b""
    if direction == "out":
        try:
            payload = _decode_payload(data, data_encoding)
        except (ValueError, binascii.Error) as exc:
            return ToolResult(False, f"Bad OUT payload: {exc}")
        if not payload:
            return ToolResult(False, "usb_transfer OUT needs a data payload.")
        denied = await service.approve_write(
            f"USB OUT {len(payload)}B to {vid}:{pid} ep=0x{int(endpoint):02x}",
            {
                "device": f"{vid}:{pid}",
                "host": host or "studio",
                "endpoint": int(endpoint),
                "bytes": len(payload),
                "preview_hex": _encode_payload(payload[:64], "hex"),
            },
        )
        if denied is not None:
            return denied

    length = max(1, min(int(length or 64), MAX_IO_BYTES))
    timeout_ms = max(50, min(int(timeout_ms or 2000), 60_000))

    if service._is_local(host):
        try:
            received = await asyncio.to_thread(
                _usb_transfer_local, vid_i, pid_i, direction, int(endpoint),
                payload, length, timeout_ms,
            )
        except Exception as exc:
            return ToolResult(False, f"USB transfer failed: {exc}")
    else:
        remote = await service._remote(
            "device_usb_transfer",
            {
                "vid": vid, "pid": pid, "direction": direction,
                "endpoint": int(endpoint),
                "data_hex": _encode_payload(payload, "hex") if payload else "",
                "length": length, "timeout_ms": timeout_ms,
            },
            timeout=timeout_ms / 1000.0 + 5.0,
            host=host,
        )
        if not remote["ok"]:
            return ToolResult(False, remote["error"])
        try:
            received = _decode_payload(remote["result"].get("data_hex") or "", "hex")
        except (ValueError, binascii.Error) as exc:
            return ToolResult(False, f"Bridge returned bad data: {exc}")

    received, clipped = _clip(received)
    body = _encode_payload(received, "hex")
    summary = f"USB {direction} {len(received)}B on {vid}:{pid}"
    if clipped:
        summary += f" (truncated at {MAX_IO_BYTES})"
    return ToolResult(
        True, summary, body,
        {"vid": vid, "pid": pid, "bytes": len(received), "truncated": clipped},
    )


def _usb_transfer_local(vid, pid, direction, endpoint, payload, length, timeout_ms) -> bytes:
    import usb.core
    import usb.util

    dev = usb.core.find(idVendor=vid, idProduct=pid)
    if dev is None:
        raise RuntimeError(f"No USB device {vid:04x}:{pid:04x} found.")
    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
    except (NotImplementedError, ValueError):
        pass
    try:
        dev.set_configuration()
    except usb.core.USBError:
        pass
    if direction == "out":
        written = dev.write(endpoint, payload, timeout=timeout_ms)
        return struct.pack(">I", int(written))
    return bytes(dev.read(endpoint, length, timeout=timeout_ms))


async def usb_control(
    service: DeviceService,
    vid: str,
    pid: str,
    request_type: int,
    request: int,
    value: int = 0,
    index: int = 0,
    data: Optional[str] = None,
    data_encoding: str = "hex",
    length: int = 0,
    host: str = "studio",
    timeout_ms: int = 2000,
) -> ToolResult:
    """USB control transfer. Host-to-device (OUT bit clear in bmRequestType's
    direction sense for data stage with payload) requires approval when sending
    a data stage; device-to-host reads do not."""
    try:
        vid_i = int(str(vid), 0)
        pid_i = int(str(pid), 0)
        bm = int(request_type)
        breq = int(request)
        wvalue = int(value)
        windex = int(index)
    except ValueError:
        return ToolResult(False, "vid/pid/request_type/request/value/index must be integers.")

    payload = b""
    if data:
        try:
            payload = _decode_payload(data, data_encoding)
        except (ValueError, binascii.Error) as exc:
            return ToolResult(False, f"Bad control payload: {exc}")

    # Direction bit 7 of bmRequestType: 1 = device-to-host (IN).
    is_in = bool(bm & 0x80)
    if payload and not is_in:
        denied = await service.approve_write(
            f"USB control OUT to {vid}:{pid} req=0x{breq:02x}",
            {
                "device": f"{vid}:{pid}",
                "host": host or "studio",
                "request_type": bm,
                "request": breq,
                "value": wvalue,
                "index": windex,
                "bytes": len(payload),
                "preview_hex": _encode_payload(payload[:64], "hex"),
            },
        )
        if denied is not None:
            return denied

    length = max(0, min(int(length or len(payload) or 0), MAX_IO_BYTES))
    timeout_ms = max(50, min(int(timeout_ms or 2000), 60_000))

    if service._is_local(host):
        try:
            received = await asyncio.to_thread(
                _usb_control_local, vid_i, pid_i, bm, breq, wvalue, windex,
                payload, length, timeout_ms, is_in,
            )
        except Exception as exc:
            return ToolResult(False, f"USB control failed: {exc}")
    else:
        remote = await service._remote(
            "device_usb_control",
            {
                "vid": vid, "pid": pid, "request_type": bm, "request": breq,
                "value": wvalue, "index": windex,
                "data_hex": _encode_payload(payload, "hex") if payload else "",
                "length": length, "timeout_ms": timeout_ms,
            },
            timeout=timeout_ms / 1000.0 + 5.0,
            host=host,
        )
        if not remote["ok"]:
            return ToolResult(False, remote["error"])
        try:
            received = _decode_payload(remote["result"].get("data_hex") or "", "hex")
        except (ValueError, binascii.Error) as exc:
            return ToolResult(False, f"Bridge returned bad data: {exc}")

    body = _encode_payload(received, "hex")
    return ToolResult(
        True, f"USB control {'IN' if is_in else 'OUT'} {len(received)}B on {vid}:{pid}",
        body, {"vid": vid, "pid": pid, "bytes": len(received)},
    )


def _usb_control_local(vid, pid, bm, breq, wvalue, windex, payload, length, timeout_ms, is_in) -> bytes:
    import usb.core

    dev = usb.core.find(idVendor=vid, idProduct=pid)
    if dev is None:
        raise RuntimeError(f"No USB device {vid:04x}:{pid:04x} found.")
    if is_in:
        return bytes(dev.ctrl_transfer(bm, breq, wvalue, windex, length or 64, timeout=timeout_ms))
    written = dev.ctrl_transfer(bm, breq, wvalue, windex, payload or b"", timeout=timeout_ms)
    return struct.pack(">I", int(written))


async def net_connect(
    service: DeviceService,
    address: str,
    port: int,
    proto: str = "tcp",
    host: str = "studio",
    timeout: float = 5.0,
) -> ToolResult:
    proto = str(proto or "tcp").lower()
    if proto not in ("tcp", "udp"):
        return ToolResult(False, "proto must be 'tcp' or 'udp'.")
    if not address or not str(address).strip():
        return ToolResult(False, "net_connect needs an address.")
    port = int(port)
    if not (1 <= port <= 65535):
        return ToolResult(False, "port must be 1-65535.")
    timeout = max(0.1, min(float(timeout), 60.0))
    handle = f"net_{uuid.uuid4().hex[:10]}"

    if service._is_local(host):
        try:
            sock = await asyncio.to_thread(_net_open_local, str(address), port, proto, timeout)
        except Exception as exc:
            return ToolResult(False, f"Could not connect to {address}:{port}: {exc}")
        service._net[handle] = _NetSession(
            handle=handle, host="studio", address=str(address), port=port,
            proto=proto, sock=sock,
        )
    else:
        remote = await service._remote(
            "device_net_connect",
            {"address": str(address), "port": port, "proto": proto,
             "timeout": timeout, "handle": handle},
            timeout=timeout + 5.0,
            host=host,
        )
        if not remote["ok"]:
            return ToolResult(False, remote["error"])
        handle = str(remote["result"].get("handle") or handle)
        service._net[handle] = _NetSession(
            handle=handle, host=str(host), address=str(address), port=port,
            proto=proto, sock=None,
        )

    return ToolResult(
        True, f"Connected {proto} {address}:{port} (handle {handle}).",
        data={"handle": handle, "address": str(address), "port": port, "proto": proto},
    )


def _net_open_local(address: str, port: int, proto: str, timeout: float):
    kind = socket.SOCK_STREAM if proto == "tcp" else socket.SOCK_DGRAM
    sock = socket.socket(socket.AF_INET, kind)
    sock.settimeout(timeout)
    if proto == "tcp":
        sock.connect((address, port))
    else:
        sock.connect((address, port))  # default destination for send/recv
    return sock


async def net_io(
    service: DeviceService,
    handle: str,
    write: Optional[str] = None,
    write_encoding: str = "hex",
    read_bytes: int = 0,
    encoding: str = "hex",
) -> ToolResult:
    session = service._net.get(handle)
    if session is None:
        return ToolResult(False, f"Unknown net handle '{handle}'. Call net_connect first.")
    try:
        payload = _decode_payload(write, write_encoding) if write else b""
    except (ValueError, binascii.Error) as exc:
        return ToolResult(False, f"Bad write payload: {exc}")
    if len(payload) > MAX_IO_BYTES:
        return ToolResult(False, f"Write payload exceeds {MAX_IO_BYTES} bytes.")

    if payload:
        denied = await service.approve_write(
            f"Send {len(payload)}B {session.proto} to {session.address}:{session.port}",
            {
                "device": f"{session.address}:{session.port}",
                "host": session.host,
                "proto": session.proto,
                "bytes": len(payload),
                "preview_hex": _encode_payload(payload[:64], "hex"),
            },
        )
        if denied is not None:
            return denied

    read_n = max(0, min(int(read_bytes or 0), MAX_IO_BYTES))
    if service._is_local(session.host):
        try:
            received = await asyncio.to_thread(
                _net_exchange_local, session.sock, payload, read_n,
            )
        except Exception as exc:
            return ToolResult(False, f"Network I/O failed: {exc}")
    else:
        remote = await service._remote(
            "device_net_io",
            {
                "handle": handle,
                "write_hex": _encode_payload(payload, "hex") if payload else "",
                "read_bytes": read_n,
            },
            timeout=30.0,
            host=session.host,
        )
        if not remote["ok"]:
            return ToolResult(False, remote["error"])
        try:
            received = _decode_payload(remote["result"].get("data_hex") or "", "hex")
        except (ValueError, binascii.Error) as exc:
            return ToolResult(False, f"Bridge returned bad data: {exc}")

    received, clipped = _clip(received)
    body = _encode_payload(received, encoding)
    summary = f"{'Sent ' + str(len(payload)) + 'B, ' if payload else ''}recv {len(received)}B"
    return ToolResult(
        True, summary, body,
        {"handle": handle, "bytes": len(received), "encoding": encoding, "truncated": clipped},
    )


def _net_exchange_local(sock, payload: bytes, read_n: int) -> bytes:
    if payload:
        sock.sendall(payload)
    if read_n <= 0:
        return b""
    chunks: List[bytes] = []
    remaining = read_n
    while remaining > 0:
        try:
            piece = sock.recv(min(4096, remaining))
        except socket.timeout:
            break
        if not piece:
            break
        chunks.append(piece)
        remaining -= len(piece)
    return b"".join(chunks)


async def net_scan(
    service: DeviceService,
    address: str,
    ports: str = "22,80,443,8080",
    host: str = "studio",
    timeout: float = 0.4,
) -> ToolResult:
    """TCP connect-scan a short list of ports. Read-only; no approval needed."""
    if not address:
        return ToolResult(False, "net_scan needs an address.")
    parsed: List[int] = []
    for part in str(ports).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                return ToolResult(False, f"Bad port range {part!r}.")
            if hi < lo or hi - lo > 256:
                return ToolResult(False, "Port ranges are capped at 256 ports.")
            parsed.extend(range(lo, hi + 1))
        else:
            try:
                parsed.append(int(part))
            except ValueError:
                return ToolResult(False, f"Bad port {part!r}.")
    if not parsed:
        return ToolResult(False, "No ports to scan.")
    if len(parsed) > 256:
        return ToolResult(False, "Scan is capped at 256 ports per call.")
    timeout = max(0.05, min(float(timeout), 5.0))

    if service._is_local(host):
        open_ports = await asyncio.to_thread(_net_scan_local, str(address), parsed, timeout)
    else:
        remote = await service._remote(
            "device_net_scan",
            {"address": str(address), "ports": parsed, "timeout": timeout},
            timeout=max(10.0, len(parsed) * timeout + 5.0),
            host=host,
        )
        if not remote["ok"]:
            return ToolResult(False, remote["error"])
        open_ports = list(remote["result"].get("open") or [])

    lines = [f"{address}:{p} open" for p in open_ports] or [f"No open ports among {len(parsed)} probed on {address}"]
    return ToolResult(
        True, f"{len(open_ports)} open port(s) on {address}",
        "\n".join(lines), {"address": address, "open": open_ports, "probed": len(parsed)},
    )


def _net_scan_local(address: str, ports: Sequence[int], timeout: float) -> List[int]:
    open_ports: List[int] = []
    for port in ports:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            if sock.connect_ex((address, int(port))) == 0:
                open_ports.append(int(port))
        except OSError:
            pass
        finally:
            sock.close()
    return open_ports


# ---------------------------------------------------------------------------
# Pins and buses — bridge only
# ---------------------------------------------------------------------------
#
# Serial, USB and sockets all exist on the hub, so those tools have a local
# backend. An I2C bus, a GPIO pin and an ADC channel do not: a Mac Studio has no
# header to put them on. Rather than pretend — pyserial cannot help here, and a
# vague failure deep in a driver teaches the model nothing — these four refuse a
# local host outright and name the node that can do the job.

# One I2C transaction, not a transfer session. A page is the natural unit on
# every part that has one, and a caller wanting a whole EEPROM reads it a page
# at a time so each call stays a bounded exchange.
MAX_I2C_BYTES = 256


def _bridge_only(service: DeviceService, host: str, tool: str) -> Optional[ToolResult]:
    if service._is_local(host):
        return ToolResult(
            False,
            f"{tool} needs a hardware bridge node: '{host or 'studio'}' is this hub, "
            "which has no I2C bus, GPIO header or ADC. Pass host='bench' for the "
            "Core2 bench node, or another host running a device bridge.",
        )
    return None


def _as_int(value: Any, name: str) -> int:
    """Accept 60, '60' or '0x3c' — the model writes bus addresses either way."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number.")
    if isinstance(value, int):
        return value
    return int(str(value).strip(), 0)


def _hex_addr(value: int) -> str:
    return f"0x{value:02x}"


async def i2c_scan(
    service: DeviceService,
    host: str = "bench",
    bus: int = 0,
) -> ToolResult:
    """Probe every 7-bit address on an I2C bus. Read-only; no approval needed."""
    refusal = _bridge_only(service, host, "i2c_scan")
    if refusal is not None:
        return refusal
    try:
        bus_i = _as_int(bus, "bus")
    except ValueError as exc:
        return ToolResult(False, str(exc))

    remote = await service._remote(
        "device_i2c_scan", {"bus": bus_i}, timeout=20.0, host=host,
    )
    if not remote["ok"]:
        return ToolResult(False, remote["error"])

    found: List[str] = []
    for entry in remote["result"].get("addresses") or []:
        try:
            found.append(_hex_addr(_as_int(entry, "address")))
        except ValueError:
            found.append(str(entry))

    body = "\n".join(f"{a} responded to a bus probe" for a in found)
    return ToolResult(
        True,
        f"{len(found)} device(s) on I2C bus {bus_i} at {host}",
        body or f"No device answered on bus {bus_i}",
        {"host": host, "bus": bus_i, "addresses": found},
    )


async def i2c_io(
    service: DeviceService,
    addr: str,
    host: str = "bench",
    register: Optional[int] = None,
    write: Optional[str] = None,
    write_encoding: str = "hex",
    read_len: int = 0,
    bus: int = 0,
) -> ToolResult:
    """One I2C transaction: optional write, optional read-back, same start.

    A register read is free. A non-empty `write` puts bytes into a part that may
    have its configuration — or its firmware — behind that register, so it waits
    on the same approval card a serial write does.
    """
    refusal = _bridge_only(service, host, "i2c_io")
    if refusal is not None:
        return refusal
    try:
        addr_i = _as_int(addr, "addr")
        bus_i = _as_int(bus, "bus")
        register_i = None if register is None or register == "" else _as_int(register, "register")
    except ValueError as exc:
        return ToolResult(False, f"Bad I2C argument: {exc}")
    if not (0x00 <= addr_i <= 0x7f):
        return ToolResult(False, f"addr must be a 7-bit I2C address (0x00-0x7f), got {addr}.")
    if register_i is not None and not (0 <= register_i <= 0xff):
        return ToolResult(False, "register must be a single byte (0-255).")

    try:
        payload = _decode_payload(write, write_encoding) if write else b""
    except (ValueError, binascii.Error) as exc:
        return ToolResult(False, f"Bad write payload: {exc}")
    if len(payload) > MAX_I2C_BYTES:
        return ToolResult(
            False, f"I2C write exceeds {MAX_I2C_BYTES} bytes; send it a page at a time."
        )

    read_n = max(0, min(int(read_len or 0), MAX_I2C_BYTES))
    if not payload and read_n == 0:
        return ToolResult(False, "i2c_io needs a write, a read_len, or both.")

    if payload:
        where = f"register {_hex_addr(register_i)}" if register_i is not None else "no register"
        denied = await service.approve_write(
            f"Write {len(payload)} byte(s) to I2C {_hex_addr(addr_i)} ({where}) on bus {bus_i}",
            {
                "device": f"i2c {_hex_addr(addr_i)}",
                "host": host,
                "bus": bus_i,
                "register": register_i,
                "bytes": len(payload),
                "preview_hex": _encode_payload(payload[:64], "hex"),
            },
        )
        if denied is not None:
            return denied

    remote = await service._remote(
        "device_i2c_io",
        {
            "bus": bus_i,
            "addr": _hex_addr(addr_i),
            "register": register_i,
            "write_hex": _encode_payload(payload, "hex") if payload else "",
            "read_len": read_n,
        },
        timeout=20.0,
        host=host,
    )
    if not remote["ok"]:
        return ToolResult(False, remote["error"])
    try:
        received = _decode_payload(remote["result"].get("data_hex") or "", "hex")
    except (ValueError, binascii.Error) as exc:
        return ToolResult(False, f"Bridge returned bad data: {exc}")

    received, clipped = _clip(received, MAX_I2C_BYTES)
    summary = (
        f"{'wrote ' + str(len(payload)) + 'B, ' if payload else ''}"
        f"read {len(received)}B from I2C {_hex_addr(addr_i)}"
    )
    return ToolResult(
        True, summary, _encode_payload(received, "hex"),
        {
            "host": host, "bus": bus_i, "addr": _hex_addr(addr_i),
            "register": register_i, "bytes": len(received), "truncated": clipped,
        },
    )


async def gpio_io(
    service: DeviceService,
    pin: int,
    host: str = "bench",
    direction: str = "read",
    value: Optional[int] = None,
    pull: str = "none",
) -> ToolResult:
    """Read a pin, or drive one. Driving is a write and needs approval.

    `direction` rather than the more natural `mode`: the dispatcher strips a
    `mode` argument before calling a tool, because the agent loop chooses the
    execution mode and a model must not. A pin tool that took one would have it
    silently removed and every drive would read instead.
    """
    refusal = _bridge_only(service, host, "gpio_io")
    if refusal is not None:
        return refusal
    direction = str(direction or "read").strip().lower()
    if direction not in ("read", "write"):
        return ToolResult(False, "direction must be 'read' or 'write'.")
    pull = str(pull or "none").strip().lower()
    if pull not in ("none", "up", "down"):
        return ToolResult(False, "pull must be 'none', 'up', or 'down'.")
    try:
        pin_i = _as_int(pin, "pin")
    except ValueError as exc:
        return ToolResult(False, str(exc))
    if pin_i < 0:
        return ToolResult(False, "pin must be a non-negative pin number.")

    level: Optional[int] = None
    if direction == "write":
        if value is None:
            return ToolResult(False, "gpio_io direction='write' needs value=0 or value=1.")
        try:
            level = 1 if _as_int(value, "value") else 0
        except ValueError as exc:
            return ToolResult(False, str(exc))
        denied = await service.approve_write(
            f"Drive GPIO {pin_i} {'high' if level else 'low'} on {host}",
            {
                "device": f"gpio {pin_i}",
                "host": host,
                "pin": pin_i,
                "value": level,
            },
        )
        if denied is not None:
            return denied

    remote = await service._remote(
        "device_gpio",
        {"pin": pin_i, "direction": direction, "value": level, "pull": pull},
        timeout=15.0,
        host=host,
    )
    if not remote["ok"]:
        return ToolResult(False, remote["error"])
    try:
        read_back = _as_int(remote["result"].get("value", level or 0), "value")
    except ValueError:
        return ToolResult(False, "Bridge returned a non-numeric pin value.")

    verb = "drove" if direction == "write" else "read"
    return ToolResult(
        True, f"{verb} GPIO {pin_i} = {read_back} on {host}",
        data={"host": host, "pin": pin_i, "direction": direction, "value": read_back},
    )


async def adc_read(
    service: DeviceService,
    pin: int,
    host: str = "bench",
    samples: int = 1,
) -> ToolResult:
    """Read an analog pin. Read-only; no approval needed."""
    refusal = _bridge_only(service, host, "adc_read")
    if refusal is not None:
        return refusal
    try:
        pin_i = _as_int(pin, "pin")
    except ValueError as exc:
        return ToolResult(False, str(exc))
    if pin_i < 0:
        return ToolResult(False, "pin must be a non-negative pin number.")
    # Averaging is the bridge's job; the cap keeps one tool call bounded.
    samples_i = max(1, min(int(samples or 1), 64))

    remote = await service._remote(
        "device_adc", {"pin": pin_i, "samples": samples_i}, timeout=15.0, host=host,
    )
    if not remote["ok"]:
        return ToolResult(False, remote["error"])
    result = remote["result"]
    try:
        raw = _as_int(result.get("raw", 0), "raw")
        millivolts = _as_int(result.get("mv", 0), "mv")
    except ValueError:
        return ToolResult(False, "Bridge returned a non-numeric ADC reading.")

    return ToolResult(
        True, f"ADC pin {pin_i} = {millivolts} mV (raw {raw}) on {host}",
        data={"host": host, "pin": pin_i, "raw": raw, "mv": millivolts,
              "samples": samples_i},
    )


# Names offered to the model — keep in sync with tool_schemas / dispatch.
DEVICE_TOOLS = (
    "list_devices",
    "serial_open",
    "serial_io",
    "serial_close",
    "usb_transfer",
    "usb_control",
    "net_connect",
    "net_io",
    "net_scan",
    "i2c_scan",
    "i2c_io",
    "gpio_io",
    "adc_read",
)
