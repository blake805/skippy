// Skippy bench IO node for the M5Stack Core2 (IoT Dev Kit v2).
//
// A wireless pair of hands on the bench. The node reaches the hub's
// /ws/factory lane as client_id "devices:<name>", and from then on it only
// answers: the hub sends {"action": ..., "task_id": ...}, the node performs it
// on the wires and replies {"task_id": ..., "ok": true, "result": {...}} or
// {"task_id": ..., "ok": false, "error": "..."}. Binary payloads are hex
// strings, matching skippy_device's _encode_payload/_decode_payload. ADR 0020
// pins the shapes.
//
// Two transports carry that one protocol. On Wi-Fi the node holds its own
// WebSocket to the hub. Over BLE it advertises a Nordic-UART-style service and
// a laptop runs skippy_ble_bridge.py, which relays the same JSON lines to the
// hub on the node's behalf — that is what makes the node a carry-along probe
// for places with no bench network. BLE wins when both are available, and the
// node drops its own hub socket while a bridge holds the link: the hub keys
// connections by client_id, and one node must never be two clients.
//
// Two things this firmware deliberately does not do. It never starts anything
// of its own — the server enforces that too, by refusing to let a "devices*"
// client reach the task runner — and it never asks the human anything. Write
// approval happens on the machine that started the run (SkippyMac shows the
// device_auth card), so by the time an action arrives here it has already been
// approved.
//
// What the screen is for. Everything on it answers one of two questions a
// person standing at the bench actually has: is this thing working, and what
// has it been doing to my part? Hence the status block, the action log, and
// the LINK button — a physical, one-tap guarantee that nothing is going to
// drive a pin while your hands are in the circuit. The node reports the same
// state up to the hub as a "node_status" message, which is what lets the app
// list this node with its battery and signal.
//
// Grove pin map for the IoT Dev Kit v2:
//   Port A  G32 SDA / G33 SCL  I2C            (i2c_* tools)
//   Port B  G36 in / G26 out   ADC and GPIO   (adc_read, gpio_io)
//   Port C  G13 RX / G14 TX    UART, Serial2  (serial_* tools)
//
// Only those pins are reachable. The rest of the header runs the screen, the
// power management chip and the internal I2C bus, and an agent that could
// drive one of them could brick the node it is being trusted with.
//
// All Grove pins are 3.3V logic. A 5V or RS-232 part needs a level shifter or
// a transceiver in between; wiring one straight to a port damages the Core2.

#include <M5Unified.h>
#include <WiFi.h>
#include <Wire.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include <Adafruit_NeoPixel.h>
#include <NimBLEDevice.h>
#include "secrets.h"

#define NODE_FIRMWARE "io-node 1.3"

// Port C: the Grove UART.
static const int UART_RX = 13;
static const int UART_TX = 14;

// Port A: the external I2C bus. Bus 0 is the only one this node exposes; the
// internal bus (AXP192, touch, IMU) belongs to M5Unified and is not for rent.
static const int I2C_SDA = 32;
static const int I2C_SCL = 33;
static const uint32_t I2C_HZ = 100000;
// The Arduino I2C driver buffers a transaction whole, so this is the hardware's
// limit rather than a policy one. Asking for more comes back as an error: a
// short read that looked like a full one would be read as data about the part.
static const size_t MAX_I2C_BYTES = 128;

// Port B: one input and one output.
static const int PIN_ADC_IN = 36;   // input only on the ESP32
static const int PIN_GPIO_OUT = 26;

// Bounded exchanges, the ADR 0015 rule, enforced on this end too. The server
// caps at 16KB; the node has far less RAM to spare, so it clips lower and says
// so in the reply rather than dropping bytes silently.
static const size_t MAX_IO_BYTES = 4096;
static const uint32_t MAX_READ_MS = 30000;

// Scrollback, not just what fits: the screen shows seven rows and a finger
// drag reaches the rest, so "what did it do five minutes ago" survives.
static const size_t LOG_LINES = 40;
static const uint32_t STATUS_PERIOD_MS = 15000;
static const uint32_t DIM_AFTER_MS = 120000;
static const uint8_t BRIGHT_ACTIVE = 200;
static const uint8_t BRIGHT_IDLE = 24;

// --- state -----------------------------------------------------------------

enum Link { LINK_WIFI, LINK_CONNECTING, LINK_ONLINE, LINK_BLE, LINK_PAUSED };

static WebSocketsClient ws;
static bool wsConnected = false;
static bool wsStarted = false;
// Set from the LINK button: the human has taken the node off the air on
// purpose — both radios — so nothing should quietly reconnect it.
static bool paused = false;
static bool pendingLinkToggle = false;

// The BLE side. A central (skippy_ble_bridge.py on a laptop) connects, proves
// itself with the token, and from then on carries the same JSON lines the
// WebSocket would. Written from the NimBLE host task; the loop task only reads
// flags and drains the line queue, so no locking beyond the queue is needed.
static NimBLEServer* bleServer = nullptr;
static NimBLECharacteristic* bleTx = nullptr;
static volatile bool bleConnected = false;
static volatile bool bleAuthed = false;
static volatile uint16_t bleConnHandle = BLE_HS_CONN_HANDLE_NONE;
static uint32_t bleAuthDeadline = 0;
static String bleCentral = "";
static QueueHandle_t bleLineQueue = nullptr;

static bool wifiConfigured() { return strlen(WIFI_SSID) > 0; }

// One UART session at a time: there is one Port C. The handle is the server's,
// echoed back so both ends agree on which session is open.
static String uartHandle = "";
static uint32_t uartBaud = 0;
static uint32_t uartTimeoutMs = 2000;
static bool i2cStarted = false;

// Reentrancy guard. A long read pumps ws.loop() so the heartbeat keeps flowing,
// which means a second action can arrive mid-action; one set of wires cannot do
// two things at once, so the second is refused rather than interleaved.
static bool inAction = false;

static uint32_t actionCount = 0;
static uint32_t lastActivityMs = 0;
static bool dimmed = false;
static bool infoPage = false;

static String logLines[LOG_LINES];
static uint32_t logStamps[LOG_LINES];
static size_t logCount = 0;

// The frame is composited into a sprite and pushed whole, so "dirty" only has
// to mean "something changed"; the old per-region flags survive as a habit of
// the call sites, all now equivalent.
static const uint8_t R_HEADER = 1 << 0;
static const uint8_t R_STATUS = 1 << 1;
static const uint8_t R_LOG = 1 << 2;
static const uint8_t R_FOOTER = 1 << 3;
static const uint8_t R_ALL = 0x0f;
static uint8_t dirty = R_ALL;

// How far back the log view is scrolled, in rows from the live tail.
// 0 means pinned to the newest line, which is where it snaps back to on a tap
// of the log title. A finger drag anywhere in the log card moves it.
static int scrollRows = 0;

// --- small helpers ---------------------------------------------------------

static Link linkState() {
  if (paused) return LINK_PAUSED;
  if (bleAuthed) return LINK_BLE;
  if (wsConnected) return LINK_ONLINE;
  if (wifiConfigured() && WiFi.status() == WL_CONNECTED) return LINK_CONNECTING;
  // LINK_WIFI doubles as "waiting": no Wi-Fi where it is expected, or a
  // BLE-only build waiting for a bridge to find it.
  return LINK_WIFI;
}

static int batteryPercent() {
  int level = M5.Power.getBatteryLevel();
  if (level < 0) return -1;
  return level > 100 ? 100 : level;
}

static bool batteryCharging() {
  return M5.Power.isCharging() == m5::Power_Class::is_charging;
}

