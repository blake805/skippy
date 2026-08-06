"""The BLE side of the bench node: a relay from a Core2 to the hub.

The io-node firmware speaks one protocol — JSON lines, ADR 0020 shapes — over
two transports. On Wi-Fi it holds its own WebSocket to the hub. Over BLE it
advertises a Nordic-UART-style service, and this bridge is the other half:
it runs on the laptop that is physically near the node, connects as a BLE
central, and relays the same lines to the hub's /ws/factory lane as
client_id "devices:<node>". The hub cannot tell the transports apart, which
is the point — nothing above this file changes.

Framing: JSON lines chunked to the BLE MTU in both directions, reassembled on
'\n'. The first line the bridge writes is {"type": "hello", "token": ...};
a node with a token refuses anything else and disconnects. After that the
node's own hello and node_status flow through, with one addition: the bridge
stamps "transport": "ble" onto node_status so the app can say how the node is
attached.

Lifecycle: scan, connect, authenticate, relay, and on either side dropping,
tear down and start over. The hub socket is closed when BLE drops so the hub
marks the node offline rather than showing a ghost.

Run it on the laptop:

    python skippy_ble_bridge.py --hub ws://192.168.1.151:8000

The token comes from --token or SKIPPY_FACTORY_TOKEN, same as everything else.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Iterator, List, Optional

logger = logging.getLogger("skippy.ble_bridge")

# Must match firmware/core2-devio/src/main.cpp.
BLE_SERVICE_UUID = "b7f80001-9a3c-4f4e-8a52-6e0d7c3b2a19"
BLE_RX_UUID = "b7f80002-9a3c-4f4e-8a52-6e0d7c3b2a19"  # bridge -> node
BLE_TX_UUID = "b7f80003-9a3c-4f4e-8a52-6e0d7c3b2a19"  # node -> bridge

# Conservative default when the negotiated MTU is unknowable; bleak on macOS
# reports the usable size via client.mtu_size once connected.
DEFAULT_CHUNK = 100

SCAN_TIMEOUT_S = 10.0
HELLO_TIMEOUT_S = 5.0
RETRY_DELAY_S = 3.0


# --- the protocol, kept free of any radio so the tests need none ------------

class LineAssembler:
    """Reassembles newline-framed text from arbitrary chunks.

    The firmware's mirror image: bytes arrive in MTU-sized pieces with the
    frame boundary nowhere in particular, and a line only exists once its
    newline does. Oversized garbage (a peer that is not speaking the protocol)
    is dropped rather than accumulated forever.
    """

    MAX_LINE = 32768

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> List[str]:
        lines: List[str] = []
        for byte in data:
            if byte == 0x0A:  # '\n'
                if self._buf:
                    lines.append(self._buf.decode("utf-8", errors="replace"))
                self._buf.clear()
            else:
                self._buf.append(byte)
                if len(self._buf) > self.MAX_LINE:
                    self._buf.clear()
        return lines


def hello_line(token: str) -> str:
    """The first line on a fresh BLE link: prove who is asking."""
    return json.dumps({"type": "hello", "token": token})


def annotate_for_hub(line: str) -> str:
    """Stamp the transport onto telemetry; pass everything else through as-is.

    Only node_status is touched. Replies are sacred — a task_id reply that a
    relay rewrote is a class of bug nobody should ever have to debug — so the
    original text is forwarded byte-for-byte unless this is telemetry.
    """
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return line
    if isinstance(data, dict) and data.get("type") == "node_status":
        data["transport"] = "ble"
        return json.dumps(data)
    return line


def node_name_from_hello(line: str) -> Optional[str]:
    """The node's name from its own hello, or None if this is not that."""
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if isinstance(data, dict) and data.get("type") == "hello" and data.get("node"):
        return str(data["node"])
    return None


def chunk_for_ble(line: str, chunk_size: int = DEFAULT_CHUNK) -> Iterator[bytes]:
    """A framed line, cut to what one BLE write carries."""
    framed = line.encode("utf-8") + b"\n"
    for offset in range(0, len(framed), chunk_size):
        yield framed[offset : offset + chunk_size]


# --- the relay ---------------------------------------------------------------

