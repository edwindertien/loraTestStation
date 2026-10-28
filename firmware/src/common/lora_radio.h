#pragma once
#include <RadioLib.h>
#include "lora_config.h"

// Shared SX1262 bring-up for both roles. Deliberately does NOT use
// M5.begin()/M5Unified at all - see pi4io_expander.h for why (confirmed
// I2C bus contention from M5GFX's board autodetection on this board
// profile). loraRadioBegin() brings up I2C and SPI itself; nothing else
// needs to run first in the role's setup().

extern SX1262 radio;

// Full bring-up: enables the RF switch/LNA and releases reset via the IO
// expander, then brings up the SX1262 over RadioLib with the given PHY
// settings. Returns false (with a Serial message) if the radio doesn't
// respond. The no-argument form uses board_config.h's compile-time defaults.
bool loraRadioBegin(const LoraConfig &cfg);
inline bool loraRadioBegin() { return loraRadioBegin(loraConfigDefaults()); }

// The PHY settings the radio is currently running with. Single source of
// truth for the CLI/OLED - never read board_config.h's LORA_* macros for
// "what is the radio doing right now", they're only the boot defaults.
const LoraConfig &loraActiveConfig();

// Changes the PHY settings on a live radio: standby, apply, re-arm receive.
// All-or-nothing - if RadioLib rejects any value, the previous settings are
// restored and the RadioLib error code is returned (RADIOLIB_ERR_NONE on
// success). Callers should validate with loraConfigSane() first; this is the
// backstop for anything RadioLib itself still refuses. Any packet being
// received at that instant is lost.
int loraApplyConfig(const LoraConfig &cfg);

// Starts continuous, non-blocking receive (interrupt-driven).
bool loraStartListening();

// True once since the last call if a packet has arrived. Non-blocking.
bool loraPacketReady();

// Call after loraPacketReady() returns true. Copies the payload into buf
// (up to maxLen bytes), and reports its length, RSSI, and SNR. Returns a
// RadioLib status code (RADIOLIB_ERR_NONE on success). On a non-success
// return, the chip's own reported length was 0 (almost always a corrupted
// header) - outLen is then a small fixed "diagnostic peek" size, not a
// confirmed packet length. Confirmed by direct testing: on this failure
// path, radio.readData() does not actually write real bytes into buf in
// our pinned RadioLib version - what's there is not genuine received
// data. RSSI/SNR are real and reliable either way.
int loraReadPacket(uint8_t *buf, size_t maxLen, size_t &outLen, float &rssi, float &snr);
