# Speaker echo: what we know (2026-08-05)

Parked work. The full diff lives on the `voice-echo-wip` branch; main was
rolled back to the last known-good build at the user's request.

## The bug

Voice works perfectly on AirPods but breaks on built-in speakers, on both the
Mac and the phone: Skippy's replies cut out mid-sentence with nobody talking.

**Root cause, confirmed:** on speakers, the mic hears Skippy's own TTS. The
hub's VAD flags the echo as speech, and the barge-in path cancels the reply —
at the time, a single 32 ms VAD frame was enough. AirPods mask the bug because
the earbuds cancel echo in their own hardware before it reaches the mic
stream.

## What was tried, and what happened

1. **Hub: sustained-speech barge-in.** `Endpointer` gained a
   `speech_confirmed` event (`SKIPPY_VOICE_BARGE_MS`, default 250 ms); a reply
   in flight is only cancelled on confirmed speech, not the first frame.
   Works, tested (`tests/test_voice.py` on the branch). Keep this — it is
   good on its own, but it cannot fix the bug alone, because a whole spoken
   sentence of echo confirms just as well as a person does.

2. **Apple voice processing (`setVoiceProcessingEnabled`) — the "right" fix.**
   Failed twice, differently per platform:
   - First attempt (VP toggle on the existing graph, playback lane fixed at
     24 kHz): the Mac engine refused to start with `-10875`
     (kAudioUnitErr_FailedInitialization); the phone started but the mic went
     completely deaf — sessions connected, zero utterances ever reached STT.
   - Second attempt (graph rebuilt to match Apple's AVEchoTouch sample:
     stopped engine, VP before any format read, playback at the voice unit's
     rate): everything *ran* — engine up, taps firing, bytes flowing — but the
     captured audio was **pure digital silence** (`peakRms=0` across whole
     sessions, verified with an in-app RMS heartbeat).
   - Likely culprit on this Mac: the VP unit builds an aggregate of the
     default input+output devices, and the **Background Music virtual audio
     devices** poison it. Symptom: the input node reported 9 channels at
     96 kHz against a 3-channel physical mic array.
   - The phone (iOS) VP path was never verified: it needs a keychain-signed
     Xcode build that was not done before parking. VP may genuinely work
     there — iOS is the platform Apple's sample targets.

3. **Echo gate — the fallback that actually worked.** Raw mic (no VP), and
   the client simply does not send mic audio while `state == "speaking"`
   unless the output route is Bluetooth. Nothing on the wire, nothing to trip
   barge-in. **Verified live on the Mac:** real speech RMS (~200–290), clean
   listening → thinking → speaking turn cycles, 25-second replies uncut, hub
   turns at ~70–90 ms STT / <1 s first audio. Trade-off: on speakers you
   interrupt with the button, not by voice; on AirPods (Bluetooth route
   detected via CoreAudio transport type) voice barge-in stays fully live.

## Where things stand after the rollback

- `main` and both installed apps are at commit `9d77976` — the state where
  AirPods work and speakers exhibit the original cut-off bug.
- The hub runs main's `skippy_voice.py` again (one-frame barge-in).
- Branch `voice-echo-wip` (one commit) holds: the hub confirm-window change +
  tests, the Mac echo-gate client (plus a temporary 3 s diagnostic heartbeat
  in `VoiceClient.swift` that logs at error level — strip before shipping),
  and the phone client with VP-attempt + gate fallback (unverified).

## Recommended path when resuming

Cherry-pick the branch, verify the phone's VP path with a signed build, strip
the Mac heartbeat diagnostics, and ship. If phone VP is also silent, the gate
fallback is already wired in and the phone behaves like the Mac. If voice
barge-in on Mac speakers is ever wanted for real, the remaining ideas are:
try VP with the Background Music devices removed, or hub-side echo
suppression (the hub knows exactly what PCM it sent and when; correlate and
subtract, or at least suppress VAD while its own audio window is in flight).