class BleBridge:
    def __init__(self, hub_url: str, token: str):
        self.hub_url = hub_url.rstrip("/")
        self.token = token

    async def run_forever(self) -> None:
        while True:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Session ended: %s", exc)
            await asyncio.sleep(RETRY_DELAY_S)

    async def _session(self) -> None:
        # Imported here so the protocol above is importable (and testable)
        # on machines with no Bluetooth stack at all.
        from bleak import BleakClient, BleakScanner

        logger.info("Scanning for a skippy io-node...")
        device = await BleakScanner.find_device_by_filter(
            lambda d, ad: BLE_SERVICE_UUID in (ad.service_uuids or []),
            timeout=SCAN_TIMEOUT_S,
        )
        if device is None:
            raise RuntimeError("no node advertising the skippy service in range")
        logger.info("Found %s (%s), connecting...", device.name or "node", device.address)

        assembler = LineAssembler()
        from_node: asyncio.Queue[str] = asyncio.Queue()
        disconnected = asyncio.Event()

        def on_notify(_handle: object, data: bytearray) -> None:
            for line in assembler.feed(bytes(data)):
                from_node.put_nowait(line)

        def on_disconnect(_client: object) -> None:
            disconnected.set()

        async with BleakClient(device, disconnected_callback=on_disconnect) as client:
            await client.start_notify(BLE_TX_UUID, on_notify)
            chunk_size = max(20, (getattr(client, "mtu_size", 23) or 23) - 3)

            # Prove ourselves, then wait for the node to say who it is. The
            # name matters: it becomes the client_id the hub routes by.
            await self._write_line(client, hello_line(self.token), chunk_size)
            node = await self._await_hello(from_node)
            logger.info("Node '%s' answered; connecting to the hub.", node)

            await self._relay(client, from_node, disconnected, node, chunk_size)

    async def _write_line(self, client, line: str, chunk_size: int) -> None:
        # Write-without-response, in order: BLE guarantees ordered delivery on
        # a connection, and waiting for a round trip per chunk would make a
        # 4KB write a ten-second affair.
        for piece in chunk_for_ble(line, chunk_size):
            await client.write_gatt_char(BLE_RX_UUID, piece, response=False)

    async def _await_hello(self, from_node: asyncio.Queue) -> str:
        deadline = asyncio.get_running_loop().time() + HELLO_TIMEOUT_S
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise RuntimeError(
                    "the node never said hello - wrong token, or not a skippy node"
                )
            line = await asyncio.wait_for(from_node.get(), timeout=remaining)
            name = node_name_from_hello(line)
            if name:
                return name

    async def _relay(
        self,
        client,
        from_node: asyncio.Queue,
        disconnected: asyncio.Event,
        node: str,
        chunk_size: int,
    ) -> None:
        import websockets

        url = f"{self.hub_url}/ws/factory?client_id=devices:{node}"
        if self.token:
            url += f"&token={self.token}"

        async with websockets.connect(url, max_size=2**22) as hub:
            logger.info("Relaying: %s <-BLE-> hub as devices:%s", node, node)
            # The hub missed the hello that authenticated us; re-introduce the
            # node so note_bridge records it.
            await hub.send(json.dumps(
                {"type": "hello", "role": "devices", "node": node, "transport": "ble"}
            ))

            async def node_to_hub() -> None:
                while True:
                    line = await from_node.get()
                    await hub.send(annotate_for_hub(line))

            async def hub_to_node() -> None:
                async for message in hub:
                    if isinstance(message, bytes):
                        message = message.decode("utf-8", errors="replace")
                    await self._write_line(client, message, chunk_size)

            async def watch_ble() -> None:
                await disconnected.wait()
                raise RuntimeError("the BLE link dropped")

            tasks = [
                asyncio.create_task(node_to_hub()),
                asyncio.create_task(hub_to_node()),
                asyncio.create_task(watch_ble()),
            ]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
                for task in done:
                    task.result()  # surfaces whichever side failed
            finally:
                for task in tasks:
                    task.cancel()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--hub",
        default=os.environ.get("SKIPPY_HUB_URL", "ws://127.0.0.1:8000"),
        help="hub base URL (ws://host:port); default from SKIPPY_HUB_URL or loopback",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("SKIPPY_FACTORY_TOKEN", ""),
        help="factory lane token; default from SKIPPY_FACTORY_TOKEN",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        asyncio.run(BleBridge(args.hub, args.token).run_forever())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
