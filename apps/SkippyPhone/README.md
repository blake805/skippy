# SkippyPhone

The iPhone seat for Skippy — the same three pages as the Mac app (Work, Voice,
Settings) speaking the same hub protocol (`/ws/factory` and `/ws/voice` on the
Mac Studio, port 8000).

## What is and is not here

- **Work**: Chat / Code / RE modes against the hub, with the full agent
  timeline (thoughts, tool calls, patches) and device-write approval sheets.
- **Voice**: full-duplex speech with barge-in, or hold-to-talk (the phone
  equivalent of the Mac's spacebar push-to-talk). Uses `.voiceChat` audio mode
  so the hardware echo canceller keeps the speaker out of the mic.
- **No device bridge**: an iPhone has no serial ports to share, so RE mode's
  `host=` targeting simply never offers this phone.

## Install (one-time, needs your Apple ID)

Building for a physical iPhone requires a signing team, which the command line
cannot do without a signed-in Xcode. On the MacBook:

1. Open `SkippyPhone.xcodeproj` in Xcode.
2. In the target's Signing & Capabilities tab pick your personal team
   (add your Apple ID under Xcode → Settings → Accounts if it is missing).
3. Select your iPhone as the run destination and press Run.
4. On the phone: Settings → General → VPN & Device Management → trust the
   developer certificate.

With a free Apple ID the app expires after 7 days and needs a re-run from
Xcode; a paid developer account extends that to a year.

## First-launch settings

In the app's Settings tab:

- **Host**: the Mac Studio's IP (`192.168.1.151` on the shop LAN), or its
  Tailscale name once cloud access is set up.
- **Port**: 8000.
- **Voice token**: copy it from the SkippyServer app on the Studio.