static String clockString(uint32_t ms) {
  uint32_t seconds = ms / 1000;
  char buf[12];
  snprintf(buf, sizeof(buf), "%02u:%02u", (unsigned)((seconds / 60) % 100),
           (unsigned)(seconds % 60));
  return String(buf);
}

static String uptimeString() {
  uint32_t seconds = millis() / 1000;
  char buf[16];
  if (seconds < 3600) {
    snprintf(buf, sizeof(buf), "%um", (unsigned)(seconds / 60));
  } else {
    snprintf(buf, sizeof(buf), "%uh%02um", (unsigned)(seconds / 3600),
             (unsigned)((seconds % 3600) / 60));
  }
  return String(buf);
}

static void wake() {
  lastActivityMs = millis();
  if (dimmed) {
    dimmed = false;
    M5.Display.setBrightness(BRIGHT_ACTIVE);
  }
}

// --- the light bar ---------------------------------------------------------
//
// The screen is only readable from a foot away, and a bench is a place where
// your eyes are on the part and your hands are in it. The base's LED bar says
// the one thing worth knowing from across the room: green is idle, cyan is
// reading, and a red flash means something just drove your circuit.

#if LED_BAR_COUNT > 0
static Adafruit_NeoPixel ledBar(LED_BAR_COUNT, LED_BAR_PIN, NEO_GRB + NEO_KHZ800);
#endif
static uint32_t ledHoldUntil = 0;

static void ledFill(uint8_t r, uint8_t g, uint8_t b) {
#if LED_BAR_COUNT > 0
  ledBar.fill(ledBar.Color(r, g, b));
  ledBar.show();
#else
  (void)r; (void)g; (void)b;
#endif
}

// The resting colour, derived from the link rather than remembered, so the bar
// can never disagree with what the screen says.
static void ledIdle() {
  ledHoldUntil = 0;
  switch (linkState()) {
    case LINK_PAUSED:     ledFill(0, 0, 40);  break;  // safe, and on purpose
    // Red is for a network that should be there and is not. A BLE-only node
    // waiting for its bridge is in its normal resting state: amber, not alarm.
    case LINK_WIFI:       wifiConfigured() ? ledFill(40, 0, 0) : ledFill(30, 18, 0); break;
    case LINK_CONNECTING: ledFill(30, 18, 0); break;
    case LINK_ONLINE:     ledFill(0, 26, 6);  break;
    case LINK_BLE:        ledFill(0, 26, 6);  break;
  }
}

static void ledFlash(uint8_t r, uint8_t g, uint8_t b, uint32_t ms) {
  ledFill(r, g, b);
  ledHoldUntil = millis() + ms;
}

// Writes get a chirp as well as a flash. A read is Skippy looking; a write is
// Skippy changing something on a bench you may have your hands in, and that
// deserves a sound whether or not you are looking at the node.
static void chirp() {
  M5.Speaker.tone(2200, 45);
}

// --- display ---------------------------------------------------------------
//
// One full-screen sprite, composited and pushed whole. The old per-region
// repaints existed to avoid flicker; a single blit avoids it better and lets
// the layout be one dark surface with cards on it instead of four rectangles
// taking turns. The sprite lives in PSRAM (the Core2 has 8MB), so the cost is
// one ~30ms push per change, and pushes only happen when something changed.

static M5Canvas canvas(&M5.Display);

// Black, white, and a handful of saturated signal colours. This panel is two
// inches across and read at arm's length: contrast is the whole game, and
// every grey that is not carrying information is a legibility tax. RGB565.
static const uint16_t COL_BG      = 0x0000;  // true black
static const uint16_t COL_TEXT    = 0xFFFF;  // primary text
static const uint16_t COL_DIM     = 0xA534;  // timestamps and labels only
static const uint16_t COL_LINE    = 0x2965;  // hairline dividers
static const uint16_t COL_BTN     = 0x10A2;  // button face
static const uint16_t COL_ACCENT  = 0x45FF;  // sky blue
static const uint16_t COL_GOOD    = 0x3EED;  // green
static const uint16_t COL_WARN    = 0xFDC6;  // amber
static const uint16_t COL_BAD     = 0xFAAA;  // red

// Layout. The action bar is drawn on the glass and hit-tested there — the
// whole panel is a touchscreen, not just the three dots below it (those still
// work, mapped to the same three actions).
struct Zone {
  int x, y, w, h;
  bool contains(int px, int py) const {
    return px >= x && px < x + w && py >= y && py < y + h;
  }
};
static const Zone CARD_LOG    = {0, 74, 320, 128};   // drag-to-scroll region
static const Zone STATS_STRIP = {0, 192, 320, 14};   // tap: back to live tail
static const Zone BTN_ZONE[3] = {{6, 206, 100, 34}, {110, 206, 100, 34}, {214, 206, 100, 34}};
static const int LOG_TEXT_TOP = 76;
static const int LOG_PITCH = 16;
static const int LOG_VISIBLE = 7;

static int maxScrollRows() {
  return logCount > LOG_VISIBLE ? (int)logCount - LOG_VISIBLE : 0;
}

static void drawBattery(int right, int cy) {
  int level = batteryPercent();
  bool charging = batteryCharging();
  uint16_t colour = charging ? COL_ACCENT
                             : (level >= 0 && level < 20 ? COL_BAD
                                : level < 50 ? COL_WARN : COL_GOOD);

  canvas.setFont(&fonts::DejaVu12);
  canvas.setTextDatum(middle_right);
  canvas.setTextColor(colour, COL_BG);
  String label = level < 0 ? String("--") : String(level) + "%";
  if (charging) label = String("+") + label;
  canvas.drawString(label, right - 32, cy);
  canvas.setTextDatum(top_left);

  // A drawn cell rather than a glyph: at a glance across the bench, the fill
  // level reads faster than the number does.
  canvas.drawRect(right - 27, cy - 6, 24, 12, colour);
  canvas.fillRect(right - 3, cy - 3, 2, 6, colour);
  int fill = level < 0 ? 0 : (level * 20) / 100;
  if (fill > 0) canvas.fillRect(right - 25, cy - 4, fill, 8, colour);
}

static void drawHeader() {
  canvas.setFont(&fonts::DejaVu12);
  canvas.setTextDatum(middle_left);
  canvas.setTextColor(COL_TEXT, COL_BG);
  canvas.drawString("SKIPPY", 10, 12);
  canvas.setTextColor(COL_DIM, COL_BG);
  canvas.drawString(String("devices:") + SKIPPY_NODE_NAME, 78, 12);
  drawBattery(314, 12);
  canvas.setTextDatum(top_left);
  canvas.drawFastHLine(0, 24, 320, COL_LINE);
}

