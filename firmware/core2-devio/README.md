# Skippy bench IO node (M5Stack Core2)

A wireless pair of hands on the bench. The Core2 reaches the hub's
`/ws/factory` lane as `client_id=devices:bench` and answers the device actions
Skippy's RE mode sends: enumerate, talk to a UART, scan and read an I2C bus,
read or drive a pin, sample an analog input. The agent, the approval card and
every decision stay on the Mac Studio (`skippy_device.py`), so the node is a
dumb executor and a model change never requires a reflash. ADR 0020 pins the
wire shapes.

Two transports carry that one protocol. On the shop network the node holds its
own WebSocket to the hub over Wi-Fi. Away from any network it is a BLE
peripheral: the laptop next to it runs `skippy_ble_bridge.py`, which relays
the same JSON lines to the hub on the node's behalf. That makes the Core2 a
carry-along probe — clip it to a target anywhere, and the MacBook is the only
other thing you need in the room. BLE wins when both links are possible, and
the node drops its own hub socket while a bridge holds the BLE link so the hub
only ever sees one `devices:bench`.

## Build and flash

1. Install [PlatformIO](https://platformio.org/) (`pip install platformio`).
2. `cp include/secrets.h.example include/secrets.h` and fill in Wi-Fi
   credentials, the Mac Studio's LAN address, the factory token, and the node
   name (`bench` unless you have more than one). Leave `WIFI_SSID` as `""` for
   a BLE-only node that never touches Wi-Fi.
3. Plug in the Core2 and run `pio run -t upload` from this directory.

## Server side

The hub binds loopback by default, which the Core2 cannot reach. Start it with
a LAN bind and a token — the token is what makes the LAN bind defensible, and
`/ws/factory` is the lane that can start agent runs:

```bash
SKIPPY_BIND_HOST=0.0.0.0 SKIPPY_FACTORY_TOKEN=some-long-secret python skippy_factory.py
```

Prefer a private interface (Tailscale) over `0.0.0.0` where you can. The token
in `secrets.h` must match `SKIPPY_FACTORY_TOKEN`, or the hub closes the
connection with code 1008 before accepting it and the screen stays on
"no server".

## The BLE bridge

Where there is no bench network, the laptop is the network. The node
advertises as `skippy-<name>` with a Nordic-UART-style service (one write
characteristic in, one notify characteristic out, JSON lines chunked to the
MTU), and the bridge relays it to the hub:

```bash
pip install bleak websockets
SKIPPY_FACTORY_TOKEN=some-long-secret python skippy_ble_bridge.py --hub ws://192.168.1.151:8000
```

The hub can be anywhere the laptop can reach — the Mac Studio over Tailscale,
or a hub running on the laptop itself. BLE is open air, so the first line a
central sends must be a hello carrying the same factory token; a wrong token,
or ten seconds of silence, gets the central disconnected. The bridge stamps
`"transport": "ble"` onto the node's telemetry so the app can say how the node
is attached. Range is roughly ten meters line of sight: a same-room tool,
which is what a probe is.

## Using it

Nothing on the device: it has no controls, because it makes no decisions. From
an RE session, pass `host="bench"` to the device tools and they route here:

```
list_devices(host="bench")
serial_open(host="bench", port="port-c", baud=115200)
serial_io(handle=..., read_until_idle=0.5)

i2c_scan(host="bench")
i2c_io(host="bench", addr="0x68", register=0x75, read_len=1)
gpio_io(host="bench", pin=26, direction="read", pull="up")
adc_read(host="bench", pin=36)
```

Reads happen straight away. Anything that sends bytes — a serial write, an I2C
write, driving a pin — waits for you to approve a card in SkippyMac first. The
card appears on the machine that started the run, never on the Core2.

## The screen

```
 SKIPPY  devices:bench                                +82% [====]
 ────────────────────────────────────────────────────────────────
 ●  Connected
    192.168.1.42   -54 dBm                    192.168.1.151:8000
 ────────────────────────────────────────────────────────────────
 01:12  i2c scan -> 2                                          ▐
 01:20  i2c 0x68 w1 r1                                         ▐
 02:03  uart w4 r18
 ────────────────────────────────────────────────────────────────
 17 actions  up 12m  uart 115200                    -3 tap: live
 [ Pause ]             [ Clear ]              [ Info ]
```

Black background, white text, and colour only where it means something —
green/amber/red state, blue for the newest action. The whole panel is a
touchscreen, not just the three dots below the glass. The frame is composited
into a PSRAM sprite and pushed whole, so nothing flickers, and the activity
log keeps 40 lines of scrollback:

- **Drag** in the activity log to scroll the history; a scrollbar and a
  `-N` badge in the stats strip say how far from live you are. New actions
  keep your place. **Tap the stats strip** to snap back to the live tail.
- **Pause / Connect** takes the node off the air — both radios: the hub
  socket drops and BLE stops advertising, and both stay down until you tap
  again. With the link down, no agent anywhere can drive a pin or push a byte
  into whatever is clipped to the ports — a guarantee you can make with a
  fingertip before putting your hands in the circuit, and watch on the screen
  while they are in there. It works mid-capture too; the node finishes the
  action it is on and then drops.
- **Clear** resets the log and the action count.
- **Info** shows firmware, client id, hub address and whether a token is set,
  Wi-Fi, Bluetooth state, MAC, battery voltage, uptime, free heap and the pin
  map. Hold the reboot button on that page (on the glass or the third dot) to
  reboot.

The status block says which link is live: an IP address and signal for Wi-Fi,
`BLE <central>  via bridge` when a bridge holds the link, and `Waiting BLE`
on a node with no Wi-Fi configured and no bridge yet in range.

The three capacitive dots below the display still map to the same three
actions, left to right. The screen dims after two minutes and the first touch
only wakes it, so reaching for a dark panel cannot disconnect the bench by
accident.

## The light bar and the chirp

A screen is only readable from a foot away, and a bench is a place where your
eyes are on the part. The M5GO base's side LEDs carry the same state across the
room: green connected and idle (either transport), amber connecting or
waiting for a BLE bridge, red for a Wi-Fi network that should be there and is
not, blue link paused by hand, cyan for the length of an action. **Any write — serial bytes, an
I2C write, driving a pin — flashes red and chirps**, whether or not you are
looking at the node. Reads are silent, which is the distinction that matters
when your hands are in the circuit.

Set `LED_BAR_COUNT=0` in `platformio.ini` for a bare Core2 or a different base.
On the M5GO Bottom2 the bar is on G25; nothing else uses that pin, so leaving it
enabled on a board without one is harmless.

Every 15 seconds the node reports its battery, signal, uptime and whether it is
mid-action to the hub. Any client can ask the hub for the list with
`{"action": "bridge_nodes"}` and get every node it has heard from, online or
not — that is what makes a node pickable in an app rather than a name you have
to remember.

## Pin map (IoT Dev Kit v2, standard Grove assignments)

| Port | Pins | Use |
| --- | --- | --- |
| A | G32 SDA / G33 SCL | I2C bus 0 (`i2c_scan`, `i2c_io`) |
| B | G36 in, G26 out | ADC (`adc_read`) and GPIO (`gpio_io`) |
| C | G13 RX / G14 TX | UART (`serial_*` tools), `Serial2` |

Those pins are the whole surface: a request for any other pin is refused. The
rest of the header runs the screen, the power management chip and the internal
I2C bus, and an agent able to drive one of them could brick the node. G36 is
input-only and has no internal pull resistors, so `gpio_io(pin=36, pull=...)`
is refused rather than quietly ignored.

**Levels.** Every Grove pin is 3.3V logic. A 5V part needs a level shifter, and
an RS-232 port needs a transceiver — wiring either straight to a port damages
the Core2. The I2C bus needs pull-ups; most Grove sensor units bring their own,
a bare chip on a breadboard does not.

## What it will not do

- **USB.** The Core2's USB port is its programming UART, not a host
  controller, so `usb_transfer` and `usb_control` come back as "unsupported on
  this node". Use `host="macbook"` for USB work.
- **Start anything.** The node only replies to actions carrying a `task_id`.
  The server enforces the same rule from its side: a `devices*` client can send
  replies and a `hello` and nothing else, so a compromised node cannot drive
  the agent.
- **Capture continuously.** Exchanges are bounded request/response per ADR
  0015: a write-then-read, a byte count, or a time-boxed capture of at most
  30 seconds and 4 KB, whichever comes first. A long trace belongs on a logic
  analyzer. One I2C transaction is capped at 128 bytes, which is the driver's
  buffer rather than a policy — read a larger part a page at a time.
