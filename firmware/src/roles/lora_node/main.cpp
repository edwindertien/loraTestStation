#include <Arduino.h>
#include <Adafruit_NeoPixel.h>
#include "board_config.h"
#include "lora_radio.h"

// Role: raw LoRa point-to-point link / LoRaWAN end device (RadioLib).
//
// Deliberately does NOT call M5.begin()/use M5Unified - see
// src/common/pi4io_expander.h for why. loraRadioBegin() brings up I2C and
// SPI itself.
//
// This role currently just brings the SX1262 up and idles; transmit /
// LoRaWAN join-and-uplink logic is still TODO. Use the sc_gateway role if
// what you want right now is to receive and inspect packets from another
// node.

Adafruit_NeoPixel rgb(1, PIN_WS2812_LED, NEO_GRB + NEO_KHZ800);

void setup() {
    Serial.begin(115200);
    delay(300);
    Serial.printf("\n[%s] Unit-C6L bring-up scaffold\n", FIRMWARE_ROLE);

    rgb.begin();
    rgb.setBrightness(40);

    if (loraRadioBegin()) {
        Serial.printf("[%s] SX1262 up at %.1f MHz (TX/join logic: TODO)\n",
                      FIRMWARE_ROLE, LORA_FREQ_MHZ);
    } else {
        Serial.printf("[%s] radio bring-up FAILED - see message above.\n", FIRMWARE_ROLE);
    }
}

void loop() {
    static uint8_t hue = 0;
    rgb.setPixelColor(0, Adafruit_NeoPixel::ColorHSV(hue * 256));
    rgb.show();
    hue += 4;
    Serial.printf("[%s] alive\n", FIRMWARE_ROLE);
    delay(500);
}
