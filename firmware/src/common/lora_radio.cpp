#include <Arduino.h>
#include <string.h>
#include "lora_radio.h"
#include "board_config.h"
#include "pi4io_expander.h"

// Standard RadioLib idiom: SX1262's constructor takes a Module*.
SX1262 radio = new Module(PIN_LORA_NSS, PIN_LORA_DIO1, RADIOLIB_NC, PIN_LORA_BUSY);

static volatile bool receivedFlag = false;
static void IRAM_ATTR setFlag(void) { receivedFlag = true; }

static void resetAndEnableRadio() {
    uint8_t pins = (1 << PI4IO_PIN_LORA_RESET) |
                   (1 << PI4IO_PIN_RF_SWITCH) |
                   (1 << PI4IO_PIN_LNA_ENABLE);
    pi4io.setOutputs(pins);   // IO_DIR: mark all three as outputs
    pi4io.enableDrive(pins);  // OUT_H_IM: clear Hi-Z - confirmed missing
                               // piece; nothing before this actually let
                               // these pins drive at all, regardless of
                               // what value/sequence was written to them.

    // Meshtastic's actual shipped m5stack_unitc6l driver - confirmed
    // working RIGHT NOW on this exact physical unit - does NOT pulse
    // SX_NRST low at all. It drives it high once, immediately, no delay
    // (per the working code quoted in meshtastic/firmware#10455).
    pi4io.write(PI4IO_PIN_LORA_RESET, true);
    pi4io.write(PI4IO_PIN_RF_SWITCH, true);   // enable SX_ANT_SW
    pi4io.write(PI4IO_PIN_LNA_ENABLE, true);  // enable SX_LNA_EN (Meshtastic's
                                               // own driver skips this - costs RX
                                               // sensitivity only, not basic init -
                                               // but the schematic confirms it's a
                                               // real signal, so we still set it)
}

bool loraRadioBegin() {
    // Deliberately does NOT call M5.begin() - see pi4io_expander.h for
    // why. Brings up I2C itself via pi4io.begin().
    Serial.println("loraRadioBegin: initializing expander directly (no M5Unified in this path)...");
    if (!pi4io.begin(PIN_I2C_SDA, PIN_I2C_SCL, PI4IO_I2C_ADDR)) {
        Serial.println("loraRadioBegin: WARNING - expander did not ACK on I2C");
    }

    resetAndEnableRadio();

    // Direct hardware sanity check: BUSY should go LOW once the chip is
    // ready. A single snapshot read can't tell "still starting up" apart
    // from "never coming up," so poll for up to 2s and report when (or
    // whether) it clears.
    pinMode(PIN_LORA_BUSY, INPUT);
    uint32_t busyStart = millis();
    while (digitalRead(PIN_LORA_BUSY) == HIGH && millis() - busyStart < 2000) {
        delay(10);
    }
    uint32_t busyElapsed = millis() - busyStart;
    if (digitalRead(PIN_LORA_BUSY) == LOW) {
        Serial.printf("loraRadioBegin: BUSY pin (%d) went LOW after %ums\n",
                      PIN_LORA_BUSY, busyElapsed);
    } else {
        Serial.printf("loraRadioBegin: BUSY pin (%d) still HIGH after %ums (gave up)\n",
                      PIN_LORA_BUSY, busyElapsed);
    }

    Serial.println("loraRadioBegin: configuring SPI...");
    SPI.begin(PIN_LORA_SCK, PIN_LORA_MISO, PIN_LORA_MOSI, PIN_LORA_NSS);

    Serial.println("loraRadioBegin: calling radio.begin()...");
    // frequency, bandwidth, spreading factor, coding rate, sync word,
    // power, preamble length, TCXO voltage, useRegulatorLDO - order and
    // the last two values confirmed against M5Stack's own example.
    int state = radio.begin(LORA_FREQ_MHZ, LORA_BW_KHZ, LORA_SF, LORA_CR,
                             LORA_SYNC_WORD, LORA_TX_POWER_DBM,
                             LORA_PREAMBLE_LEN, LORA_TCXO_VOLTAGE,
                             /* useRegulatorLDO = */ true);
    Serial.printf("loraRadioBegin: radio.begin() returned, code %d\n", state);
    if (state != RADIOLIB_ERR_NONE) {
        Serial.printf("SX1262 begin() failed, code %d\n", state);
        return false;
    }
    return true;
}

bool loraStartListening() {
    radio.setPacketReceivedAction(setFlag);
    int state = radio.startReceive();
    if (state != RADIOLIB_ERR_NONE) {
        Serial.printf("startReceive() failed, code %d\n", state);
        return false;
    }
    return true;
}

bool loraPacketReady() {
    if (receivedFlag) {
        receivedFlag = false;
        return true;
    }
    return false;
}

int loraReadPacket(uint8_t *buf, size_t maxLen, size_t &outLen, float &rssi, float &snr) {
    size_t len = radio.getPacketLength();
    rssi = radio.getRSSI();
    snr = radio.getSNR();

    // When the official length is 0 (near-certainly a corrupted header),
    // read a small fixed "peek" amount instead of either fabricating a
    // full-buffer read (an earlier bug) or skipping the read entirely.
    //
    // NOTE, from direct testing: in our pinned RadioLib version,
    // radio.readData() does NOT write real bytes here even though we ask
    // it to - a sentinel-fill test showed the buffer coming back
    // completely untouched on a CRC/header-failed packet. A follow-up
    // attempt to get real bytes anyway, by bypassing readData() and
    // calling RadioLib's low-level readBuffer()/clearIrqStatus() directly,
    // broke reception outright (zero further packets detected at all) and
    // was reverted. Not worth pursuing further without deeper RadioLib
    // internals knowledge than we currently have - RSSI/SNR/error code
    // stay reliable on every attempt regardless, which is enough to keep
    // debugging the actual SF/BW/CR mismatch.
    constexpr size_t kDiagnosticPeekLen = 32;
    bool lengthKnown = (len > 0);
    if (!lengthKnown) len = kDiagnosticPeekLen;
    if (len > maxLen) len = maxLen; // buffer-overflow guard only

    int state = radio.readData(buf, len);
    outLen = len; // NOTE: when !lengthKnown, this is a peek size, not a
                  // confirmed packet length, and the bytes are not real
                  // received data (see note above) - see loraReadPacket()'s
                  // doc comment in the header for how callers should treat it.
    radio.startReceive(); // resume listening for the next packet
    return state;
}