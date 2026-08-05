# 0015 — Reverse-engineering mode: live device I/O

Status: accepted
Date: 2026-08-04
Follows 0012 (reverse-engineering mode). Uses the approval channel from 0005.

## Context

ADR 0012 deliberately made RE mode static-inspection only. The coding allowlist
cannot run the artifact; the inspection allowlist has no interpreters. That is
the right answer for someone else's binary.

It is the wrong answer for a part sitting on the bench. Reverse-engineering a
sensor, a motor controller, or a radio almost always means talking to it —
serial, USB control transfers, a TCP service on its LAN address. Stuffing
`minicom` / `screen` / `nc` into `INSPECTION_RULES` would reintroduce every
problem ADR 0011 and 0012 spent pages preventing: unbounded blocking reads,
stdin that `run_command` pins to `DEVNULL`, 40KB text truncation of binary
payloads, and no human gate on a write that can brick the part.

## Decision

A new RE-only tool class in `skippy_device.py`, offered only by `re_tools()`,
never by `workspace_tools()`.

### Reads are free; writes ask

`list_devices`, pure serial/USB/network reads, and `net_scan` proceed without a
human. Any tool call that sends bytes to hardware — a non-empty `serial_io`
write, a USB OUT / host-to-device control transfer, a `net_io` send — waits on
an approve/deny card (`type: "device_auth"`) through the existing
`hub.request_authorization` channel from ADR 0005. Timeout and a missing client
both fail closed.

### Devices are not sandboxed by path

`/dev/cu.*` lives outside every workspace root, so `Sandbox.resolve` would
refuse it. The boundary is enumeration plus a session handle: you can only talk
to something `list_devices` has named, and only through a handle `serial_open`
(or `net_connect`) issued. Binary payloads move as hex/base64 with their own
size cap (`MAX_IO_BYTES`), bypassing the text compressor that would otherwise
corrupt them.

### The host can be local or remote

`host=studio` (default) runs through pyserial / pyusb / sockets in-process on
the hub. Any other host routes through `hub.execute_tool_on_client` to a
device-bridge client (client_id `devices`), the same RPC channel the Cursor
extension already uses. The model sees one tool surface either way.

### Request/response, not a live monitor

Exchanges are bounded write-then-read-with-timeout and time-boxed captures
(max 30s). A persistent always-on stream belongs in a dedicated analyzer, not
an agent tool call that would pin a step for minutes.

## Consequences

An RE session can probe a live part and still record findings with evidence.
Coding mode is untouched. The approval UI is required for the write path to be
usable — SkippyMac is the first client that speaks `device_auth`; headless
runs without a client fail closed on writes, which is correct.
