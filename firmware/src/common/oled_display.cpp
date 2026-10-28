#include <Arduino.h>
#include <SPI.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include "oled_display.h"
#include "board_config.h"

// 64x48 SSD1306 over SPI. Adafruit_SSD1306 has no built-in case for this
// resolution (only the common 128x64/128x32/96x16 sizes), so it applies
// the wrong COM-pin hardware configuration - this is a confirmed, known
// issue for this exact panel (MeshCore hit and fixed the identical bug
// for the same Unit C6L display, PR #1885), and it's what produces
// output "spread into vertical stripes" rather than a garbled image or
// nothing at all. Fixed below by resending the SET_COM_PINS command with
// the correct value ourselves, right after the library's own begin().
static Adafruit_SSD1306 display(64, 48, &SPI, PIN_OLED_DC, PIN_OLED_RST, PIN_OLED_CS);

// Sends one raw SSD1306 command byte directly, bypassing Adafruit_SSD1306's
// (non-public) command helper - only needed for this one COM-pin fixup.
static void sendRawCommand(uint8_t cmd) {
    pinMode(PIN_OLED_DC, OUTPUT);
    pinMode(PIN_OLED_CS, OUTPUT);
    digitalWrite(PIN_OLED_DC, LOW); // command mode
    SPI.beginTransaction(SPISettings(8000000, MSBFIRST, SPI_MODE0));
    digitalWrite(PIN_OLED_CS, LOW);
    SPI.transfer(cmd);
    digitalWrite(PIN_OLED_CS, HIGH);
    SPI.endTransaction();
}

bool oledInit() {
    // Assumes SPI.begin() already ran (loraRadioBegin() does this) with
    // SCK/MISO/MOSI matching board_config.h - only CS/DC/RST are ours.
    if (!display.begin(SSD1306_SWITCHCAPVCC)) {
        return false;
    }

    // The COM-pin fix: 0xDA = SET_COM_PINS command, 0x12 = the value this
    // exact panel needs (confirmed working for Unit C6L by MeshCore's own
    // fix) - alternative COM pin configuration, left/right remap disabled.
    sendRawCommand(0xDA);
    sendRawCommand(0x12);

    display.setRotation(2); // was upside down at rotation 0
    display.setTextColor(SSD1306_WHITE);
    display.cp437(true);
    display.clearDisplay();
    display.display();
    return true;
}

void oledShowStatus(const char *role, float freqMHz, int sf, uint32_t rxOkCount, uint32_t rxFailCount) {
    display.clearDisplay();
    display.setTextSize(1);
    display.setCursor(0, 0);
    display.println(role);
    // 3 decimals, no space: "868.525MHz" is exactly 10 chars = 60px, which
    // fits the 64px panel; one decimal would show 868.525 as "868.5".
    display.printf("%.3fMHz\n", freqMHz);
    display.printf("SF%d\n", sf);
    display.printf("OK: %lu\n", (unsigned long)rxOkCount);
    display.printf("Fail: %lu\n", (unsigned long)rxFailCount);
    display.display();
}

void oledShowLastPacket(size_t lenBytes, float rssi, float snr) {
    display.clearDisplay();
    display.setTextSize(1);
    display.setCursor(0, 0);
    display.println("LAST OK");
    display.printf("%.0fdBm\n", rssi);
    display.printf("%.1fdB %uB\n", snr, (unsigned)lenBytes);
    display.display();
}

void oledShowRaw(const uint8_t *buf, size_t len, int errCode, float rssi, float snr) {
    display.clearDisplay();
    display.setTextSize(1);
    display.setCursor(0, 0);
    display.printf("e:%d %.0fdBm\n", errCode, rssi);
    size_t showBytes = len < 8 ? len : 8;
    for (size_t i = 0; i < showBytes; i++) {
        display.printf("%02X", buf[i]);
        if (i % 4 == 3) display.println();
        else display.print(' ');
    }
    display.display();
}