static void drawStatus() {
  Link state = linkState();
  const char* label = "";
  uint16_t colour = COL_TEXT;
  switch (state) {
    case LINK_PAUSED:     label = "Paused";     colour = COL_WARN;   break;
    case LINK_WIFI:
      if (wifiConfigured()) { label = "No Wi-Fi";    colour = COL_BAD; }
      else                  { label = "Waiting BLE"; colour = COL_WARN; }
      break;
    case LINK_CONNECTING: label = "Connecting"; colour = COL_WARN;   break;
    case LINK_ONLINE:     label = "Connected";  colour = COL_GOOD;   break;
    case LINK_BLE:        label = "Connected";  colour = COL_GOOD;   break;
  }
  canvas.fillCircle(17, 41, 6, colour);

  canvas.setFont(&fonts::DejaVu24);
  canvas.setTextDatum(middle_left);
  canvas.setTextColor(colour, COL_BG);
  canvas.drawString(label, 32, 41);

  canvas.setFont(&fonts::DejaVu12);
  canvas.setTextColor(COL_DIM, COL_BG);
  String line;
  String right;
  if (state == LINK_BLE) {
    line = String("BLE  ") + bleCentral;
    right = "via bridge";
  } else if (WiFi.status() == WL_CONNECTED) {
    line = WiFi.localIP().toString() + "   " + String(WiFi.RSSI()) + " dBm";
    right = String(SKIPPY_HOST) + ":" + SKIPPY_PORT;
  } else if (wifiConfigured()) {
    line = String("waiting for ") + WIFI_SSID;
    right = String(SKIPPY_HOST) + ":" + SKIPPY_PORT;
  } else {
    line = String("advertising skippy-") + SKIPPY_NODE_NAME;
    right = "";
  }
  canvas.drawString(line, 32, 62);
  if (right.length()) {
    canvas.setTextDatum(middle_right);
    canvas.drawString(right, 314, 62);
  }
  canvas.setTextDatum(top_left);
  canvas.drawFastHLine(0, 72, 320, COL_LINE);
}

static void drawLog() {
  canvas.setFont(&fonts::DejaVu12);

  if (logCount == 0) {
    canvas.setTextColor(COL_DIM, COL_BG);
    canvas.drawString("no actions yet", 10, LOG_TEXT_TOP + 2);
  }

  int clampedScroll = scrollRows > maxScrollRows() ? maxScrollRows() : scrollRows;
  int last = (int)logCount - 1 - clampedScroll;          // newest row on screen
  int first = last - (LOG_VISIBLE - 1);
  if (first < 0) first = 0;

  for (int i = first; i <= last; i++) {
    int y = LOG_TEXT_TOP + (i - first) * LOG_PITCH;
    canvas.setTextColor(COL_DIM, COL_BG);
    canvas.drawString(clockString(logStamps[i]), 10, y);
    // Every message in white — history you scrolled to is history you are
    // reading. The live tail is the accent: "what just happened" in colour.
    bool isLiveTail = (i == (int)logCount - 1) && clampedScroll == 0;
    canvas.setTextColor(isLiveTail ? COL_ACCENT : COL_TEXT, COL_BG);
    canvas.drawString(logLines[i], 58, y);
  }

  // Scrollbar, only once there is something off-screen to point at.
  if (logCount > LOG_VISIBLE) {
    int trackX = 315;
    int trackY = LOG_TEXT_TOP;
    int trackH = LOG_VISIBLE * LOG_PITCH - 4;
    canvas.fillRect(trackX, trackY, 3, trackH, COL_LINE);
    int thumbH = trackH * LOG_VISIBLE / (int)logCount;
    if (thumbH < 10) thumbH = 10;
    // scroll 0 = pinned to the bottom of the track.
    int span = trackH - thumbH;
    int thumbY = trackY + span - (maxScrollRows() ? span * clampedScroll / maxScrollRows() : 0);
    canvas.fillRect(trackX, thumbY, 3, thumbH, COL_ACCENT);
  }

  // The stats strip. Tapping it snaps the view back to the live tail.
  canvas.drawFastHLine(0, 192, 320, COL_LINE);
  canvas.setFont(&fonts::DejaVu9);
  canvas.setTextDatum(middle_left);
  canvas.setTextColor(COL_DIM, COL_BG);
  String tail = String(actionCount) + " actions   up " + uptimeString() + "   " +
                (uartHandle.length() ? String("uart ") + uartBaud : String("uart closed"));
  canvas.drawString(tail, 10, 199);
  if (scrollRows > 0) {
    canvas.setTextDatum(middle_right);
    canvas.setTextColor(COL_ACCENT, COL_BG);
    canvas.drawString(String("-") + scrollRows + "  tap: live", 314, 199);
  }
  canvas.setTextDatum(top_left);
}

static void drawButton(int index, const char* label, uint16_t colour) {
  const Zone& z = BTN_ZONE[index];
  if (!label[0]) return;
  // Crisp: a dark face, a single-pixel outline, and a readable label.
  canvas.fillRoundRect(z.x, z.y + 4, z.w, z.h - 8, 6, COL_BTN);
  canvas.drawRoundRect(z.x, z.y + 4, z.w, z.h - 8, 6, colour);
  canvas.setFont(&fonts::DejaVu12);
  canvas.setTextDatum(middle_center);
  canvas.setTextColor(colour, COL_BTN);
  canvas.drawString(label, z.x + z.w / 2, z.y + z.h / 2);
  canvas.setTextDatum(top_left);
}

static void drawButtonBar() {
  if (infoPage) {
    drawButton(0, "Back", COL_TEXT);
    drawButton(2, "Hold: reboot", COL_BAD);
    return;
  }
  drawButton(0, paused ? "Connect" : "Pause", paused ? COL_GOOD : COL_WARN);
  drawButton(1, "Clear", COL_TEXT);
  drawButton(2, "Info", COL_ACCENT);
}

static void drawInfoPage() {
  canvas.setFont(&fonts::DejaVu12);

  struct Row { const char* key; String value; };
  const Row rows[] = {
    {"firmware", NODE_FIRMWARE},
    {"client id", String("devices:") + SKIPPY_NODE_NAME},
    {"hub", String(SKIPPY_HOST) + ":" + SKIPPY_PORT +
                (strlen(SKIPPY_TOKEN) ? "  (token set)" : "  (no token)")},
    {"wi-fi", wifiConfigured()
                  ? String(WIFI_SSID) + "  " + String(WiFi.RSSI()) + " dBm  " +
                        WiFi.localIP().toString()
                  : String("not configured (BLE only)")},
    {"bluetooth", bleAuthed ? String("bridge ") + bleCentral
                            : (paused ? String("paused")
                                      : String("advertising skippy-") + SKIPPY_NODE_NAME)},
    {"mac", WiFi.macAddress()},
    {"battery", (batteryPercent() < 0 ? String("--") : String(batteryPercent())) + "%  " +
                    String(M5.Power.getBatteryVoltage() / 1000.0f, 2) + " V" +
                    (batteryCharging() ? "  charging" : "")},
    {"uptime", uptimeString() + "   " + String(actionCount) + " actions"},
    {"free heap", String(ESP.getFreeHeap() / 1024) + " KB"},
    {"port A", "G32 SDA / G33 SCL   i2c"},
    {"port B", "G36 adc in / G26 gpio out"},
    {"port C", "G13 RX / G14 TX   uart"},
  };

  int y = 32;
  for (const Row& row : rows) {
    canvas.setTextColor(COL_DIM, COL_BG);
    canvas.drawString(row.key, 10, y);
    canvas.setTextColor(COL_TEXT, COL_BG);
    canvas.drawString(row.value, 96, y);
    y += 13;
  }
  canvas.setTextColor(COL_WARN, COL_BG);
  canvas.drawString("3.3V logic only - shift 5V and RS-232", 10, y + 2);
}

static void render() {
  if (!dirty) return;
  canvas.fillSprite(COL_BG);
  drawHeader();
  if (infoPage) {
    drawInfoPage();
  } else {
    drawStatus();
    drawLog();
  }
  drawButtonBar();
  canvas.pushSprite(0, 0);
  dirty = 0;
}

