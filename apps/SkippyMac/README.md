# SkippyMac

Native macOS client for the Skippy hub: chat, coding agent, reverse-engineering
(with live device I/O), and voice — wearing the silver-can icon.

## Build (on the MacBook)

```bash
cd apps/SkippyMac
xcodebuild -scheme SkippyMac -configuration Release \
  -derivedDataPath /tmp/SkippyMac-build build
open /tmp/SkippyMac-build/Build/Products/Release/SkippyMac.app
```

Or open `SkippyMac.xcodeproj` in Xcode and run.

## Settings

- **Hub host** defaults to `192.168.1.151` (Mac Studio).
- **Voice token** must match `SKIPPY_VOICE_TOKEN` on the hub when set.
- **Share devices** registers a `client_id=devices` bridge so RE mode can use
  `host=macbook` for serial/network hardware plugged into this machine. Writes
  still require an on-screen approval.
