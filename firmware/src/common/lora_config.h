#pragma once

// Runtime-changeable LoRa PHY settings. board_config.h's LORA_* macros are
// now only the compile-time DEFAULTS; everything at runtime (the CLI, the
// OLED, the radio itself) goes through this struct instead.
//
// Fields are plain int/float on purpose: values arrive from a text CLI, so
// they're range-checked at full width BEFORE ever being narrowed to the
// uint8_t/uint16_t RadioLib wants (otherwise "sf 300" would silently wrap
// to a valid-looking 44).
struct LoraConfig {
    float freqMHz;   // 150..960 (the SX1262's own limits; RadioLib enforces the same)
    float bwKHz;     // one of the discrete SX126x bandwidths, see loraSnapBandwidth()
    int   sf;        // spreading factor, 5..12
    int   cr;        // coding-rate denominator: 5..8 means 4/5 .. 4/8
    int   syncWord;  // 0x00..0xFF (0x12 = "private", 0x34 = LoRaWAN public)
    int   preamble;  // symbols; RadioLib doesn't range-check this for LoRa, so
                     // 6..65535 here is our own sanity bound (it's a 16-bit field)
};

// board_config.h's LORA_* values.
LoraConfig loraConfigDefaults();

// Full-struct range check. On failure returns false and, if why != nullptr,
// points it at a short human-readable reason.
bool loraConfigSane(const LoraConfig &c, const char **why = nullptr);

// The SX126x only supports these bandwidths (kHz): 7.8 10.4 15.6 20.8 31.25
// 41.7 62.5 125 250 500. Snaps a user-typed value (e.g. 41.67) to the exact
// canonical one; returns false if it isn't close to any of them.
bool loraSnapBandwidth(float in, float &out);

// Standard EU868 channel plan (the 3 mandatory + 5 commonly-added uplink
// channels): 0..2 = 868.1 / 868.3 / 868.5, 3..7 = 867.1 / .3 / .5 / .7 / .9.
// Returns 0.0 for an out-of-range index.
constexpr int kLoraChannelCount = 8;
float loraChannelFreq(int ch);

// Persistence (ESP32 NVS, namespace "lora"). Load leaves `out` untouched and
// returns false if nothing valid is stored.
bool loraConfigLoad(LoraConfig &out);
bool loraConfigSave(const LoraConfig &c);
void loraConfigClearSaved();
