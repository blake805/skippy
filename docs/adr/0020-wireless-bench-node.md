# 0020 — A wireless bench node, and the auth that makes it survivable

Status: accepted
Date: 2026-08-05
Follows 0015 (RE live device I/O). Uses the approval channel from 0005.

## Context

ADR 0015 gave RE mode live device I/O and routed any non-local `host` through
`hub.execute_tool_on_client` to a bridge client. In practice the bridge is a
Mac running SkippyMac, which means the part under test has to be plugged into
a laptop, and the laptop has to be at the bench.

An M5Stack Core2 sitting on the bench permanently is a better answer for the
hardware side: it is already on the LAN, it costs nothing to leave powered, and
its Grove ports expose the three things a serial-only bridge cannot — an I2C
bus, a GPIO pin, and an ADC channel. Reverse-engineering a part means reading
its bus and watching its pins at least as often as it means talking to its
UART.

Three things stood in the way.

The bridge id was a single well-known string, `devices`, so a second bridge had
nowhere to live. `/ws/factory` had no authentication at all, and any message on
it that is not a reply, a greeting or a cancel starts an agent run — ADR 0015's
consequences flagged this and the loopback bind of ADR 0014 was the only thing
holding it. A permanent wireless node forces a non-loopback bind, which turns
that gap into the actual security boundary. And the node itself is an ESP32 on
a LAN: assuming it will never be compromised is not a plan.

## Decision

### Named bridges

`host` picks the bridge. `bridge_client_id(host)` maps `"bench"` to the client
id `devices:bench`; `DeviceService._bridge_for` uses that id when such a client
is connected, and otherwise falls back to the bare `devices` id. So the Core2
and a Mac bridge coexist, `host="macbook"` keeps reaching a single unnamed Mac
bridge exactly as before, and the offline error names both ids it looked for.

### A token on /ws/factory

`SKIPPY_FACTORY_TOKEN`, checked as a `token` query parameter, closing with code
1008 before accept on a mismatch — the same mechanism and the same shape as
`skippy_voice._authorized`, because this lane is strictly more dangerous than
the voice one. Unset means allow, which keeps loopback development unchanged;
the boot warning for a non-loopback bind now says which of the two you have.

### A bridge cannot start a run

A client whose id is `devices` or begins `devices:` is RPC-only. Its whole
vocabulary is `task_id` replies, a `hello`, and `node_status` telemetry;
anything else is refused and logged rather than reaching `runner.start` or the
dashboard actions. A compromised node can lie about a voltage. It cannot edit a
repository.

### The node reports itself

Presence is not enough to trust a bench node in another room. The node pushes a
`node_status` every 15 seconds and on connect; the hub keeps the last one per
client id, including after a disconnect, and answers a `bridge_nodes` query
with the lot. That is what lets a client offer a `host` worth picking — with
its battery, signal and whether it is mid-action — instead of a name to type.

```jsonc
// node -> hub, unsolicited, no task_id
{"type": "node_status", "node": "bench", "firmware": "io-node 1.0",
 "battery": 82, "charging": false, "rssi": -54, "ip": "192.168.1.42",
 "uptime_s": 1200, "actions": 17, "busy": false, "uart_open": true,
 "ports": ["uart", "i2c", "gpio", "adc"]}

// any non-bridge client -> hub
{"action": "bridge_nodes"}
{"type": "bridge_nodes", "nodes": [
  {"client_id": "devices:bench", "host": "bench", "online": true,
   "seen_seconds_ago": 3.2, "battery": 82, "rssi": -54, ...}]}
```

Pairing, in the sense of a device the app adopts by name, is this plus a
decision about who may connect at all — and that decision is the token. A
richer flow (a code on the node's screen, per-node tokens the hub can revoke,
Wi-Fi provisioned over a captive portal so credentials are not compiled in)
buys real things once there is more than one node, and is deliberately not
built yet: with one node on a private interface, the token and this list are
the whole of what "paired" needs to mean.

### Pins and buses are their own tools

