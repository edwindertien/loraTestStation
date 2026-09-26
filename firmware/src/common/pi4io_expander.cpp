#include "pi4io_expander.h"

PI4IOExpander pi4io;

bool PI4IOExpander::begin(uint8_t sda, uint8_t scl, uint8_t addr) {
    _addr = addr;
    Wire.begin(sda, scl);
    uint8_t dummy;
    return readReg(REG_IO_DIR, dummy); // simple ACK check
}

bool PI4IOExpander::writeReg(uint8_t reg, uint8_t value) {
    Wire.beginTransmission(_addr);
    Wire.write(reg);
    Wire.write(value);
    return Wire.endTransmission() == 0;
}

bool PI4IOExpander::readReg(uint8_t reg, uint8_t &value) {
    Wire.beginTransmission(_addr);
    Wire.write(reg);
    if (Wire.endTransmission(false) != 0) return false; // repeated start
    if (Wire.requestFrom((int)_addr, 1) != 1) return false;
    value = Wire.read();
    return true;
}

void PI4IOExpander::setOutputs(uint8_t pinMask) {
    uint8_t dir = 0;
    readReg(REG_IO_DIR, dir);
    writeReg(REG_IO_DIR, dir | pinMask);
}

void PI4IOExpander::enableDrive(uint8_t pinMask) {
    uint8_t hiZ = 0xFF; // assume worst case (all Hi-Z) if the read fails
    readReg(REG_OUT_H_IM, hiZ);
    writeReg(REG_OUT_H_IM, hiZ & ~pinMask); // clear bits -> stop forcing Hi-Z
}

void PI4IOExpander::write(uint8_t pin, bool high) {
    if (high) _outState |= (1 << pin);
    else _outState &= ~(1 << pin);
    writeReg(REG_OUT_PORT, _outState);
}

void PI4IOExpander::setInputPullUp(uint8_t pinMask) {
    uint8_t en = 0;
    readReg(REG_PULL_EN, en);
    writeReg(REG_PULL_EN, en | pinMask);
    uint8_t sel = 0;
    readReg(REG_PULL_SEL, sel);
    writeReg(REG_PULL_SEL, sel | pinMask); // 1 = pull-up
}

bool PI4IOExpander::read(uint8_t pin) {
    uint8_t status = 0;
    readReg(REG_IN_STATUS, status);
    return status & (1 << pin);
}