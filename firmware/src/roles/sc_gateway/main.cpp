#include <Arduino.h>
#include <string.h>
#include <stdlib.h>
#include <SPI.h>
#include <Adafruit_NeoPixel.h>
#include "board_config.h"
#include "lora_config.h"
#include "lora_radio.h"
#include "oled_display.h"
#include "pi4io_expander.h"

// Role: single-channel LoRa receiver / "gateway" test tool. Brings up the
// SX1262, listens continuously, and reports every received packet's
// RSSI/SNR/decoded text on serial, the OLED and the RGB LED. It is a
// PHY-level receive tool, not yet a real LoRaWAN gateway: the Semtech UDP
// packet-forwarder layer that would let ChirpStack/TTN decode traffic is
// still TODO.
//
// Radio settings (frequency, bandwidth, spreading factor, coding rate, sync
// word, preamble) are RUNTIME state. board_config.h only supplies the boot
// defaults; the serial CLI changes them live ('help' lists the commands),
// and 'save' persists them across reboots in NVS. They must match the
// transmitter exactly - see context.md for the sync-word/coding-rate
// mismatch that once cost a long debugging session.
//
// Deliberately does NOT call M5.begin()/use M5Unified - see
// src/common/pi4io_expander.h for why. loraRadioBegin() brings up I2C and
// SPI itself. The OLED is direct SPI and the button goes through the same
// pi4io driver as the radio (I2C only), so neither touches M5GFX.

Adafruit_NeoPixel rgb(1, PIN_WS2812_LED, NEO_GRB + NEO_KHZ800);

static bool radioReady = false; // captured once in setup(); shown via 'info'
static bool oledOk = false;
// Where the active radio settings came from: "defaults" (board_config.h),
// "saved" (NVS), or "modified" (changed via the CLI since, not yet saved).
static const char *cfgState = "defaults";

static uint32_t rxOkCount = 0;
static uint32_t rxFailCount = 0;
static bool rawMode = false; // when true: also show the hex bytes alongside the normal summary
static bool jsonMode = false; // when true: each RX attempt is one JSON line, for apps (see printRxJson)
static uint8_t lastBuf[256];
static size_t lastLen = 0;
static int lastErrCode = 0;
static float lastRssi = 0, lastSnr = 0;
static uint32_t lastAttemptMs = 0;
static bool haveAttempt = false;

// The status screen always reflects the radio's ACTUAL settings, not the
// compile-time defaults - so it stays right after a CLI change.
static void redrawStatus() {
    if (!oledOk) return;
    const LoraConfig &c = loraActiveConfig();
    oledShowStatus(FIRMWARE_ROLE, c.freqMHz, c.sf, rxOkCount, rxFailCount);
}

// Simple debounced edge-detect on the button (active-low: pressed pulls
// to GND, via the expander's internal pull-up - no external pull-up on
// the board, confirmed from the carrier schematic). Returns true exactly
// once per press, not while held.
static bool buttonPressed() {
    static bool lastState = true; // idle = HIGH via the expander's internal pull-up
    static uint32_t lastChangeMs = 0;
    bool now = pi4io.read(PI4IO_PIN_BUTTON);
    bool pressed = false;
    if (now != lastState && millis() - lastChangeMs > 30) { // 30ms debounce
        lastChangeMs = millis();
        if (lastState && !now) pressed = true; // falling edge
        lastState = now;
    }
    return pressed;
}

static void flashWhite() {
    rgb.setPixelColor(0, rgb.Color(255, 255, 255));
    rgb.show();
    delay(80);
}

static void printHex(const uint8_t *buf, size_t len) {
    for (size_t i = 0; i < len; i++) Serial.printf("%02X", buf[i]);
}

static void printAscii(const uint8_t *buf, size_t len) {
    // Printable ASCII shown as-is; anything else (binary, or a
    // failed-attempt peek that's mostly garbage) shown as '.' rather than
    // garbling the terminal or misrepresenting non-text bytes.
    Serial.print('"');
    for (size_t i = 0; i < len; i++) {
        char c = (char)buf[i];
        Serial.print((c >= 32 && c < 127) ? c : '.');
    }
    Serial.print('"');
}