// Every action Skippy runs on the wires lands here. Someone standing at the
// bench can see what the agent is doing to the part in front of them, and
// anything that changed the circuit says so in red and out loud.
static void logAction(const String& line, bool wrote = false) {
  if (logCount < LOG_LINES) {
    logLines[logCount] = line;
    logStamps[logCount] = millis();
    logCount++;
  } else {
    for (size_t i = 1; i < LOG_LINES; i++) {
      logLines[i - 1] = logLines[i];
      logStamps[i - 1] = logStamps[i];
    }
    logLines[LOG_LINES - 1] = line;
    logStamps[LOG_LINES - 1] = millis();
  }
  actionCount++;
  // A reader partway up the history keeps the lines they are looking at; a
  // reader at the tail stays at the tail.
  if (scrollRows > 0 && scrollRows < maxScrollRows()) scrollRows++;
  dirty |= R_LOG;
  wake();
  if (wrote) {
    ledFlash(60, 0, 0, 700);
    chirp();
  }
}

// --- hex ------------------------------------------------------------------

static int hexNibble(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

// Returns the byte count, or -1 if the string is not clean hex.
static int hexDecode(const char* hex, uint8_t* out, size_t maxLen) {
  if (!hex) return 0;
  size_t len = strlen(hex);
  if (len % 2) return -1;
  size_t n = len / 2;
  if (n > maxLen) return -1;
  for (size_t i = 0; i < n; i++) {
    int hi = hexNibble(hex[2 * i]), lo = hexNibble(hex[2 * i + 1]);
    if (hi < 0 || lo < 0) return -1;
    out[i] = (uint8_t)((hi << 4) | lo);
  }
  return (int)n;
}

static String hexEncode(const uint8_t* data, size_t len) {
  static const char* digits = "0123456789abcdef";
  String out;
  out.reserve(len * 2);
  for (size_t i = 0; i < len; i++) {
    out += digits[data[i] >> 4];
    out += digits[data[i] & 0x0f];
  }
  return out;
}

// --- replies ---------------------------------------------------------------

// The transport seam. Everything above the wire protocol calls sendJson and
// never learns which radio carried the line. BLE wins while a bridge holds an
// authenticated link; otherwise the node's own WebSocket, if it has one.
static void bleSendLine(const String& line);

static void sendLine(String& out) {
  if (bleAuthed) {
    bleSendLine(out);
    return;
  }
  if (wsConnected) ws.sendTXT(out);
}

static void sendJson(JsonDocument& doc) {
  String out;
  serializeJson(doc, out);
  sendLine(out);
}

static void replyError(const char* taskId, const String& message) {
  JsonDocument doc;
  doc["task_id"] = taskId ? taskId : "";
  doc["ok"] = false;
  doc["error"] = message;
  sendJson(doc);
}

// --- telemetry -------------------------------------------------------------

// What the hub (and through it the app) knows about this node. Presence alone
// is not enough to trust a bench node from another room: a node on 4% battery
// or -89 dBm is one whose next reading you should not believe.
static void sendNodeStatus() {
  if (!wsConnected && !bleAuthed) return;
  JsonDocument doc;
  doc["type"] = "node_status";
  doc["node"] = SKIPPY_NODE_NAME;
  doc["firmware"] = NODE_FIRMWARE;
  doc["battery"] = batteryPercent();
  doc["charging"] = batteryCharging();
  doc["transport"] = bleAuthed ? "ble" : "wifi";
  // Signal and address describe the Wi-Fi link; on BLE they would be noise
  // (or garbage, on a node with the radio never joined). Absent fields are
  // the contract's way of saying "not applicable" — the app parses defensively.
  if (WiFi.status() == WL_CONNECTED) {
    doc["rssi"] = WiFi.RSSI();
    doc["ip"] = WiFi.localIP().toString();
  }
  doc["uptime_s"] = millis() / 1000;
  doc["actions"] = actionCount;
  doc["busy"] = inAction;
  doc["uart_open"] = uartHandle.length() > 0;
  JsonArray ports = doc["ports"].to<JsonArray>();
  ports.add("uart");
  ports.add("i2c");
  ports.add("gpio");
  ports.add("adc");
  sendJson(doc);
}

static void sendHello() {
  JsonDocument doc;
  doc["type"] = "hello";
  doc["role"] = "devices";
  doc["node"] = SKIPPY_NODE_NAME;
  doc["firmware"] = NODE_FIRMWARE;
  sendJson(doc);
}

// --- the BLE link ------------------------------------------------------------
//
// A Nordic-UART-style service: the bridge writes JSON lines into RX, the node
// notifies JSON lines out of TX, both chunked to the negotiated MTU and framed
// on '\n'. BLE is open air with no LAN to hide behind, so the first line a
// central sends must be {"type": "hello", "token": ...} matching SKIPPY_TOKEN;
// anything else — or ten seconds of silence — gets disconnected.
//
// NimBLE callbacks run on the BLE host task, and an action can block the main
// loop for a 30-second capture. Lines therefore go into a queue and are only
// acted on from loop(): the radio never waits on the wires.

static const char* BLE_SERVICE_UUID = "b7f80001-9a3c-4f4e-8a52-6e0d7c3b2a19";
static const char* BLE_RX_UUID      = "b7f80002-9a3c-4f4e-8a52-6e0d7c3b2a19";
static const char* BLE_TX_UUID      = "b7f80003-9a3c-4f4e-8a52-6e0d7c3b2a19";
static const uint32_t BLE_AUTH_GRACE_MS = 10000;
// A full serial capture reply is ~8.5KB of JSON; anything past this without a
// newline is not our protocol.
static const size_t BLE_MAX_LINE = 12288;
static const int BLE_QUEUE_DEPTH = 6;

// Assembled on the host task only; cleared there on disconnect.
static String bleRxAssembly = "";

static void bleSendLine(const String& line) {
  if (!bleAuthed || !bleTx || !bleServer || bleConnHandle == BLE_HS_CONN_HANDLE_NONE) return;
  uint16_t mtu = bleServer->getPeerMTU(bleConnHandle);
  size_t chunk = mtu > 23 ? (size_t)(mtu - 3) : 20;
  String framed = line + "\n";
  const uint8_t* data = (const uint8_t*)framed.c_str();
  size_t len = framed.length();
  for (size_t off = 0; off < len; off += chunk) {
    size_t n = len - off < chunk ? len - off : chunk;
    bleTx->setValue(data + off, n);
    bleTx->notify();
    // Pacing, not politeness: notify() queues into a finite mbuf pool and a
    // 35-chunk reply sent flat out overruns it silently.
    delay(3);
  }
}

class BleServerCallbacks : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer* server, ble_gap_conn_desc* desc) override {
    bleConnHandle = desc->conn_handle;
    bleConnected = true;
    bleAuthed = false;
    bleAuthDeadline = millis() + BLE_AUTH_GRACE_MS;
    bleCentral = String(NimBLEAddress(desc->peer_ota_addr).toString().c_str());
    dirty |= R_STATUS;
  }
  void onDisconnect(NimBLEServer* server) override {
    bleConnected = false;
    bleAuthed = false;
    bleConnHandle = BLE_HS_CONN_HANDLE_NONE;
    bleRxAssembly = "";
    dirty |= R_STATUS;
  }
};

class BleRxCallbacks : public NimBLECharacteristicCallbacks {
  void onWrite(NimBLECharacteristic* characteristic) override {
    std::string value = characteristic->getValue();
    for (char c : value) {
      if (c == '\n') {
        if (bleRxAssembly.length() && bleLineQueue) {
          String* line = new String(bleRxAssembly);
          if (xQueueSend(bleLineQueue, &line, 0) != pdTRUE) delete line;
        }
        bleRxAssembly = "";
      } else {
        bleRxAssembly += c;
        if (bleRxAssembly.length() > BLE_MAX_LINE) bleRxAssembly = "";
      }
    }
  }
};

