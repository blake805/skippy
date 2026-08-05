// Skippy voice node for the M5Stack Core2.
//
// The device is a dumb terminal on purpose: it streams raw 16 kHz PCM up the
// websocket and plays raw PCM that comes back. VAD, transcription, the brain
// and the voice all live on the Mac Studio (skippy_voice.py), so a model swap
// on the server never touches this firmware.
//
// The one piece of intelligence it does need is I2S discipline. The Core2's
// PDM microphone and its speaker amplifier share the I2S clock lines with
// incompatible configurations, so the hardware is half-duplex: it cannot hear
// while it speaks. M5Unified owns that switch (M5.Mic <-> M5.Speaker), and the
// server's audio_start/audio_end messages are what drive it. Barge-in is
// therefore a tap on the screen, which sends {"type":"interrupt"}; the server
// answers with audio_cancel and this side throws away its buffered audio.
//
// Touch UX: tap to open a session and start listening. Tap while Skippy is
// talking to interrupt him. Hold ~1s to end the session (the server writes a
// summary of the conversation into project memory) and go back to idle.

#include <M5Unified.h>
#include <WiFi.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include "secrets.h"

static const int SAMPLE_RATE = 16000;
// 512 samples = 32 ms, the server's VAD window.
static const size_t MIC_CHUNK = 512;
// ~4 s of 16-bit PCM in PSRAM. The server sends audio faster than real time,
// which is what makes first-word latency low; this is where the rest waits.
static const size_t RING_SIZE = 128 * 1024;
static const uint32_t HOLD_TO_END_MS = 1000;

enum State { BOOTING, IDLE, LISTENING, THINKING, SPEAKING };

static WebSocketsClient ws;
static State state = BOOTING;
static bool wsConnected = false;
static bool sessionOpen = false;

static int16_t micBuf[MIC_CHUNK];

// Single-producer (websocket callback) / single-consumer (loop) ring buffer.
static uint8_t* ring = nullptr;
static volatile size_t ringHead = 0;  // write position
static volatile size_t ringTail = 0;  // read position
static int playbackRate = SAMPLE_RATE;

static String lastYou = "";
static String lastSkippy = "";

// --- ring buffer -----------------------------------------------------------

static size_t ringUsed() {
  size_t h = ringHead, t = ringTail;
  return h >= t ? h - t : RING_SIZE - t + h;
}

static void ringPush(const uint8_t* data, size_t len) {
  for (size_t i = 0; i < len; i++) {
    size_t next = (ringHead + 1) % RING_SIZE;
    if (next == ringTail) return;  // full: drop late audio rather than block the socket
    ring[ringHead] = data[i];
    ringHead = next;
  }
}

static size_t ringPop(uint8_t* out, size_t maxLen) {
  size_t n = 0;
  while (n < maxLen && ringTail != ringHead) {
    out[n++] = ring[ringTail];
    ringTail = (ringTail + 1) % RING_SIZE;
  }
  return n;
}

static void ringFlush() {
  ringTail = ringHead;
}

// --- display ---------------------------------------------------------------

static void draw() {
  M5.Display.startWrite();
  M5.Display.fillScreen(TFT_BLACK);
  M5.Display.setTextDatum(top_left);

  const char* label = "";
  uint16_t color = TFT_WHITE;
  switch (state) {
    case BOOTING:   label = "connecting..."; color = TFT_DARKGREY;  break;
    case IDLE:      label = "tap to talk";   color = TFT_DARKGREY;  break;
    case LISTENING: label = "listening";     color = TFT_GREEN;     break;
    case THINKING:  label = "thinking";      color = TFT_YELLOW;    break;
    case SPEAKING:  label = "speaking";      color = TFT_CYAN;      break;
  }
  M5.Display.setTextColor(color, TFT_BLACK);
  M5.Display.setTextSize(3);
  M5.Display.drawString(label, 10, 8);

  M5.Display.setTextSize(2);
  M5.Display.setTextColor(TFT_LIGHTGREY, TFT_BLACK);
  M5.Display.drawString("You:", 10, 56);
  M5.Display.setTextColor(TFT_WHITE, TFT_BLACK);
  M5.Display.setCursor(10, 80);
  M5.Display.println(lastYou.substring(0, 120));

  M5.Display.setTextColor(TFT_LIGHTGREY, TFT_BLACK);
  M5.Display.drawString("Skippy:", 10, 140);
  M5.Display.setTextColor(TFT_CYAN, TFT_BLACK);
  M5.Display.setCursor(10, 164);
  M5.Display.println(lastSkippy.substring(0, 120));

  if (!wsConnected) {
    M5.Display.setTextColor(TFT_RED, TFT_BLACK);
    M5.Display.drawString("no server", 10, 220);
  }
  M5.Display.endWrite();
}

static void setState(State next) {
  if (state == next) return;
  state = next;
  draw();
}

// --- half-duplex switch ----------------------------------------------------

static void audioToMic() {
  M5.Speaker.end();
  M5.Mic.begin();
}