static void printHelp() {
    Serial.println("Commands:");
    Serial.println("  help          show this list");
    Serial.println("  stats         show RX success/fail counts and uptime");
    Serial.println("  info          show radio init status and the active radio settings");
    Serial.println("  last          reprint the most recent RX attempt (success or fail)");
    Serial.println("  raw on|off    also show hex bytes alongside the normal summary (default: off)");
    Serial.println("  json on|off   print each RX as one JSON line, for apps (default: off)");
    Serial.println("Radio settings (apply immediately; lost on reboot unless saved):");
    Serial.println("  get           all settings as one JSON line");
    Serial.println("  freq <MHz>    frequency, e.g. freq 868.3");
    Serial.println("  ch <0-7>      EU868 channel: 0-2 = 868.1/.3/.5, 3-7 = 867.1/.3/.5/.7/.9");
    Serial.println("  bw <kHz>      7.8 10.4 15.6 20.8 31.25 41.7 62.5 125 250 500");
    Serial.println("  sf <5-12>     spreading factor");
    Serial.println("  cr <5-8>      coding rate: 5 = 4/5 ... 8 = 4/8");
    Serial.println("  sync <hex>    sync word, e.g. sync 12  (0x12 private, 0x34 LoRaWAN)");
    Serial.println("  pre <6-65535> preamble length in symbols");
    Serial.println("  save          keep the current radio settings across reboots");
    Serial.println("  defaults      back to the board_config.h settings, forget saved ones");
    Serial.println("Replies to settings commands start with 'ok' or 'err'.");
}

// Shared by the live loop() print and the on-demand 'last' command, so
// both always agree on format.
static void printLastAttempt() {
    Serial.printf("[%s] %.1fs  ", FIRMWARE_ROLE, lastAttemptMs / 1000.0f);
    if (lastErrCode == RADIOLIB_ERR_NONE) {
        Serial.printf("RX %u bytes  RSSI %.1f dBm  SNR %.1f dB  ",
                      (unsigned)lastLen, lastRssi, lastSnr);
        printAscii(lastBuf, lastLen);
    } else {
        Serial.printf("RX fail  err=%d  RSSI %.1f dBm  SNR %.1f dB",
                      lastErrCode, lastRssi, lastSnr);
    }
    if (rawMode) {
        // On failure, lastLen is a fixed-size diagnostic peek, not a
        // confirmed packet length - see loraReadPacket()'s doc comment.
        Serial.print(lastErrCode == RADIOLIB_ERR_NONE ? "  hex: " : "  peek: ");
        printHex(lastBuf, lastLen);
    }
    Serial.println();
}

// One RX attempt as a single JSON line, for a monitoring app (jsonMode).
//  - The payload goes out as hex: lossless, and no escaping problems with
//    the quotes inside JSON payloads.
//  - A FAILED attempt carries no len/hex on purpose: its bytes are not real
//    received data (see loraReadPacket()'s doc comment), so an app must not
//    be handed them as if they were.
//  - Every event carries the radio settings it was received under, so a
//    stored record stays self-describing even after settings change.
// Printed hex byte-by-byte through the (now 2 KB) USB TX buffer - see the
// setTxBufferSize() note in setup() for why that buffer size matters here.
static void printRxJson() {
    const LoraConfig &c = loraActiveConfig();
    bool ok = (lastErrCode == RADIOLIB_ERR_NONE);
    Serial.printf("{\"ev\":\"rx\",\"up_ms\":%lu,\"ok\":%s,\"err\":%d,\"rssi\":%.1f,\"snr\":%.1f,",
                  (unsigned long)lastAttemptMs, ok ? "true" : "false", lastErrCode, lastRssi, lastSnr);
    if (ok) {
        Serial.printf("\"len\":%u,\"hex\":\"", (unsigned)lastLen);
        printHex(lastBuf, lastLen);
        Serial.print("\",");
    }
    Serial.printf("\"freq\":%.3f,\"bw\":%g,\"sf\":%d,\"cr\":%d,\"sync\":\"0x%02X\"}\n",
                  c.freqMHz, c.bwKHz, c.sf, c.cr, c.syncWord);
}

// ---- runtime radio settings ----------------------------------------------

// Whole-string parsers: strtof/strtol with an end-pointer check, because
// String::toFloat()/toInt() return 0 on garbage, which is indistinguishable
// from a legitimate 0 and would silently turn "freq abc" into "freq 0".
static bool parseFloatArg(const String &s, float &out) {
    if (s.length() == 0) return false;
    char *end = nullptr;
    float v = strtof(s.c_str(), &end);
    if (end == s.c_str() || *end != '\0') return false;
    out = v;
    return true;
}

