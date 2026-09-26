#pragma once
#include <Arduino.h>
#include <Wire.h>

// Direct driver for the PI4IOE5V6408 I/O expander, used ONLY by the radio
// bring-up path (src/common/lora_radio.cpp), which deliberately does NOT
// call M5.begin() at all.
//
// Why: M5Unified/M5GFX's board autodetection for this very new board
// profile (board_M5UnitC6L) generates persistent I2C traffic - confirmed
// by testing, not assumed: isolated raw Wire writes to this same expander
// failed 5/5 retries with ESP_ERR_INVALID_STATE while M5.begin() was
// active. That's not occasional contention, it's the bus being
// monopolized. Meshtastic's own firmware, independently proven working on
// this exact hardware, has no M5Unified dependency at all - this class
// takes the same approach for the one path that needs deterministic,
// uncontended access to this chip.
//
// Register map (confirmed from two independent public sources, and the
// register names match a real Meshtastic bug report about this same
// board): 0x03 = IO direction (1=output), 0x05 = output port (value),
// 0x07 = output high-impedance override (1=forced Hi-Z regardless of
// direction/value - must be cleared for a pin to actually drive; this is
// the step that turned out to matter here). 0x0B = pull enable, 0x0D =
// pull select (1=pull-up, 0=pull-down), 0x0F = live input status.

class PI4IOExpander {
public:
    // Brings up Wire itself on the given pins - do not call Wire.begin()
    // separately. Returns false (chip didn't ACK) without blocking.
    bool begin(uint8_t sda, uint8_t scl, uint8_t addr = 0x43);

    void setOutputs(uint8_t pinMask);      // IO_DIR: mark these pins as outputs
    void enableDrive(uint8_t pinMask);     // OUT_H_IM: clear high-Z on these pins
    void write(uint8_t pin, bool high);    // OUTPUT_PORT
    void setInputPullUp(uint8_t pinMask);  // enable + select pull-up for these pins
    bool read(uint8_t pin);                // live INPUT_STATUS bit for this pin

private:
    uint8_t _addr = 0x43;
    uint8_t _outState = 0;

    bool writeReg(uint8_t reg, uint8_t value);
    bool readReg(uint8_t reg, uint8_t &value);

    static constexpr uint8_t REG_IO_DIR    = 0x03;
    static constexpr uint8_t REG_OUT_PORT  = 0x05;
    static constexpr uint8_t REG_OUT_H_IM  = 0x07;
    static constexpr uint8_t REG_PULL_EN   = 0x0B;
    static constexpr uint8_t REG_PULL_SEL  = 0x0D;
    static constexpr uint8_t REG_IN_STATUS = 0x0F;
};

extern PI4IOExpander pi4io;