static void audioToSpeaker() {
  M5.Mic.end();
  M5.Speaker.begin();
  M5.Speaker.setVolume(200);
}

// --- websocket -------------------------------------------------------------

static void sendControl(const char* type) {
  JsonDocument doc;
  doc["type"] = type;
  if (strcmp(type, "start") == 0) doc["duplex"] = false;  // we cannot hear ourselves talk
  String out;
  serializeJson(doc, out);
  ws.sendTXT(out);
}

static void onText(uint8_t* payload, size_t length) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, length) != DeserializationError::Ok) return;
  const char* type = doc["type"];
  if (!type) return;

  if (strcmp(type, "state") == 0) {
    const char* s = doc["state"];
    if (!sessionOpen || !s) return;
    if (strcmp(s, "thinking") == 0)  setState(THINKING);
    // "listening"/"speaking" are driven locally by the audio bracket below,
    // because the I2S direction has to match what this side is actually doing.
  } else if (strcmp(type, "transcript") == 0) {
    lastYou = String((const char*)doc["text"]);
    draw();
  } else if (strcmp(type, "reply") == 0) {
    lastSkippy = String((const char*)doc["text"]);
    draw();
  } else if (strcmp(type, "audio_start") == 0) {
    playbackRate = doc["rate"] | SAMPLE_RATE;
    ringFlush();
    audioToSpeaker();
    setState(SPEAKING);
  } else if (strcmp(type, "audio_end") == 0 || strcmp(type, "audio_cancel") == 0) {
    if (strcmp(type, "audio_cancel") == 0) {
      ringFlush();
      M5.Speaker.stop();
    }
    // audio_end arrives when the server has *sent* everything, not when we
    // have played it; the loop drains the ring before flipping back to mic.
  }
}

static void onWsEvent(WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED:
      wsConnected = true;
      setState(sessionOpen ? LISTENING : IDLE);
      draw();
      break;
    case WStype_DISCONNECTED:
      wsConnected = false;
      sessionOpen = false;
      setState(BOOTING);
      break;
    case WStype_TEXT:
      onText(payload, length);
      break;
    case WStype_BIN:
      ringPush(payload, length);
      break;
    default:
      break;
  }
}

// --- setup / loop ----------------------------------------------------------

void setup() {
  auto cfg = M5.config();
  cfg.internal_mic = true;
  cfg.internal_spk = true;
  M5.begin(cfg);
  M5.Display.setRotation(1);
  M5.Touch.setHoldThresh(HOLD_TO_END_MS);

  ring = (uint8_t*)ps_malloc(RING_SIZE);

  draw();
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) delay(100);

  String path = "/ws/voice";
  if (strlen(SKIPPY_TOKEN) > 0) path += String("?token=") + SKIPPY_TOKEN;
  ws.begin(SKIPPY_HOST, SKIPPY_PORT, path);
  ws.onEvent(onWsEvent);
  ws.setReconnectInterval(2000);
  // Disconnects show up in seconds, not minutes, when the Mac goes away.
  ws.enableHeartbeat(5000, 3000, 2);
}

static void handleTouch() {
  auto touch = M5.Touch.getDetail();

  // Hold to end the session: the server summarizes the conversation into
  // project memory, so this is "save and hang up", not just stop.
  if (sessionOpen && touch.wasHold()) {
    sendControl("end");
    sessionOpen = false;
    M5.Mic.end();
    M5.Speaker.stop();
    ringFlush();
    lastYou = lastSkippy = "";
    setState(IDLE);
    return;
  }

  if (!touch.wasClicked()) return;

  if (!sessionOpen) {
    if (!wsConnected) return;
    sendControl("start");
    sessionOpen = true;
    audioToMic();
    setState(LISTENING);
  } else if (state == SPEAKING) {
    sendControl("interrupt");  // barge-in, the half-duplex way
    ringFlush();
    M5.Speaker.stop();
  }
}

void loop() {
  M5.update();
  ws.loop();
  handleTouch();

  if (!sessionOpen) {
    delay(5);
    return;
  }

  if (state == LISTENING || state == THINKING) {
    // Mic streaming continues while the server thinks: the user restating or
    // extending the question mid-think is normal conversation, and the server
    // handles it as a new utterance.
    if (M5.Mic.isEnabled() && M5.Mic.record(micBuf, MIC_CHUNK, SAMPLE_RATE)) {
      if (wsConnected) ws.sendBIN((uint8_t*)micBuf, MIC_CHUNK * sizeof(int16_t));
    }
  } else if (state == SPEAKING) {
    static int16_t playBuf[2048];
    if (ringUsed() >= sizeof(playBuf) || (ringUsed() > 0 && !M5.Speaker.isPlaying())) {
      size_t got = ringPop((uint8_t*)playBuf, sizeof(playBuf));
      if (got > 0) M5.Speaker.playRaw(playBuf, got / 2, playbackRate, false, 1, 0);
    }
    // Reply fully played: flip the shared I2S bus back to the microphone.
    if (ringUsed() == 0 && !M5.Speaker.isPlaying()) {
      audioToMic();
      setState(LISTENING);
    }
  }
}
