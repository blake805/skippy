# Cloud access to Skippy (from anywhere)

Goal: the SkippyMac and SkippyPhone apps reach the hub when away from the shop
LAN, without exposing port 8000 to the internet.

## Why Tailscale, not port forwarding

`/ws/factory` has no authentication — any message that reaches it can start an
agent run that edits the workspace roots and runs commands (see the
`bind_host()` docstring in `skippy_factory.py` and ADR 0014). That is
acceptable on a private interface and indefensible on a public one. Tailscale
gives every enrolled device a private WireGuard address; nothing is exposed,
and the apps keep speaking plain `ws://` exactly as they do on the LAN.

## Setup (one account, three devices — needs your logins)

1. **Mac Studio** (the hub): install the Tailscale app —
   `brew install --cask tailscale-app` (asks for the admin password), or from
   https://tailscale.com/download. Open it, sign in (Apple/Google/GitHub —
   pick one and use it everywhere).
2. **MacBook**: same install, same account.
3. **iPhone**: App Store → Tailscale → sign in → allow the VPN profile.
4. On the Studio, note its tailnet name (Tailscale menu → the device name,
   e.g. `mac-studio.tail1234.ts.net`) or its 100.x.y.z address.
5. In SkippyMac and SkippyPhone Settings, set **Host** to that name. Port
   stays 8000. The voice token works unchanged.

## Notes

- The hub already binds `0.0.0.0`, so it answers on the tailnet interface with
  no server change. `SKIPPY_BIND_HOST` could later be pinned to the Tailscale
  IP to drop LAN exposure entirely.
- MagicDNS names (`*.ts.net`) survive IP changes; prefer them over the 100.x
  address so a DHCP-style renumber never strands the apps again.
- Voice over the tailnet from a phone on LTE works but pays the round trip:
  expect a few hundred extra milliseconds versus the shop LAN.