static bool parseLongArg(const String &s, long &out, int base) {
    if (s.length() == 0) return false;
    char *end = nullptr;
    long v = strtol(s.c_str(), &end, base);
    if (end == s.c_str() || *end != '\0') return false;
    out = v;
    return true;
}

// key=value pairs, e.g. for the reply to a successful set command.
static void printCfgPairs(const LoraConfig &c) {
    Serial.printf("freq=%.3f bw=%g sf=%d cr=%d sync=0x%02X pre=%d",
                  c.freqMHz, c.bwKHz, c.sf, c.cr, c.syncWord, c.preamble);
}

// One JSON object on one line - for an external monitoring app. The sync
// word is a hex string because that's how the 'sync' command takes it.
static void printCfgJson(const LoraConfig &c) {
    Serial.printf("{\"freq\":%.3f,\"bw\":%g,\"sf\":%d,\"cr\":%d,\"sync\":\"0x%02X\",\"pre\":%d,\"state\":\"%s\"}\n",
                  c.freqMHz, c.bwKHz, c.sf, c.cr, c.syncWord, c.preamble, cfgState);
}

// Validate, apply to the live radio, report. Used by every set command.
static void applyConfig(const LoraConfig &n) {
    const char *why = nullptr;
    if (!loraConfigSane(n, &why)) {
        Serial.printf("err %s\n", why);
        return;
    }
    int st = loraApplyConfig(n);
    if (st != RADIOLIB_ERR_NONE) {
        Serial.printf("err radio rejected the change (code %d); 'get' shows the active settings\n", st);
        return;
    }
    cfgState = "modified";
    Serial.print("ok ");
    printCfgPairs(loraActiveConfig());
    Serial.println();
    redrawStatus();
}

static void handleSetCommand(const String &cmd, const String &args) {
    if (!radioReady) {
        Serial.println("err radio not initialised");
        return;
    }
    LoraConfig n = loraActiveConfig();
    float f = 0;
    long v = 0;

    if (cmd == "freq") {
        if (!parseFloatArg(args, f)) { Serial.println("err usage: freq <MHz>"); return; }
        n.freqMHz = f;
    } else if (cmd == "ch") {
        if (!parseLongArg(args, v, 10) || loraChannelFreq((int)v) == 0.0f) {
            Serial.println("err usage: ch <0-7>");
            return;
        }
        n.freqMHz = loraChannelFreq((int)v);
    } else if (cmd == "bw") {
        if (!parseFloatArg(args, f) || !loraSnapBandwidth(f, n.bwKHz)) {
            Serial.println("err bandwidth must be one of 7.8 10.4 15.6 20.8 31.25 41.7 62.5 125 250 500 (kHz)");
            return;
        }
    } else if (cmd == "sf") {
        if (!parseLongArg(args, v, 10)) { Serial.println("err usage: sf <5-12>"); return; }
        n.sf = (int)v;
    } else if (cmd == "cr") {
        if (!parseLongArg(args, v, 10)) { Serial.println("err usage: cr <5-8>"); return; }
        n.cr = (int)v;
    } else if (cmd == "sync") {
        // Base 16 on purpose: sync words are conventionally hex, so "12"
        // means 0x12 (and "0x34" works too).
        if (!parseLongArg(args, v, 16)) { Serial.println("err usage: sync <hex>, e.g. sync 12"); return; }
        n.syncWord = (int)v;
    } else if (cmd == "pre") {
        if (!parseLongArg(args, v, 10)) { Serial.println("err usage: pre <6-65535>"); return; }
        n.preamble = (int)v;
    }
    applyConfig(n); // range-checks everything (including values too wide for the field) before touching the radio
}

