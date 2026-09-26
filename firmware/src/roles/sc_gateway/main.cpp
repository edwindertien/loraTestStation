#include <Arduino.h>
#include <string.h>
#include <Adafruit_NeoPixel.h>
#include "board_config.h"
#include "lora_radio.h"

// Role: single-channel LoRaWAN "gateway" - for now, this only proves out
// PHY-level receive (bring up the SX1262, listen continuously, print any
// received packet's raw bytes/RSSI/SNR to serial). That's enough to answer
// "is my LoRaWAN node's radio actually reaching this board" before adding
// the Semtech UDP packet-forwarder layer that would let a real network
// server (ChirpStack, TTN) decode it - that part is still TODO.
//
// Deliberately NOT using the OLED/button right now: reception only works
// reliably without them, even with the SPI-pin-reassert fix in place - the
// display's SPI usage still interferes with the shared bus somehow that
// hasn't been fully isolated yet. Revisit once the SEN66 link itself is
// solid. All diagnostics here go through this serial CLI instead, which
// can't touch the SPI/I2C buses at all, so it carries zero risk of
// reintroducing that regression.
//
// Deliberately does NOT call M5.begin()/use M5Unified - see
// src/common/pi4io_expander.h for why. loraRadioBegin() brings up I2C and
// SPI itself.
//
// Match LORA_FREQ_MHZ/LORA_SF/LORA_BW_KHZ in board_config.h to your node's
// settings, or nothing below will ever fire.

Adafruit_NeoPixel rgb(1, PIN_WS2812_LED, NEO_GRB + NEO_KHZ800);

static bool radioReady = false; // captured once in setup(), shown on every heartbeat below
static uint32_t rxOkCount = 0;
static uint32_t rxFailCount = 0;
static bool rawMode = false; // when true: full detail for every attempt, not just clean successes
static uint8_t lastBuf[256];
static size_t lastLen = 0;
static int lastErrCode = 0;
static float lastRssi = 0, lastSnr = 0;
static uint32_t lastAttemptMs = 0;

static void flashWhite() {
    rgb.setPixelColor(0, rgb.Color(255, 255, 255));
    rgb.show();
    delay(80);
}

static void printHexDump(const uint8_t *buf, size_t len) {
    for (size_t i = 0; i < len; i++) Serial.printf("%02X", buf[i]);
    Serial.println();
}

static void printHelp() {
    Serial.println("Commands:");
    Serial.println("  help          show this list");
    Serial.println("  stats         show RX success/fail counts and uptime");
    Serial.println("  info          show current radio config (freq/SF/BW/CR/sync)");
    Serial.println("  last          reprint the most recent RX attempt (success or fail)");
    Serial.println("  raw on|off    show full detail for every attempt, not just clean successes");
}

static void printAttempt(const char *label) {
    // On failure, lastLen is a fixed-size diagnostic peek, not a confirmed
    // packet length (see loraReadPacket()'s doc comment) - label it as
    // such so it's not mistaken for "the packet was N bytes."
    const char *lenLabel = (lastErrCode == RADIOLIB_ERR_NONE) ? "len" : "peek";
    Serial.printf("[%s] %s  err=%d  RSSI %.1f dBm  SNR %.1f dB  %s=%u  ",
                  FIRMWARE_ROLE, label, lastErrCode, lastRssi, lastSnr, lenLabel, (unsigned)lastLen);
    printHexDump(lastBuf, lastLen);
}

static void handleCommand(const String &line) {
    int sp = line.indexOf(' ');
    String cmd = sp < 0 ? line : line.substring(0, sp);
    String args = sp < 0 ? "" : line.substring(sp + 1);
    args.trim();

    if (cmd == "help") {
        printHelp();
    } else if (cmd == "stats") {
        Serial.printf("RX ok: %lu   RX fail: %lu   uptime: %lus\n",
                      (unsigned long)rxOkCount, (unsigned long)rxFailCount, millis() / 1000);
    } else if (cmd == "info") {
        Serial.printf("radio init: %s\n", radioReady ? "ok" : "FAILED");
        Serial.printf("%.1f MHz  SF%d  BW%.0f kHz  CR4/%d  sync 0x%02X\n",
                      LORA_FREQ_MHZ, LORA_SF, LORA_BW_KHZ, LORA_CR, LORA_SYNC_WORD);
    } else if (cmd == "last") {
        if (lastAttemptMs == 0) {
            Serial.println("No RX attempts yet.");
        } else {
            printAttempt("last attempt");
        }
    } else if (cmd == "raw") {
        if (args == "on") { rawMode = true; Serial.println("raw mode: on"); }
        else if (args == "off") { rawMode = false; Serial.println("raw mode: off"); }
        else Serial.println("usage: raw on|off");
    } else {
        Serial.printf("unknown command '%s' - try 'help'\n", cmd.c_str());
    }
}

void setup() {
    Serial.begin(115200);
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

    radioReady = loraRadioBegin() && loraStartListening();
    if (radioReady) {
        Serial.printf("[%s] listening at %.1f MHz, SF%d, BW%.0f kHz\n",
                      FIRMWARE_ROLE, LORA_FREQ_MHZ, LORA_SF, LORA_BW_KHZ);
    } else {
        Serial.printf("[%s] radio bring-up FAILED - see message above.\n", FIRMWARE_ROLE);
    }

    printHelp();
}

void loop() {
    // Non-blocking CLI: buffer characters, process a full line at a time.
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

        // Captured on every attempt, success or failure - useful the
        // moment you ask 'last', and for raw mode below.
        lastErrCode = state;
        lastRssi = rssi;
        lastSnr = snr;
        lastLen = len < sizeof(lastBuf) ? len : sizeof(lastBuf);
        memcpy(lastBuf, buf, lastLen);
        lastAttemptMs = millis();

        if (state == RADIOLIB_ERR_NONE) {
            rxOkCount++;
            if (rawMode) {
                printAttempt("RX ok");
            } else {
                Serial.printf("[%s] RX %u bytes  RSSI %.1f dBm  SNR %.1f dB  ",
                              FIRMWARE_ROLE, (unsigned)len, rssi, snr);
                printHexDump(buf, len);
            }
            flashWhite();
        } else {
            rxFailCount++;
            if (rawMode) {
                printAttempt("RX fail");
            } else {
                Serial.printf("[%s] readData() failed, code %d  RSSI %.1f dBm  SNR %.1f dB\n",
                              FIRMWARE_ROLE, state, rssi, snr);
            }
        }
    }

    // Idle heartbeat so you can tell it's alive even with zero traffic.
    static uint32_t lastBeat = 0;
    if (millis() - lastBeat > 2000) {
        lastBeat = millis();
        rgb.setPixelColor(0, rgb.Color(0, 30, 30)); // dim cyan = idle/listening
        rgb.show();
        // Direct GPIO read of DIO1, bypassing RadioLib's interrupt/flag
        // mechanism entirely. If a real packet arrives and our software
        // handler doesn't service it, DIO1 should stay physically asserted
        // (nothing else clears it) - so this should catch a missed
        // interrupt reliably even at a 2s sample rate, not just a brief
        // pulse we might sample past.
        Serial.printf("[%s] listening... (radio init: %s, DIO1=%d)\n",
                      FIRMWARE_ROLE, radioReady ? "ok" : "FAILED", digitalRead(PIN_LORA_DIO1));
    }
}