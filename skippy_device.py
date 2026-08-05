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

    async def _remote(self, action: str, payload: dict, timeout: float = 30.0) -> dict:
        if self.hub is None:
            return {"ok": False, "error": "No hub available for remote device access."}
        if self.bridge_client_id not in getattr(self.hub, "active_connections", {}):
            return {
                "ok": False,
                "error": (
                    f"No device bridge is connected (expected client_id="
                    f"'{self.bridge_client_id}'). Open SkippyMac on the machine "
                    "that has the hardware and enable device sharing."
                ),
            }
        request = dict(payload)
        request["action"] = action
        response = await self.hub.execute_tool_on_client(
            self.bridge_client_id, request, timeout=timeout
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
            "device_list", {"host": host, "kinds": sorted(wanted)}, timeout=20.0
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
        await service._remote("device_serial_close", {"handle": handle}, timeout=10.0)
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
)
