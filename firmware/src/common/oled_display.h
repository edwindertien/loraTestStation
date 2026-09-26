#pragma once
#include <stddef.h>

// Minimal direct-SPI SSD1306 driver (Adafruit_SSD1306, NOT M5GFX) for the
// Unit-C6L's onboard 64x48 OLED. Deliberately independent of M5Unified,
// for the same reason as pi4io_expander.h: M5GFX's board autodetection
// generates I2C traffic that contends with the expander this project
// depends on for the radio. The OLED itself is SPI - confirmed from the
// carrier schematic (OLED_SCK/OLED_MOSI are the same G20/G21 the SX1262
// uses, just with their own CS line) - so driving it directly adds no I2C
// traffic at all.
//
// Assumes SPI.begin() has already been called elsewhere (loraRadioBegin()
// does this) with SCK/MISO/MOSI matching board_config.h - this only adds
// its own CS/DC/RST on top of that shared bus.

bool oledInit();
void oledShowStatus(const char *role, float freqMHz, int sf, uint32_t rxOkCount, uint32_t rxFailCount);
void oledShowLastPacket(size_t lenBytes, float rssi, float snr);
// Shows the most recent RX attempt regardless of outcome - error code,
// RSSI/SNR, and the first few raw bytes - useful for tuning SF/BW/CR
// against another node without needing a laptop attached.
void oledShowRaw(const uint8_t *buf, size_t len, int errCode, float rssi, float snr);