static void bleDisconnectCentral() {
  if (bleServer && bleConnHandle != BLE_HS_CONN_HANDLE_NONE) {
    bleServer->disconnect(bleConnHandle);
  }
}

static void startBle() {
  bleLineQueue = xQueueCreate(BLE_QUEUE_DEPTH, sizeof(String*));
  NimBLEDevice::init((String("skippy-") + SKIPPY_NODE_NAME).c_str());
  NimBLEDevice::setMTU(247);
  bleServer = NimBLEDevice::createServer();
  bleServer->setCallbacks(new BleServerCallbacks());
  // Advertising is managed from loop() against `paused`; the library
  // re-advertising on its own would undo a pause the moment it took effect.
  bleServer->advertiseOnDisconnect(false);

  NimBLEService* service = bleServer->createService(BLE_SERVICE_UUID);
  NimBLECharacteristic* rx = service->createCharacteristic(
      BLE_RX_UUID, NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::WRITE_NR);
  rx->setCallbacks(new BleRxCallbacks());
  bleTx = service->createCharacteristic(BLE_TX_UUID, NIMBLE_PROPERTY::NOTIFY);
  service->start();

  NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
  adv->addServiceUUID(BLE_SERVICE_UUID);
  adv->setScanResponse(true);
}

// Forward: lines arrive here from loop(), never from the radio's own task.
static void onText(uint8_t* payload, size_t length);

static void bleHandleLine(const String& line) {
  if (!bleAuthed) {
    JsonDocument doc;
    bool ok = deserializeJson(doc, line) == DeserializationError::Ok &&
              strcmp(doc["type"] | "", "hello") == 0 &&
              String(doc["token"] | "") == SKIPPY_TOKEN;
    if (!ok) {
      logAction("ble auth refused");
      bleDisconnectCentral();
      return;
    }
    bleAuthed = true;
    logAction("ble link up");
    // The same greeting the hub would get from the node's own socket; the
    // bridge forwards it, so the hub cannot tell the transports apart.
    sendHello();
    sendNodeStatus();
    dirty |= R_STATUS;
    ledIdle();
    return;
  }
  onText((uint8_t*)line.c_str(), line.length());
}

// --- waiting ---------------------------------------------------------------

// Sleep while keeping the websocket alive. A 30 s capture that blocked would
// miss the heartbeat and the hub would drop us mid-read.
static void pump(uint32_t ms) {
  uint32_t until = millis() + ms;
  while ((int32_t)(until - millis()) > 0) {
    ws.loop();
    M5.update();
    // The LINK button stays live during a long capture — that is most of its
    // point — but it is applied by the main loop once the action has finished
    // rather than tearing the socket down underneath it.
    if (M5.BtnA.wasClicked()) pendingLinkToggle = true;
    delay(1);
  }
}

// --- serial ----------------------------------------------------------------

// Common framings only. Anything else falls back to 8N1 and says so in the
// reply, which beats guessing at a config constant and silently mangling every
// byte on the line.
static uint32_t uartConfig(int bits, const String& parity, float stop, bool* exact) {
  *exact = true;
  char p = parity.length() ? toupper(parity[0]) : 'N';
  int s = (stop >= 2.0f) ? 2 : 1;
  if (bits == 8 && p == 'N' && s == 1) return SERIAL_8N1;
  if (bits == 8 && p == 'N' && s == 2) return SERIAL_8N2;
  if (bits == 8 && p == 'E' && s == 1) return SERIAL_8E1;
  if (bits == 8 && p == 'O' && s == 1) return SERIAL_8O1;
  if (bits == 7 && p == 'N' && s == 1) return SERIAL_7N1;
  if (bits == 7 && p == 'E' && s == 1) return SERIAL_7E1;
  if (bits == 7 && p == 'O' && s == 1) return SERIAL_7O1;
  *exact = false;
  return SERIAL_8N1;
}

static void actionSerialOpen(const JsonDocument& req, const char* taskId) {
  uint32_t baud = req["baud"] | 115200;
  int bits = req["bytesize"] | 8;
  float stop = req["stopbits"] | 1.0f;
  String parity = req["parity"] | "N";
  String handle = req["handle"] | "";

  if (uartHandle.length()) Serial2.end();

  bool exact = true;
  uint32_t config = uartConfig(bits, parity, stop, &exact);
  Serial2.begin(baud, config, UART_RX, UART_TX);
  uartHandle = handle;
  uartBaud = baud;
  // The per-read timeout the tool call carried, so a part that never answers
  // costs one bounded wait rather than the whole agent step.
  float timeout = req["timeout"] | 2.0f;
  uartTimeoutMs = (uint32_t)(timeout * 1000.0f);
  if (uartTimeoutMs < 50) uartTimeoutMs = 50;
  if (uartTimeoutMs > MAX_READ_MS) uartTimeoutMs = MAX_READ_MS;
  logAction(String("uart open ") + baud + " " + bits + parity + (int)stop);

  JsonDocument doc;
  doc["task_id"] = taskId;
  doc["ok"] = true;
  JsonObject result = doc["result"].to<JsonObject>();
  result["handle"] = handle;
  result["port"] = "port-c";
  result["baud"] = baud;
  if (!exact) {
    result["note"] = "framing not supported on this node; opened 8N1";
  }
  sendJson(doc);
}

static void actionSerialIo(const JsonDocument& req, const char* taskId) {
  String handle = req["handle"] | "";
  if (!uartHandle.length() || handle != uartHandle) {
    replyError(taskId, "No UART session with that handle; call serial_open first.");
    return;
  }

  const char* writeHex = req["write_hex"] | "";
  static uint8_t buffer[MAX_IO_BYTES];
  int written = 0;
  if (strlen(writeHex)) {
    int n = hexDecode(writeHex, buffer, MAX_IO_BYTES);
    if (n < 0) {
      replyError(taskId, "write_hex is not clean hex, or is larger than 4096 bytes.");
      return;
    }
    // Approval already happened on the run's own client; by here the human has
    // said yes and this is just the wire.
    Serial2.write(buffer, (size_t)n);
    Serial2.flush();
    written = n;
  }

  size_t readBytes = req["read_bytes"] | 0;
  float idleSeconds = req["read_until_idle"] | 0.0f;
  float captureSeconds = req["capture_seconds"] | 0.0f;
  if (readBytes > MAX_IO_BYTES) readBytes = MAX_IO_BYTES;

  size_t got = 0;
  bool truncated = false;
  uint32_t idleMs = (uint32_t)(idleSeconds * 1000.0f);
  uint32_t captureMs = (uint32_t)(captureSeconds * 1000.0f);
  if (captureMs > MAX_READ_MS) captureMs = MAX_READ_MS;
  if (idleMs > MAX_READ_MS) idleMs = MAX_READ_MS;

  if (captureMs > 0) {
    uint32_t until = millis() + captureMs;
    while ((int32_t)(until - millis()) > 0 && got < MAX_IO_BYTES) {
      while (Serial2.available() && got < MAX_IO_BYTES) buffer[got++] = (uint8_t)Serial2.read();
      pump(5);
    }
    truncated = got >= MAX_IO_BYTES;
  } else if (readBytes > 0) {
    // Bounded by the read timeout the open carried, not by the part answering:
    // a device that says nothing must not pin the socket forever.
    uint32_t until = millis() + uartTimeoutMs;
    while (got < readBytes && (int32_t)(until - millis()) > 0) {
      while (Serial2.available() && got < readBytes) buffer[got++] = (uint8_t)Serial2.read();
      pump(5);
    }
  } else if (idleMs > 0) {
    uint32_t until = millis() + idleMs;
    while ((int32_t)(until - millis()) > 0 && got < MAX_IO_BYTES) {
      bool sawByte = false;
      while (Serial2.available() && got < MAX_IO_BYTES) {
        buffer[got++] = (uint8_t)Serial2.read();
        sawByte = true;
      }
      if (sawByte) {
        until = millis() + idleMs;  // the idle window restarts on activity
      } else if (got > 0) {
        break;
      }
      pump(5);
    }
    truncated = got >= MAX_IO_BYTES;
  }

  logAction(String("uart w") + written + " r" + (int)got, written > 0);

  JsonDocument doc;
  doc["task_id"] = taskId;
  doc["ok"] = true;
  JsonObject result = doc["result"].to<JsonObject>();
  result["data_hex"] = hexEncode(buffer, got);
  result["wrote"] = written;
  result["truncated"] = truncated;
  sendJson(doc);
}

