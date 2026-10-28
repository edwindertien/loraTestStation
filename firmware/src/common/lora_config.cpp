#include <Arduino.h>
#include <Preferences.h>
#include <math.h>
#include "lora_config.h"
#include "board_config.h"

LoraConfig loraConfigDefaults() {
    LoraConfig c;
    c.freqMHz  = LORA_FREQ_MHZ;
    c.bwKHz    = LORA_BW_KHZ;
    c.sf       = LORA_SF;
    c.cr       = LORA_CR;
    c.syncWord = LORA_SYNC_WORD;
    c.preamble = LORA_PREAMBLE_LEN;
    return c;
}

// Discrete bandwidths the SX126x supports in LoRa mode (kHz) - matches the
// switch in RadioLib's SX126x::setBandwidth().
static const float kBandwidths[] = {
    7.8f, 10.4f, 15.6f, 20.8f, 31.25f, 41.7f, 62.5f, 125.0f, 250.0f, 500.0f
};

bool loraSnapBandwidth(float in, float &out) {
    for (float bw : kBandwidths) {
        if (fabsf(in - bw) < 0.1f) { // also false for NaN
            out = bw;
            return true;
        }
    }
    return false;
}

bool loraConfigSane(const LoraConfig &c, const char **why) {
    const char *unused;
    if (!why) why = &unused;

    // Comparisons are written in the "!(in range)" form so NaN is rejected.
    if (!(c.freqMHz >= 150.0f && c.freqMHz <= 960.0f)) {
        *why = "frequency must be 150-960 MHz";
        return false;
    }
    float snapped;
    if (!loraSnapBandwidth(c.bwKHz, snapped) || fabsf(snapped - c.bwKHz) > 0.001f) {
        *why = "bandwidth must be one of 7.8 10.4 15.6 20.8 31.25 41.7 62.5 125 250 500 (kHz)";
        return false;
    }
    if (c.sf < 5 || c.sf > 12) {
        *why = "spreading factor must be 5-12";
        return false;
    }
    if (c.cr < 5 || c.cr > 8) {
        *why = "coding rate must be 5-8 (meaning 4/5 .. 4/8)";
        return false;
    }
    if (c.syncWord < 0 || c.syncWord > 255) {
        *why = "sync word must be 00-FF (hex)";
        return false;
    }
    if (c.preamble < 6 || c.preamble > 65535) {
        *why = "preamble must be 6-65535 symbols";
        return false;
    }
    return true;
}

static const float kEu868Channels[kLoraChannelCount] = {
    868.1f, 868.3f, 868.5f, 867.1f, 867.3f, 867.5f, 867.7f, 867.9f
};

float loraChannelFreq(int ch) {
    return (ch >= 0 && ch < kLoraChannelCount) ? kEu868Channels[ch] : 0.0f;
}

// ---- persistence ------------------------------------------------------------
// Opened read-write even for loading: in read-only mode a namespace that
// doesn't exist yet logs an error on every first boot; read-write just creates
// it empty and stays quiet.
static const char *kNvsNamespace = "lora";
static const uint8_t kNvsVersion = 1;

bool loraConfigLoad(LoraConfig &out) {
    Preferences p;
    if (!p.begin(kNvsNamespace, false)) return false;

    bool ok = (p.getUChar("v", 0) == kNvsVersion);
    if (ok) {
        LoraConfig c;
        c.freqMHz  = p.getFloat("freq", NAN);
        c.bwKHz    = p.getFloat("bw", NAN);
        c.sf       = p.getUChar("sf", 0);
        c.cr       = p.getUChar("cr", 0);
        c.syncWord = p.getUChar("sync", 0);
        c.preamble = p.getUShort("pre", 0);
        // Never trust stored values blindly - corrupted or out-of-range
        // settings fall back to defaults instead of bricking the radio.
        ok = loraConfigSane(c, nullptr);
        if (ok) out = c;
    }
    p.end();
    return ok;
}

bool loraConfigSave(const LoraConfig &c) {
    Preferences p;
    if (!p.begin(kNvsNamespace, false)) return false;

    bool ok = true;
    ok &= (p.putFloat("freq", c.freqMHz) == sizeof(float));
    ok &= (p.putFloat("bw", c.bwKHz) == sizeof(float));
    ok &= (p.putUChar("sf", (uint8_t)c.sf) == 1);
    ok &= (p.putUChar("cr", (uint8_t)c.cr) == 1);
    ok &= (p.putUChar("sync", (uint8_t)c.syncWord) == 1);
    ok &= (p.putUShort("pre", (uint16_t)c.preamble) == 2);
    ok &= (p.putUChar("v", kNvsVersion) == 1); // written last: marks the set complete
    p.end();
    return ok;
}

void loraConfigClearSaved() {
    Preferences p;
    if (p.begin(kNvsNamespace, false)) {
        p.clear();
        p.end();
    }
}
