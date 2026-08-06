# 0021 — BLE as a second transport for the bench node

Status: accepted
Date: 2026-08-06
Follows 0020 (wireless bench node). Changes no wire shapes.

## Context

ADR 0020 made the Core2 a wireless bench node, but "wireless" meant the shop
Wi-Fi: the node holds its own WebSocket to the hub, so it works anywhere on
the network and nowhere off it. The actual use for the node turns out to be
broader — a carry-along probe clipped to a target in an RE environment, where
the only other machine in the room is the MacBook and there is no network to
join.

Bluetooth LE reaches exactly that case. The Core2's ESP32 has the radio, the
MacBook has CoreBluetooth, and the traffic — JSON lines, 4 KB bounded
exchanges per ADR 0015 — fits comfortably in BLE's throughput. What BLE cannot
carry is a WebSocket, and what it must not become is a second protocol.

## Decision

### One protocol, two transports

The ADR 0020 shapes are untouched. The firmware routes the same JSON lines
over whichever link is up: its own WebSocket on Wi-Fi, or a Nordic-UART-style
GATT service (write characteristic in, notify characteristic out, lines
chunked to the negotiated MTU, framed on `\n`) when a BLE central holds an
authenticated connection.

### The bridge is a relay, not a peer

`skippy_ble_bridge.py` runs on the laptop near the node. It scans for the
service UUID, authenticates, then connects to the hub as
`client_id=devices:<node>` — the node's own id — and relays lines verbatim
both ways. The hub cannot tell the transports apart and needs no changes
beyond one additive field: the bridge stamps `"transport": "ble"` onto
`node_status`, and `transport` joined `NODE_FIELDS` so the app can show how a
node is attached. Replies are forwarded byte-for-byte; a relay that rewrites a
`task_id` reply is a bug class nobody should meet.

### Exactly one client per node

The hub keys connections by client id, so a node must never be connected
twice. While a bridge holds the BLE link the node drops its own hub socket,
and reopens it when the bridge goes away. BLE wins because the bridge being
in range means the human carried the laptop to the node, which is the stronger
signal about where work is happening.

### The token gates the air

BLE has no LAN to hide behind: anyone in radio range can connect. The first
line a central sends must be `{"type": "hello", "token": ...}` matching
`SKIPPY_TOKEN`; a wrong token or ten seconds of silence disconnects the
central. It is the same shared secret the factory lane already uses, carried
one hop further.

### Pause means both radios

The node's one control took on the second radio: Pause drops the hub socket
and stops BLE advertising, and nothing reconnects either until the human taps
again. A safety guarantee that only covered one of two ways in was not one.

## Consequences

The node works where there is no network, at BLE range — roughly ten meters,
a same-room tool. The bridge is one more process to run, on the laptop, and
its absence is visible rather than mysterious: the node's screen says
"Waiting BLE" and the hub marks the node offline when the bridge closes.

A Wi-Fi-less build (`WIFI_SSID ""`) never touches the Wi-Fi radio, which is
the right posture for a probe carried into someone else's building: it emits
nothing but its own advertisement and joins nothing.

Throughput drops relative to Wi-Fi — a full 4 KB exchange is around a second
over BLE — which the bounded request/response model absorbs without protocol
changes. A persistent stream would not fit; per ADR 0015, it was never
allowed.