static void actionSerialClose(const JsonDocument& req, const char* taskId) {
  String handle = req["handle"] | "";
  if (uartHandle.length() && handle == uartHandle) {
    Serial2.end();
    uartHandle = "";
    uartBaud = 0;
    logAction("uart close");
  }

  JsonDocument doc;
  doc["task_id"] = taskId;
  doc["ok"] = true;
  JsonObject result = doc["result"].to<JsonObject>();
  result["handle"] = handle;
  sendJson(doc);
}

// --- i2c -------------------------------------------------------------------

static bool ensureI2c(int bus, const char* taskId) {
  if (bus != 0) {
    replyError(taskId, "Only bus 0 (Grove Port A, G32/G33) is wired on this node.");
    return false;
  }
  if (!i2cStarted) {
    Wire.begin(I2C_SDA, I2C_SCL, I2C_HZ);
    i2cStarted = true;
  }
  return true;
}

static void actionI2cScan(const JsonDocument& req, const char* taskId) {
  int bus = req["bus"] | 0;
  if (!ensureI2c(bus, taskId)) return;

  JsonDocument doc;
  doc["task_id"] = taskId;
  doc["ok"] = true;
  JsonObject result = doc["result"].to<JsonObject>();
  JsonArray addresses = result["addresses"].to<JsonArray>();

  int found = 0;
  char hex[7];
  // 0x00 is the general-call address and 0x78+ is reserved; probing the
  // reserved ranges only produces noise.
  for (uint8_t addr = 0x01; addr <= 0x77; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      snprintf(hex, sizeof(hex), "0x%02x", addr);
      // String, not the char buffer: ArduinoJson stores a const char* by
      // reference, and this one is about to be overwritten by the next probe.
      addresses.add(String(hex));
      found++;
    }
  }
  logAction(String("i2c scan -> ") + found);
  sendJson(doc);
}

// endTransmission's codes, in words. "Nothing acked" and "the bus is stuck
// low" call for completely different next moves, and the model can only pick
// one if the reply says which happened.
static const char* i2cErrorText(uint8_t code) {
  switch (code) {
    case 1: return "the transaction is longer than the bus buffer";
    case 2: return "no device acknowledged that address";
    case 3: return "the device NACKed a data byte";
    case 5: return "the bus timed out (check pull-ups and wiring)";
    default: return "the bus reported an error";
  }
}

static void actionI2cIo(const JsonDocument& req, const char* taskId) {
  int bus = req["bus"] | 0;
  if (!ensureI2c(bus, taskId)) return;

  long addr = strtol(req["addr"] | "0x00", nullptr, 0);
  if (addr < 0x01 || addr > 0x7f) {
    replyError(taskId, "addr must be a 7-bit I2C address.");
    return;
  }
  bool hasRegister = !req["register"].isNull();
  int reg = req["register"] | 0;
  size_t readLen = req["read_len"] | 0;
  if (readLen > MAX_I2C_BYTES) {
    replyError(taskId, String("This node reads at most ") + MAX_I2C_BYTES +
                           " bytes per I2C transaction; read it in pages.");
    return;
  }

  static uint8_t buffer[MAX_I2C_BYTES];
  const char* writeHex = req["write_hex"] | "";
  int written = 0;
  if (strlen(writeHex)) {
    written = hexDecode(writeHex, buffer, MAX_I2C_BYTES);
    if (written < 0) {
      replyError(taskId, String("write_hex is not clean hex, or is over ") +
                             MAX_I2C_BYTES + " bytes.");
      return;
    }
  }

  if (hasRegister || written > 0) {
    Wire.beginTransmission((uint8_t)addr);
    if (hasRegister) Wire.write((uint8_t)reg);
    if (written > 0) Wire.write(buffer, (size_t)written);
    // A repeated start when a read follows: releasing the bus in between lets
    // another master move the register pointer under us.
    uint8_t err = Wire.endTransmission(readLen == 0);
    if (err != 0) {
      replyError(taskId, String("I2C write failed: ") + i2cErrorText(err));
      return;
    }
  }

  size_t got = 0;
  if (readLen > 0) {
    // Both arguments cast to an exact overload: the mixed int/size_t forms of
    // requestFrom are ambiguous, and readLen is a byte by the check above.
    size_t available = Wire.requestFrom((uint16_t)addr, (uint8_t)readLen);
    while (Wire.available() && got < readLen) buffer[got++] = (uint8_t)Wire.read();
    if (available == 0 && got == 0) {
      replyError(taskId, "I2C read failed: no device acknowledged that address.");
      return;
    }
  }

  logAction(String("i2c ") + (req["addr"] | "?") + " w" + written + " r" + (int)got,
            written > 0);

  JsonDocument doc;
  doc["task_id"] = taskId;
  doc["ok"] = true;
  JsonObject result = doc["result"].to<JsonObject>();
  result["data_hex"] = hexEncode(buffer, got);
  result["wrote"] = written;
  sendJson(doc);
}

// --- pins ------------------------------------------------------------------

static void actionGpio(const JsonDocument& req, const char* taskId) {
  int pin = req["pin"] | -1;
  String direction = req["direction"] | "read";
  String pull = req["pull"] | "none";

  if (direction == "write") {
    if (pin != PIN_GPIO_OUT) {
      replyError(taskId, String("Only G") + PIN_GPIO_OUT +
                             " (Grove Port B out) can be driven on this node.");
      return;
    }
    if (req["value"].isNull()) {
      replyError(taskId, "direction='write' needs value 0 or 1.");
      return;
    }
    int level = (req["value"] | 0) ? HIGH : LOW;
    pinMode(pin, OUTPUT);
    digitalWrite(pin, level);
    logAction(String("gpio ") + pin + " <- " + (level == HIGH ? 1 : 0), true);

    JsonDocument doc;
    doc["task_id"] = taskId;
    doc["ok"] = true;
    JsonObject result = doc["result"].to<JsonObject>();
    result["pin"] = pin;
    result["value"] = (level == HIGH) ? 1 : 0;
    sendJson(doc);
    return;
  }

  if (pin != PIN_GPIO_OUT && pin != PIN_ADC_IN) {
    replyError(taskId, String("Only G") + PIN_GPIO_OUT + " and G" + PIN_ADC_IN +
                           " (Grove Port B) are readable on this node.");
    return;
  }
  // G36 is input-only and has no internal pull resistors, so a pull request
  // there is refused rather than silently ignored.
  if (pull != "none" && pin == PIN_ADC_IN) {
    replyError(taskId, String("G") + PIN_ADC_IN + " has no internal pull resistors.");
    return;
  }
  if (pull == "up") {
    pinMode(pin, INPUT_PULLUP);
  } else if (pull == "down") {
    pinMode(pin, INPUT_PULLDOWN);
  } else {
    pinMode(pin, INPUT);
  }
  int level = digitalRead(pin);
  logAction(String("gpio ") + pin + " -> " + level);

  JsonDocument doc;
  doc["task_id"] = taskId;
  doc["ok"] = true;
  JsonObject result = doc["result"].to<JsonObject>();
  result["pin"] = pin;
  result["value"] = level;
  sendJson(doc);
}