static void handleCommand(const String &line) {
    int sp = line.indexOf(' ');
    String cmd = sp < 0 ? line : line.substring(0, sp);
    String args = sp < 0 ? "" : line.substring(sp + 1);
    cmd.toLowerCase();
    args.trim();

    if (cmd == "help") {
        printHelp();
    } else if (cmd == "stats") {
        Serial.printf("RX ok: %lu   RX fail: %lu   uptime: %lus\n",
                      (unsigned long)rxOkCount, (unsigned long)rxFailCount, millis() / 1000);
    } else if (cmd == "info") {
        const LoraConfig &c = loraActiveConfig();
        Serial.printf("radio init: %s\n", radioReady ? "ok" : "FAILED");
        Serial.printf("%.3f MHz  SF%d  BW%g kHz  CR4/%d  sync 0x%02X  preamble %d\n",
                      c.freqMHz, c.sf, c.bwKHz, c.cr, c.syncWord, c.preamble);
        Serial.printf("settings: %s\n", cfgState);
    } else if (cmd == "last") {
        if (!haveAttempt) {
            Serial.println("No RX attempts yet.");
        } else {
            printLastAttempt();
        }
    } else if (cmd == "raw") {
        if (args == "on") { rawMode = true; Serial.println("raw mode: on"); }
        else if (args == "off") { rawMode = false; Serial.println("raw mode: off"); }
        else Serial.println("usage: raw on|off");
    } else if (cmd == "json") {
        if (args == "on") { jsonMode = true; Serial.println("ok json=on"); }
        else if (args == "off") { jsonMode = false; Serial.println("ok json=off"); }
        else Serial.println("err usage: json on|off");
    } else if (cmd == "get") {
        printCfgJson(loraActiveConfig());
    } else if (cmd == "freq" || cmd == "ch" || cmd == "bw" || cmd == "sf" ||
               cmd == "cr" || cmd == "sync" || cmd == "pre") {
        handleSetCommand(cmd, args);
    } else if (cmd == "save") {
        if (!radioReady) {
            Serial.println("err radio not initialised");
        } else if (loraConfigSave(loraActiveConfig())) {
            cfgState = "saved";
            Serial.println("ok saved");
        } else {
            Serial.println("err could not write the saved settings");
        }
    } else if (cmd == "defaults") {
        if (!radioReady) {
            Serial.println("err radio not initialised");
        } else {
            int st = loraApplyConfig(loraConfigDefaults());
            if (st != RADIOLIB_ERR_NONE) {
                Serial.printf("err radio rejected the defaults (code %d)\n", st);
            } else {
                loraConfigClearSaved();
                cfgState = "defaults";
                Serial.print("ok defaults restored, saved settings cleared: ");
                printCfgPairs(loraActiveConfig());
                Serial.println();
                redrawStatus();
            }
        }
    } else {
        Serial.printf("unknown command '%s' - try 'help'\n", cmd.c_str());
    }
}

