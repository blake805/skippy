# Skippy voice node (M5Stack Core2)

A wireless microphone and speaker for Skippy's `/ws/voice` lane. The device
streams raw 16 kHz PCM to the Mac Studio and plays the PCM that comes back;
all listening, thinking and speaking happens on the server
(`skippy_voice.py`), so model changes never require a reflash.

## Build and flash

1. Install [PlatformIO](https://platformio.org/) (`pip install platformio`).
2. `cp include/secrets.h.example include/secrets.h` and fill in Wi-Fi
   credentials, the Mac Studio's LAN address, and the voice token.
3. Plug in the Core2 and run `pio run -t upload` from this directory.

## Server side

The hub binds loopback by default, which the Core2 cannot reach. Start it
with a LAN bind and a token — the token is what makes the LAN bind defensible:

```bash
SKIPPY_BIND_HOST=0.0.0.0 SKIPPY_VOICE_TOKEN=some-long-secret python skippy_factory.py
```

Note the warning this prints is real: `/ws/factory` has no auth of its own,
so prefer a private interface (Tailscale) over `0.0.0.0` where possible.

## Using it

- **Tap** the screen to open a session and start talking. Speak normally;
  the server's VAD decides when you have finished a sentence.
- **Tap while Skippy is talking** to interrupt him.
- **Hold for one second** to end the session. The server writes a summary of
  the conversation into project memory before hanging up.

## Why barge-in is a tap and not a word

The Core2's PDM microphone and its speaker amplifier share the I2S clock
lines with incompatible configurations, so the hardware can only run one
direction at a time. While Skippy is speaking, the microphone is physically
off. Full acoustic barge-in needs full-duplex hardware (e.g. an
ESP32-S3-Box); the desk client (`clients/voice_client.py`) has it, because a
Mac is full duplex.