static void actionAdc(const JsonDocument& req, const char* taskId) {
  int pin = req["pin"] | -1;
  if (pin != PIN_ADC_IN) {
    replyError(taskId, String("Only G") + PIN_ADC_IN +
                           " (Grove Port B in) is an analog input on this node.");
    return;
  }
  int samples = req["samples"] | 1;
  if (samples < 1) samples = 1;
  if (samples > 64) samples = 64;

  uint32_t rawTotal = 0, mvTotal = 0;
  for (int i = 0; i < samples; i++) {
    rawTotal += analogRead(pin);
    // The calibrated reading: the ESP32's ADC is neither linear nor
    // consistent between chips, so the raw count alone is not a voltage.
    mvTotal += analogReadMilliVolts(pin);
    delay(1);
  }
  int raw = (int)(rawTotal / samples);
  int mv = (int)(mvTotal / samples);
  logAction(String("adc ") + pin + " -> " + mv + "mV");

  JsonDocument doc;
  doc["task_id"] = taskId;
  doc["ok"] = true;
  JsonObject result = doc["result"].to<JsonObject>();
  result["pin"] = pin;
  result["raw"] = raw;
  result["mv"] = mv;
  result["samples"] = samples;
  sendJson(doc);
}

static void actionList(const JsonDocument& req, const char* taskId) {
  logAction("device_list");

  JsonDocument doc;
  doc["task_id"] = taskId;
  doc["ok"] = true;
  JsonObject result = doc["result"].to<JsonObject>();
  JsonArray devices = result["devices"].to<JsonArray>();
  // The node's ports are fixed hardware, so this is a description of the board
  // rather than an enumeration. Naming them is still what makes serial_open's
  // "only talk to something list_devices named" rule work here.
  JsonObject uart = devices.add<JsonObject>();
  uart["kind"] = "serial";
  uart["host"] = SKIPPY_NODE_NAME;
  uart["port"] = "port-c";
  uart["description"] = "Grove Port C UART (G13 RX / G14 TX), 3.3V logic";
  uart["vid"] = "";
  uart["pid"] = "";
  sendJson(doc);
}

// --- dispatch --------------------------------------------------------------

static void onAction(const JsonDocument& req, const char* action, const char* taskId) {
  if (strcmp(action, "device_list") == 0) {
    actionList(req, taskId);
  } else if (strcmp(action, "device_serial_open") == 0) {
    actionSerialOpen(req, taskId);
  } else if (strcmp(action, "device_serial_io") == 0) {
    actionSerialIo(req, taskId);
  } else if (strcmp(action, "device_serial_close") == 0) {
    actionSerialClose(req, taskId);
  } else if (strcmp(action, "device_i2c_scan") == 0) {
    actionI2cScan(req, taskId);
  } else if (strcmp(action, "device_i2c_io") == 0) {
    actionI2cIo(req, taskId);
  } else if (strcmp(action, "device_gpio") == 0) {
    actionGpio(req, taskId);
  } else if (strcmp(action, "device_adc") == 0) {
    actionAdc(req, taskId);
  } else if (strncmp(action, "device_usb", 10) == 0) {
    // The Core2's USB port is the programming UART, not a host controller.
    replyError(taskId, "USB is unsupported on this node; it has no host controller.");
  } else {
    replyError(taskId, String("Unsupported on this node: ") + action);
  }
}

static void onText(uint8_t* payload, size_t length) {
  JsonDocument req;
  if (deserializeJson(req, payload, length) != DeserializationError::Ok) return;

  const char* action = req["action"];
  const char* taskId = req["task_id"];
  if (!action) return;  // status pushes and the like: nothing here acts on them
  if (!taskId) {
    // Without a task_id the hub has nothing to resolve, so a reply would be
    // dropped and a run would hang on a future nobody completes.
    return;
  }
  if (inAction) {
    replyError(taskId, "Node is busy with another action; retry.");
    return;
  }

  inAction = true;
  ledFill(0, 30, 40);  // cyan for the whole action, however long it runs
  onAction(req, action, taskId);
  inAction = false;
  // A write flash outlives the action it belongs to; anything else goes
  // straight back to the resting colour.
  if (ledHoldUntil == 0) ledIdle();
}

static void onWsEvent(WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED:
      wsConnected = true;
      sendHello();
      sendNodeStatus();
      dirty |= R_STATUS;
      ledIdle();
      break;
    case WStype_DISCONNECTED:
      wsConnected = false;
      dirty |= R_STATUS;
      ledIdle();
      break;
    case WStype_TEXT:
      onText(payload, length);
      break;
    default:
      break;
  }
}

// --- link control ----------------------------------------------------------

static void startLink() {
  String path = String("/ws/factory?client_id=devices:") + SKIPPY_NODE_NAME;
  if (strlen(SKIPPY_TOKEN) > 0) path += String("&token=") + SKIPPY_TOKEN;
  ws.begin(SKIPPY_HOST, SKIPPY_PORT, path);
  ws.onEvent(onWsEvent);
  ws.setReconnectInterval(2000);
  // Generous enough that a long capture pumping this loop still answers in
  // time, tight enough that a hub going away shows on screen in seconds.
  ws.enableHeartbeat(15000, 8000, 2);
  wsStarted = true;
}

// The one control on the device, and the reason it exists: with the link down,
// no agent anywhere can drive a pin or push a byte into whatever is clipped to
// the ports. That is a guarantee you can make with a fingertip before putting
// your hands in the circuit, and see on the screen while they are in there.
static void toggleLink() {
  paused = !paused;
  if (paused) {
    ws.disconnect();
    wsConnected = false;
    wsStarted = false;
    // Both radios, or the guarantee is not one. Advertising stops so a bridge
    // cannot quietly re-establish what a fingertip just tore down.
    NimBLEDevice::getAdvertising()->stop();
    bleDisconnectCentral();
    logAction("link paused by hand");
  } else {
    logAction("link resumed");
    if (WiFi.status() == WL_CONNECTED) startLink();
    // Advertising resumes from loop(), which owns that decision.
  }
  dirty |= R_STATUS | R_FOOTER;
  ledIdle();
}

static void rebootWithNotice() {
  M5.Display.fillScreen(TFT_BLACK);
  M5.Display.setTextDatum(middle_center);
  M5.Display.setTextColor(TFT_RED, TFT_BLACK);
  M5.Display.drawString("rebooting", 160, 120);
  M5.Display.setTextDatum(top_left);
  delay(400);
  ESP.restart();
}

// The three actions, shared by the on-glass buttons and the dots below the
// display. Index matches both left to right.
static void buttonAction(int index, bool held) {
  if (infoPage) {
    if (index == 0 && !held) {
      infoPage = false;
      dirty = R_ALL;
    }
    if (index == 2 && held) rebootWithNotice();
    return;
  }
  if (held) return;
  switch (index) {
    case 0: toggleLink(); break;
    case 1:
      logCount = 0;
      actionCount = 0;
      scrollRows = 0;
      dirty |= R_LOG;
      break;
    case 2:
      infoPage = true;
      dirty = R_ALL;
      break;
  }
}