void setup() {
    // Must come BEFORE Serial.begin(): the USB-CDC TX buffer defaults to just
    // 256 bytes, and with the zero write-timeout set below, a write that
    // doesn't fit is TRUNCATED, not blocked (read from arduino-esp32's
    // HWCDC::write()). A 90-byte payload is ~400 bytes as a JSON line, so a
    // slow-draining host would cut lines short - and an app parsing them
    // would silently lose packets. 2 KB holds any line we print.
    Serial.setTxBufferSize(2048);
    Serial.begin(115200);
    // Confirmed fix (arduino-esp32's own issue tracker, #7779) for native
    // USB CDC: without this, Serial.print()/printf() calls can block
    // indefinitely once the USB TX buffer fills and nothing is reading it
    // - i.e. exactly "works fine with a terminal open, freezes solid
    // without one," since every Serial call in loop() would then hang the
    // whole firmware. This makes writes non-blocking instead - with no
    // host connected, output is just silently dropped rather than
    // stalling everything else (the radio, the button, the display).
    Serial.setTxTimeoutMs(0);
    // Wait for the terminal to actually reconnect, rather than guessing at
    // a fixed delay - native USB CDC re-enumerates on reset, which can
    // take longer than a short delay covers, especially for an
    // autoconnecting terminal. Caps out after 3s so this still boots and
    // runs fine with no terminal attached at all (e.g. in the field).
    uint32_t waitStart = millis();
    while (!Serial && millis() - waitStart < 3000) {
        delay(10);
    }
    delay(200);
    Serial.printf("\n[%s] Unit-C6L bring-up scaffold\n", FIRMWARE_ROLE);

    rgb.begin();
    rgb.setBrightness(40);

    // Radio settings: saved ones if there are valid ones, else board_config.h's.
    LoraConfig cfg = loraConfigDefaults();
    bool fromSaved = loraConfigLoad(cfg); // leaves cfg untouched if nothing valid is stored
    cfgState = fromSaved ? "saved" : "defaults";

    radioReady = loraRadioBegin(cfg) && loraStartListening();
    if (!radioReady && fromSaved) {
        // Saved settings passed our own range checks but the radio still
        // refused them (e.g. after a RadioLib upgrade changed what it accepts).
        // They persist across reboots, so leaving them in place would keep the
        // board deaf on every boot - fall back and clear them. (A normal
        // upload keeps them; a merged-image flash wipes the NVS region - see
        // context.md, "Flashing".)
        Serial.printf("[%s] radio rejected the saved settings - retrying with defaults\n", FIRMWARE_ROLE);
        loraConfigClearSaved();
        cfgState = "defaults";
        radioReady = loraRadioBegin(loraConfigDefaults()) && loraStartListening();
    }
    if (radioReady) {
        const LoraConfig &c = loraActiveConfig();
        Serial.printf("[%s] listening at %.3f MHz, SF%d, BW%g kHz (%s)\n",
                      FIRMWARE_ROLE, c.freqMHz, c.sf, c.bwKHz, cfgState);
    } else {
        Serial.printf("[%s] radio bring-up FAILED - see message above.\n", FIRMWARE_ROLE);
    }

    // Button pull-up: only relevant now that pi4io.begin() (inside
    // loraRadioBegin()) has brought up I2C. P0 has no external pull-up on
    // the board (confirmed from the carrier schematic) - without this it
    // floats and reads meaninglessly.
    pi4io.setInputPullUp(1 << PI4IO_PIN_BUTTON);

    oledOk = oledInit();
    Serial.printf("[%s] OLED init: %s\n", FIRMWARE_ROLE, oledOk ? "ok" : "FAILED");
    // Adafruit_SSD1306's begin() touches the same shared SPI object -
    // reassert our pins afterward regardless, cheap insurance either way.
    SPI.begin(PIN_LORA_SCK, PIN_LORA_MISO, PIN_LORA_MOSI, PIN_LORA_NSS);
    redrawStatus();

    printHelp();
}

void loop() {
    // Button: only one screen exists right now, so "reset the counters"
    // is the one clearly useful action available - easy to repurpose into
    // a screen-cycle/menu button once there's more than one screen.
    if (buttonPressed()) {
        rxOkCount = 0;
        rxFailCount = 0;
        Serial.printf("[%s] button pressed - counters reset\n", FIRMWARE_ROLE);
        redrawStatus();
    }

    // Non-blocking CLI: buffer characters, process a full line at a time.
    // Runs in the same loop as packet handling, so a radio reconfiguration
    // can never interleave with a packet read.
    static String line;
    while (Serial.available()) {
        char c = Serial.read();
        if (c == '\n' || c == '\r') {
            line.trim();
            if (line.length() > 0) handleCommand(line);
            line = "";
        } else {
            line += c;
        }
    }

    if (loraPacketReady()) {
        uint8_t buf[256];
        size_t len = 0;
        float rssi = 0, snr = 0;
        int state = loraReadPacket(buf, sizeof(buf), len, rssi, snr);

        // Captured on every attempt, success or failure - this is what
        // 'last' reprints, and what the live print below uses too.
        lastErrCode = state;
        lastRssi = rssi;
        lastSnr = snr;
        lastLen = len < sizeof(lastBuf) ? len : sizeof(lastBuf);
        memcpy(lastBuf, buf, lastLen);
        lastAttemptMs = millis();
        haveAttempt = true;

        if (state == RADIOLIB_ERR_NONE) rxOkCount++; else rxFailCount++;
        if (jsonMode) printRxJson(); else printLastAttempt();
        if (state == RADIOLIB_ERR_NONE) flashWhite();
        redrawStatus();
    }

    // Idle heartbeat - a simple, distinguishable "still alive" marker.
    static uint32_t lastBeat = 0;
    if (millis() - lastBeat > 2000) {
        lastBeat = millis();
        rgb.setPixelColor(0, rgb.Color(0, 30, 30)); // dim cyan = idle/listening
        rgb.show();
        Serial.printf("[%s] listening...\n", FIRMWARE_ROLE);
    }
}