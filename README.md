# c6l-hub

Flexible LoRa/WiFi test hub: a Raspberry Pi 3 as the fixed brain, an M5Stack
Unit-C6L (ESP32-C6 + SX1262) as a swappable radio front-end.

```
firmware/   PlatformIO project for the Unit-C6L (VS Code + PlatformIO)
pi/         Flask app + scripts that run on the Pi and flash the module
```

## The two custom roles in here

`firmware/` currently scaffolds two roles you write yourself:

- `lora_node` - raw LoRa / LoRaWAN end device (RadioLib)
- `sc_gateway` - single-channel LoRaWAN packet forwarder (RadioLib)

Meshtastic and MeshCore are **not** built from this project - they're full
upstream firmware projects with their own build systems. Build those from
their own repos, then treat the resulting `.bin` the same way as the two
roles above: copy it into `pi/firmware_bins/<role>/` and flash it from the
Pi.

## Important hardware note before you write radio code

On the Unit-C6L, the SX1262's **RESET line, the RF switch, and the LNA
enable are not plain ESP32-C6 GPIOs** - they're wired through a
PI4IOE5V6408 I2C GPIO expander (address `0x43`). A community board-support
fix for this exact board (meshcore-dev/MeshCore PR #3088, Aug 2026)
documents a real crash loop caused by exactly this: if you don't
explicitly clear the expander's "output high-impedance" register for the
reset pin, the SX1262 reset line never actually gets driven and
`radio.begin()` hangs waiting on BUSY until the watchdog resets the chip.

`firmware/src/common/pi4io_expander.{h,cpp}` gives you a minimal, sourced
driver for that expander (register map cross-checked against two
independent public implementations). `firmware/include/board_config.h`
lists which pins are confirmed from M5Stack/Zephyr docs and which ones
(the SX1262's direct SPI pins, and the I2C bus pins to the expander itself)
are **not yet confirmed** in this scaffold - copy those from a known-working
reference (e.g. TheRealHaoLiu/MeshCore, `main-m5stack-unit-c6l` branch)
before trusting them.

The two role sketches currently only prove out board bring-up (serial +
RGB heartbeat + PI4IO expander ACK check) - `radioInit()` is a documented
stub, deliberately not guessing at unconfirmed pins.

## Quick start - firmware side (on macOS)

1. Open `firmware/` in VS Code with the PlatformIO extension.
2. `pio run -e lora_node` (or `-e sc_gateway`) to build.
3. Once pins are confirmed, flash directly from the Mac with
   `pio run -e lora_node -t upload` to sanity-check on the bench.
4. For the Pi-side workflow, merge the build into one flashable file:
   ```
   esptool.py --chip esp32c6 merge_bin -o lora_node.bin \
     --flash_mode dio --flash_size 16MB \
     0x0 .pio/build/lora_node/bootloader.bin \
     0x8000 .pio/build/lora_node/partitions.bin \
     0x10000 .pio/build/lora_node/firmware.bin
   ```
   (Run `pio run -e lora_node -t upload -v` once to see the exact file
   list/offsets PlatformIO used for this board - copy them from there.)
5. Copy the merged file to the Pi: `scp lora_node.bin pi@<host>:~/c6l-hub/pi/firmware_bins/lora_node/`

## Quick start - Pi side

```
cd pi
./setup.sh              # creates venv, installs Flask/pyserial/esptool
./run.sh                # starts the dashboard on http://<pi>:5000
```

Or flash from the command line instead of the web UI:

```
source pi/venv/bin/activate
./pi/flash_role.sh lora_node
```

Set `C6L_PORT` if the module doesn't show up as `/dev/ttyACM0`.