// The capacitive dots below the glass, kept for muscle memory. They map to
// the same three actions as the buttons drawn above them.
static void handleButtons() {
  bool touched = M5.BtnA.wasClicked() || M5.BtnB.wasClicked() ||
                 M5.BtnC.wasClicked() || M5.BtnC.wasHold();
  if (!touched) return;

  // The first tap on a dimmed screen only wakes it. Reaching for a dark panel
  // and disconnecting the bench by accident is exactly the wrong surprise.
  if (dimmed) {
    wake();
    return;
  }
  wake();

  if (M5.BtnC.wasHold()) {
    buttonAction(2, true);
  } else if (M5.BtnA.wasClicked()) {
    buttonAction(0, false);
  } else if (M5.BtnB.wasClicked()) {
    buttonAction(1, false);
  } else if (M5.BtnC.wasClicked()) {
    buttonAction(2, false);
  }
}

// The glass itself. The whole panel is a touchscreen: the action bar is
// hit-tested where it is drawn, a drag in the activity area scrolls the
// history, and a tap on the stats strip snaps back to the live tail.
static int dragStartY = -1;
static int dragStartRows = 0;
static bool touchOnlyWoke = false;

static void handleTouch() {
  auto t = M5.Touch.getDetail();

  if (t.wasPressed()) {
    // Same rule as the dots: a tap on a dark panel wakes it and does nothing
    // else, including the drag it might otherwise begin.
    touchOnlyWoke = dimmed;
    wake();
    if (!touchOnlyWoke && !infoPage && CARD_LOG.contains(t.x, t.y) &&
        !STATS_STRIP.contains(t.x, t.y)) {
      dragStartY = t.y;
      dragStartRows = scrollRows;
    }
  }

  // Content follows the finger: dragging down reveals older lines.
  if (t.isPressed() && dragStartY >= 0) {
    int rows = dragStartRows + (t.y - dragStartY) / LOG_PITCH;
    if (rows < 0) rows = 0;
    if (rows > maxScrollRows()) rows = maxScrollRows();
    if (rows != scrollRows) {
      scrollRows = rows;
      dirty |= R_LOG;
    }
  }
  if (t.wasReleased()) dragStartY = -1;

  if (touchOnlyWoke) return;

  if (t.wasClicked()) {
    for (int i = 0; i < 3; i++) {
      if (BTN_ZONE[i].contains(t.x, t.y)) {
        buttonAction(i, false);
        return;
      }
    }
    // The stats strip: back to the live tail.
    if (!infoPage && scrollRows > 0 && STATS_STRIP.contains(t.x, t.y)) {
      scrollRows = 0;
      dirty |= R_LOG;
    }
  }

  if (t.wasHold() && infoPage && BTN_ZONE[2].contains(t.x, t.y)) {
    buttonAction(2, true);
  }
}

// --- setup / loop ----------------------------------------------------------

void setup() {
  auto cfg = M5.config();
  cfg.internal_spk = true;  // for the write chirp; the mic is never used here
  M5.begin(cfg);
  M5.Display.setRotation(1);
  M5.Display.setBrightness(BRIGHT_ACTIVE);
  M5.Display.fillScreen(TFT_BLACK);
  M5.Speaker.setVolume(70);

  // The frame buffer. 16-bit wants ~150KB, which is what the PSRAM is for;
  // if allocation still fails, 8-bit halves it rather than booting blind.
  canvas.setPsram(true);
  canvas.setColorDepth(16);
  if (!canvas.createSprite(320, 240)) {
    canvas.setColorDepth(8);
    canvas.createSprite(320, 240);
  }
  canvas.setTextSize(1);

#if LED_BAR_COUNT > 0
  ledBar.begin();
  ledBar.setBrightness(40);
#endif
  ledIdle();

  // Non-blocking: the screen says "NO WI-FI" and keeps saying it, rather than
  // the node looking dead on a boot with the router down. An empty SSID means
  // a BLE-only node: no Wi-Fi radio at all, just the advertisement.
  if (wifiConfigured()) {
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
  }

  startBle();

  lastActivityMs = millis();
  dirty = R_ALL;
  render();
}

void loop() {
  M5.update();
  if (wsStarted) ws.loop();
  handleButtons();
  handleTouch();

  if (pendingLinkToggle) {
    pendingLinkToggle = false;
    toggleLink();
  }

  // Lines the radio queued while this task was elsewhere. Auth first, then
  // actions — a long capture holds the loop, and the next line simply waits
  // its turn here rather than interleaving on the wires.
  if (bleLineQueue) {
    String* line = nullptr;
    while (xQueueReceive(bleLineQueue, &line, 0) == pdTRUE) {
      bleHandleLine(*line);
      delete line;
    }
  }

  // A central that connects and says nothing is holding the advertisement
  // slot hostage; ten seconds is more than an honest bridge needs.
  if (bleConnected && !bleAuthed && bleAuthDeadline &&
      (int32_t)(millis() - bleAuthDeadline) > 0) {
    bleAuthDeadline = 0;
    logAction("ble auth timeout");
    bleDisconnectCentral();
  }

  static bool hadBle = false;
  if ((bool)bleAuthed != hadBle) {
    hadBle = bleAuthed;
    dirty |= R_STATUS;
    ledIdle();
  }

  static bool hadWifi = false;
  bool haveWifi = WiFi.status() == WL_CONNECTED;
  if (haveWifi != hadWifi) {
    hadWifi = haveWifi;
    dirty |= R_STATUS;
    ledIdle();
  }
  if (ledHoldUntil && (int32_t)(millis() - ledHoldUntil) >= 0) ledIdle();

  // Transport arbitration. The hub keys connections by client_id, so the node
  // must be exactly one client: while a bridge holds the BLE link, the node's
  // own socket goes away, and it comes back the moment the bridge does.
  if (bleAuthed && wsStarted) {
    ws.disconnect();
    wsConnected = false;
    wsStarted = false;
    dirty |= R_STATUS;
  }
  if (!bleAuthed && haveWifi && !wsStarted && !paused) startLink();

  // Advertising follows `paused` and nothing else; with a central connected
  // NimBLE has already stopped it for us.
  if (bleServer && !paused && !bleConnected &&
      !NimBLEDevice::getAdvertising()->isAdvertising()) {
    NimBLEDevice::getAdvertising()->start();
  }

  static uint32_t nextStatus = 0;
  static uint32_t nextChrome = 0;
  uint32_t now = millis();

  if ((wsConnected || bleAuthed) && (int32_t)(now - nextStatus) >= 0) {
    nextStatus = now + STATUS_PERIOD_MS;
    if (!inAction) sendNodeStatus();
  }

  // Battery, signal and uptime move slowly; repainting them on a timer keeps
  // the panel honest without making it busy. The composited frame makes this
  // safe on the info page too — a sprite push cannot flicker, so its numbers
  // stay live instead of being one tap from fresh.
  if ((int32_t)(now - nextChrome) >= 0) {
    nextChrome = now + 5000;
    dirty |= R_ALL;
  }

  if (!dimmed && (int32_t)(now - lastActivityMs) > (int32_t)DIM_AFTER_MS) {
    dimmed = true;
    M5.Display.setBrightness(BRIGHT_IDLE);
  }

  render();
  delay(2);
}
