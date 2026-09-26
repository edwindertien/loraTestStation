#pragma once
// M5Stack Unit-C6L (Stamp C6LoRa module: ESP32-C6 + SX1262) pin map.
// See ../../README.md for the full hardware background.

// --- CONFIRMED (M5Stack / Zephyr board docs) ---
// NOTE: named PIN_WS2812_LED, not PIN_RGB_LED - the esp32-c6-devkitc-1
// board variant's own pins_arduino.h already #defines PIN_RGB_LED (to 8,
// that dev kit's onboard LED - meaningless for our custom board), and
// silently redefining a framework macro is worth avoiding even though the
// compiler only warns rather than errors on it.
#define PIN_WS2812_LED      2   // WS2812C, single pixel
#define PIN_BUZZER          11  // passive buzzer, PWM
#define PIN_GROVE_SDA       4   // Grove Port A (shared with LP-UART RX)
#define PIN_GROVE_SCL       5   // Grove Port A (shared with LP-UART TX)
#define PIN_OLED_CS         6
#define PIN_OLED_DC         18
#define PIN_OLED_RST        15  // GPIO15 is an ESP32-C6 strapping pin - a
                                 // community board-support fix removed an
                                 // LED from this pin for exactly that
                                 // reason, worth remembering if this line
                                 // ever misbehaves at boot.

// --- CONFIRMED (now from TWO sources: community board-support commits,
// AND M5Stack's own official Unit C6L LoRa example at
// docs.m5stack.com/en/arduino/unit_c6l/lora, which matches exactly) ---
// SX1262 direct SPI/GPIO pins.
#define PIN_LORA_SCK        20
#define PIN_LORA_MISO       22
#define PIN_LORA_MOSI       21
#define PIN_LORA_NSS        23
#define PIN_LORA_DIO1       7
#define PIN_LORA_BUSY       19
// M5Stack's own example never calls SPI.begin() manually - M5.begin()
// already brings up the shared SPI bus for this board. SCK/MISO/MOSI above
// are kept only as a documented fallback if that ever turns out false.
//
// RESET is NOT a plain GPIO - see PI4IO_PIN_LORA_RESET below. Likewise
// there is deliberately no direct RXEN pin here: an earlier board-support
// attempt wired one and it was wrong for this board - RX/TX path
// switching goes entirely through the PI4IO expander.

// --- CONFIRMED: PI4IOE5V6408 I/O expander ---
// The radio-critical path talks to this directly (src/common/pi4io_expander.h),
// bypassing M5.begin()/M5Unified entirely: M5GFX's board autodetection for
// this very new board profile generates persistent I2C traffic that
// monopolizes the bus (confirmed - isolated raw Wire writes failed 5/5
// retries while M5.begin() was active), which is fatal for anything that
// needs deterministic expander access. Meshtastic's own firmware has no
// M5Unified dependency at all, for what looks like the same reason.
#define PIN_I2C_SDA            10
#define PIN_I2C_SCL            8
#define PI4IO_I2C_ADDR         0x43
#define PI4IO_PIN_LORA_RESET   7   // P7 - SX_NRST
#define PI4IO_PIN_RF_SWITCH    6   // P6 - SX_ANT_SW (confirmed: always "true"/enabled, not RX/TX-dependent)
#define PI4IO_PIN_LNA_ENABLE   5   // P5 - SX_LNA_EN (confirmed: always "true"/enabled)
#define PI4IO_PIN_BUTTON       0   // P0 (input) - only relevant to the hello_world role, which still uses M5Unified/M5.BtnA

// --- LoRa radio parameters ---
// Matched directly against the actual SEN66/PicoW source
// (src/lora_wan.cpp): confirmed the two projects' sync words did not
// match (we had the LoRaWAN-standard 0x34; the SEN66 node hardcodes 0x12,
// unconditionally, on every init) - almost certainly why DIO1 never
// asserted despite a confirmed real signal at the right frequency. Coding
// rate also confirmed mismatched (SEN66 never overrides RadioLib's
// begin()-default CR 4/7; we had 4/5) - lower-priority than sync word
// since a CR mismatch alone usually doesn't block detection in
// explicit-header mode, but aligned anyway. NOTE: the SEN66 side's config
// default for SF is 9, not 7 - if your runtime SF-match was done via its
// CLI rather than that default, worth confirming it actually persisted.
#define LORA_FREQ_MHZ       868.1f
#define LORA_BW_KHZ         125.0f
#define LORA_SF             7
#define LORA_CR             7      // 4/7 - matches SEN66's un-overridden RadioLib default
#define LORA_SYNC_WORD      0x12   // matches SEN66's hardcoded radio.setSyncWord(0x12)
#define LORA_PREAMBLE_LEN   8
#define LORA_TX_POWER_DBM   14     // EU868 legal limit; unused by a receive-only gateway
#define LORA_TCXO_VOLTAGE   3.0f   // confirmed from source (was wrongly 1.8V in an earlier port)