`i2c_scan`, `i2c_io`, `gpio_io` and `adc_read` join the RE device set, under
the same rule as the rest: reads are free, anything that drives the wires waits
on a `device_auth` card, which goes to the client that started the run and
never to the node. They have no local backend — a Mac Studio has no I2C bus —
so a local host is refused with a message naming the bench node rather than
attempted and failed obscurely.

### Wire shapes

Pinned here so firmware and server can be changed independently. Every request
carries `action` and `task_id`; every reply echoes `task_id` and is either
`{"ok": true, "result": {...}}` or `{"ok": false, "error": "..."}`. Binary is
hex, addresses are hex strings, per ADR 0015.

```jsonc
// device_i2c_scan
{"action": "device_i2c_scan", "task_id": "...", "bus": 0}
{"task_id": "...", "ok": true, "result": {"addresses": ["0x3c", "0x68"]}}

// device_i2c_io — register is null for a raw transaction; a read after a
// write is one transaction with a repeated start.
{"action": "device_i2c_io", "task_id": "...", "bus": 0, "addr": "0x68",
 "register": 117, "write_hex": "", "read_len": 1}
{"task_id": "...", "ok": true, "result": {"data_hex": "71", "wrote": 0}}

// device_gpio — direction "read" | "write"; value is null on a read,
// pull is "none" | "up" | "down". The field is "direction" and not "mode"
// because the dispatcher strips a `mode` argument before calling a tool: the
// agent loop picks the execution mode, so a pin tool taking one would have it
// removed and every drive would quietly become a read.
{"action": "device_gpio", "task_id": "...", "pin": 26, "direction": "write",
 "value": 1, "pull": "none"}
{"task_id": "...", "ok": true, "result": {"pin": 26, "value": 1}}

// device_adc — raw count and calibrated millivolts, because the ESP32's ADC
// is neither linear nor consistent between chips.
{"action": "device_adc", "task_id": "...", "pin": 36, "samples": 4}
{"task_id": "...", "ok": true, "result": {"pin": 36, "raw": 2048, "mv": 1650}}
```

The serial actions (`device_list`, `device_serial_open`, `device_serial_io`,
`device_serial_close`) are unchanged from ADR 0015 and the node implements them
as they stand. `device_usb_*` is answered `{"ok": false, "error": ...}` —
"unsupported on this node" — since the Core2's USB port is its programming
UART, not a host controller.

### Still bounded request/response

ADR 0015's rule holds and the hardware agrees with it: a write-then-read, a
byte count, or a time-boxed capture, never a persistent stream. The node clips
serial exchanges at 4KB and 30 seconds, whichever comes first, and says in the
reply when it truncated; an I2C transaction is capped at 128 bytes, the
driver's buffer, and a larger request is refused rather than short-read.
`firmware/core2-devio/` is the implementation; its README carries the pin map
and the 3.3V caveat.

## Consequences

An RE session can probe a part nobody is holding: scan its bus from another
room, read a pin, drive a reset line with a human's approval on the laptop
where the run started. The bench node is a second thing to keep flashed and in
sync with these shapes, which is what the pinned JSON above is for.

The token is a shared secret in `secrets.h` on the device — the same posture as
the voice node, and the right one at this scope. It is not a substitute for a
private interface: prefer Tailscale over `0.0.0.0`, and the boot warning says
so either way. Setting it is opt-in precisely because every other client has to
carry it too: SkippyMac opens two `/ws/factory` sockets (its own and the device
bridge), SkippyPhone opens one, and so does the Cursor extension. Until those
append `&token=`, turning the token on locks them out — which is the correct
failure, but a deliberate one to schedule rather than discover.

The RPC-only rule costs nothing today: SkippyMac's bridge socket already sends
only a `hello` and `task_id` replies, on a connection separate from the one its
UI drives runs over.

Restricting the node to the three Grove ports means a request for any other pin
is refused by the firmware. That is deliberate: the remaining pins run the
screen, the power management chip and the internal I2C bus, and the failure
mode of getting this wrong is a bricked node rather than a bad reading.
