# Context: how we got here

This documents the debugging journey behind the current working
`sc_gateway` firmware, since several of the decisions in the code look
arbitrary (or plain wrong) without the reasoning - most of them were only
reached after eliminating simpler-looking alternatives that didn't
actually work on this specific hardware.

## Hardware confirmed via schematics

Two schematic sets settled every pin mapping in `board_config.h` -
nothing there is inferred or guessed:

- `U202-SCH_Sch_M5_C6_Lora_*` - the Stamp-C6LoRa core module (ESP32-C6 +
  SX1262 + PI4IOE5V6408 expander)
- `U202-SCH_Unit_C6L_SCH_*` - the Unit-C6L carrier board (OLED, RGB LED,
  buzzer, buttons)

Key confirmed facts:

- SX1262 SPI: SCK=20, MISO=22, MOSI=21, NSS=23, DIO1=7, BUSY=19 - shares
  the bus with the OLED (SCK/MOSI only; separate CS lines)
- PI4IOE5V6408 expander (I2C, addr 0x43, SDA=10 SCL=8): P5=SX_LNA_EN,
  P6=SX_ANT_SW, P7=SX_NRST, P0=user button
- The carrier board has **two separate physical buttons**: S1 is a
  hardware reset/bootloader button wired straight to MCU_RST, entirely
  unrelated to firmware; S2 is the actual user button, wired to the
  expander's P0 with **no external pull-up** on the board - the
  expander's internal pull-up must be enabled in software or the pin
  just floats
- The OLED is a 64x48 SSD1306 over SPI, not I2C

## Why the radio path doesn't use M5Unified

M5Unified/M5GFX's board autodetection for this board profile
(`board_M5UnitC6L`) generates I2C traffic that doesn't stop - confirmed
empirically, not assumed: isolated raw `Wire` writes to the expander
failed 5/5 retries with `ESP_ERR_INVALID_STATE` while `M5.begin()` was
active. That's not occasional contention, it's the bus being
monopolized. This is almost certainly why the SX1262 would never
reliably come out of reset when brought up through M5Unified.

`src/common/pi4io_expander.{h,cpp}` is a direct, minimal driver instead,
used only by the radio path. `hello_world` still uses M5Unified for the
display/button/buzzer, where this contention is cosmetic log noise
rather than a functional blocker.

## The SX1262 bring-up sequence

Two real bugs, found in this order:

1. **Missing Hi-Z clear.** The PI4IOE5V6408 has a separate "output
   high-impedance override" register (0x07) - a pin stays electrically
   floating regardless of its direction/value setting until this is
   explicitly cleared for it. Neither M5Unified's internal setup nor our
   own early attempts did this, so no reset pulse pattern could ever
   have worked, regardless of timing.
2. **Reset pulse pattern.** Meshtastic's actual shipped driver for this
   exact board (Meshtastic-Solutions/mainline-firmware) does **not**
   pulse NRST low-then-high - it drives it high once, directly, with no
   delay. This differs from M5Stack's own official Arduino example
   (which does pulse). Meshtastic is the one independently confirmed
   working on this specific physical unit, so that's what got matched.

`pi4io_expander.cpp`'s `enableDrive()` and the no-pulse reset in
`lora_radio.cpp` are both a direct result of this.

## The OLED

Two independent bugs here, not one:

- **COM-pin misconfiguration.** Adafruit_SSD1306 has no built-in case for
  64x48 panels (only the common 128x64/128x32/96x16 sizes), so it
  applies the wrong hardware COM-pin config - this produces output
  "spread into vertical stripes," not a garbled image or nothing at all.
  Fixed by resending the `SET_COM_PINS` command (`0xDA`, value `0x12`)
  directly, right after the library's own `begin()` - the same fix
  MeshCore needed for this exact panel (their PR #1885).
- **Shared-SPI-bus interference**, suspected for a long time, turned out
  to be a red herring - see below.

## The "OLED breaks reception" false alarm

For a while, reintroducing the OLED appeared to reliably break LoRa
reception, and real effort went into suspecting SPI bus sharing
(Adafruit_SSD1306's `begin()` re-touching the shared SPI object,
resetting pin assignment). The actual cause, found later, was a **sync
word and coding rate mismatch** against the real sender (see below) - a
completely unrelated, totally-blocking bug that happened to be active
during every one of those "OLED breaks it" tests. Once the real bug was
fixed, the OLED (and the button) went back in with zero effect on
reception. Worth remembering generally: a regression that appears when
adding X is not proof X is the cause, if something else was already
broken at the same time - that comparison is confounded, not conclusive.

For reference, Meshtastic and the SEN66/PicoW project both handle
*genuine* shared-SPI-bus contention (radio vs. display/SD card) the same
way: an explicit lock that hands the bus entirely to one side at a time
(Meshtastic's `spiLock`/`ISpiLock`; SEN66's coarse-grained suspend/resume
between the radio and its LCD), not naive per-transaction sharing. If a
real SPI conflict ever does show up here, that's the pattern to follow
rather than another reassert-and-hope attempt.

## The real "no reception" bug: sync word and coding rate

The actual SEN66/PicoW source (`src/lora_wan.cpp`) was eventually checked
directly, which is what actually resolved this:

```c
radio.setSyncWord(0x12);   // hardcoded, unconditional
radio.begin();             // called with ZERO arguments -> CR defaults to 4/7
```

Our gateway was set to the LoRaWAN-standard sync word (0x34) and CR 4/5.
The sync word mismatch is the one that mattered: a LoRa receiver
abandons a packet right after the preamble if the sync word doesn't
match, before ever reaching `RX_DONE` - so `DIO1` never asserts at all,
regardless of how strong or correctly-tuned-in-frequency the signal
actually is. That matched the symptom exactly (confirmed via a direct
GPIO read of DIO1, bypassing RadioLib's interrupt handling entirely: it
stayed at 0 throughout, even with a real signal independently confirmed
present via a spectrum analyzer). Fixed by matching both values in
`board_config.h`.

Lesson for next time: matching frequency and spreading factor is
necessary but not sufficient. Sync word and coding rate need checking
too, and a spectrum analyzer can confirm a signal exists at the right
frequency but can't confirm any of these modulation-level parameters
actually match - that only shows up in the transmitting code itself.

## USB CDC freeze without a terminal attached

Native USB CDC (used for `Serial` on this board) has a transmit buffer
that only drains while a host is actively reading it. Without
`Serial.setTxTimeoutMs(0)`, a `Serial.print()`/`printf()` call can block
indefinitely once that buffer fills with no host connected - which
freezes the entire firmware, since prints happen throughout `loop()`
(the heartbeat, every RX event). This is a confirmed, documented issue
(arduino-esp32 issue #7779), not specific to this project - worth
remembering for any future ESP32 project using native USB CDC as the
primary serial port, especially anything meant to run standalone/in the
field without a permanently-attached terminal.

## USB CDC: the TX buffer, and why long lines can arrive truncated

The freeze fix above (`setTxTimeoutMs(0)`) has a flip side, read from arduino-esp32's `HWCDC.cpp`
(3.3.5 and master behave the same): the USB TX ring buffer defaults to **256 bytes** (`begin()` only
creates one if none exists), and with a zero timeout a write that doesn't fit is **truncated**, not
blocked. A line printed as many small writes can therefore lose its tail whenever the host drains
slower than the firmware prints. That's harmless for short human lines but fatal for a program
parsing JSON: a 90-byte payload is ~400 bytes as one event line. `Serial.setTxBufferSize(2048)`
before `Serial.begin()` fixes it (`setTxBufferSize()` replaces the ring; `begin()` then keeps it).

This comes from reading the source; the host test only verifies the call order. It has not been
observed on the board. (It also applies to the human-readable lines printed by earlier firmware.)

## Flashing: how it works now, and what to do if uploads break again

**Now (confirmed working, October 2026):** `platformio.ini` uses
`platform = https://github.com/pioarduino/platform-espressif32/archive/refs/heads/develop.zip` and you flash
with plain `pio run -e sc_gateway -t upload`. No extra tooling, no manual esptool, no extra venv. (`-e` matters:
`default_envs` is `hello_world`.) The official espressif32 platform has no Arduino support for the ESP32-C6,
hence the pioarduino fork.

**History.** For a while every upload crashed *after a successful build* with a Python traceback from
esptool's progress-bar code (`AttributeError` ... `_get_progress_print_file`), because pioarduino bundled
esptool 5.4.0 with a bug in it (`pioarduino/platform-espressif32#539`). The fix is `pioarduino/esptool#26`
(its author confirmed it addresses exactly that error); it reached `develop`, and uploads work again.
`develop` is a moving target, so this can regress. The firmware itself was never at fault.

**The esptool version number does not tell you whether you have the bug.** The working setup below
still reports `tool-esptoolpy 5.4.0` - the same number as the broken one - so pioarduino's esptool was
fixed without a version bump. Recognise the bug by its symptom, never by the version.

**Known-good toolchain**, recorded October 2026 from `pio pkg list --global` while uploads worked. (That command
lists everything PlatformIO has ever installed on the machine - AVR, Teensy, RP2040, older ESP32 platforms - and
only these lines belong to this project.)

| Package | Version | Source |
|---|---|---|
| platform `espressif32` | `55.3.312+develop` | pioarduino `develop.zip` (what `platformio.ini` points at) |
| `framework-arduinoespressif32` | `3.3.12` | arduino-esp32 `master.zip` |
| `framework-arduinoespressif32-libs` | `5.5.5` (IDF 5.5, build `b774170ff46`) | esp32-arduino-lib-builder |
| `toolchain-riscv32-esp` | `14.2.0+20260121` | |
| `tool-esptoolpy` | `5.4.0` | pioarduino's own copy, kept under `~/.platformio/tools/` |
| `tool-scons` | `4.11.1` | pioarduino |

Two things to know about it: both the platform (`develop.zip`) and the Arduino core (`master.zip`) are moving
targets, so "the same `platformio.ini`" does not guarantee the same build next year. And `stable` currently
reports the same base number (`55.3.312`); whether `stable` now also has the esptool fix has not been checked.
Libraries (RadioLib, M5Unified, the Adafruit ones) float within the `^` ranges in `platformio.ini` and don't appear
in the global list - `pio pkg list -e sc_gateway`, run inside `firmware/`, shows the versions actually in use.

**If it comes back:**
- Check first whether it is the same bug: build succeeds, upload dies with that `AttributeError`.
- To get back to a known state, pin the platform instead of tracking `develop`: replace `refs/heads/develop.zip`
  with `<commit-sha>.zip` of a commit you know worked. The table above does **not** identify the commit behind
  `55.3.312+develop`, so that exact build can't be pinned after the fact. If you want that option next time, look up
  a commit on GitHub (pioarduino/platform-espressif32 -> commits) after a successful upload and write its sha here.
- A `platform_packages = ...tool-esptoolpy@...` override to pin an older esptool does **not** work: pioarduino
  manages esptool in its own internal Python venv, so there's no classic tool package to attach to
  (`UnknownPackageError`).
- The tested workaround is to let PlatformIO build, then flash the merged image it produces with an esptool
  installed separately. `pio run -e sc_gateway` writes `.pio/build/sc_gateway/firmware.factory.bin`
  (bootloader + partition table + app in one file), which goes to address 0x0:

  ```
  python3 -m venv .venv-esptool && .venv-esptool/bin/pip install "esptool==5.3.0"
  .venv-esptool/bin/esptool --chip esp32c6 --port /dev/cu.usbmodemXXXX --baud 1500000 \
      --before default-reset --after hard-reset \
      write-flash -z --flash-mode keep --flash-freq keep --flash-size keep \
      0x0 .pio/build/sc_gateway/firmware.factory.bin
  ```

  5.3.0 is a plain PyPI esptool (unrelated to pioarduino's fork) that this workaround was proven with. Add `firmware/.venv-esptool/` to
  `.gitignore` if you recreate that venv. The script below automates exactly this; save it as
  `firmware/flash_manual.sh` and `chmod +x` it (usage: `./flash_manual.sh sc_gateway [port]`):

```bash
#!/usr/bin/env bash
# Build an env with PlatformIO, then flash it with a known-working esptool
# binary instead of pioarduino's currently-broken one.
#
# Why this exists: pioarduino's bundled esptool (5.4.0) crashes on every
# upload with an AttributeError in its progress-bar code
# (pioarduino/platform-espressif32#539). PlatformIO's own build still
# succeeds and produces a single pre-merged, ready-to-flash file -
# .pio/build/<env>/firmware.factory.bin - as a normal part of compiling,
# regardless of whether the (broken) upload step runs afterward. So: let
# PlatformIO compile, flash with a working esptool from elsewhere.
#
# esptool 5.3.0 (confirmed on PyPI, matching the version already proven
# working here) is installed into a project-local Python venv
# (.venv-esptool/, created automatically on first run) rather than relying
# on Arduino IDE's board-package copy - self-contained, independent of
# whether Arduino IDE or any board package is installed at all.
#
# Usage: ./flash_manual.sh <env> [port]
#   ./flash_manual.sh sc_gateway
#   ./flash_manual.sh sc_gateway /dev/cu.usbmodem213101
#
# Override the esptool binary if you want a different one instead of the
# auto-managed venv copy:
#   ESPTOOL_BIN=/path/to/esptool ./flash_manual.sh sc_gateway
set -e
cd "$(dirname "$0")"

ENV="${1:?Usage: $0 <env> (lora_node|sc_gateway) [port]}"
PORT="${2:-/dev/cu.usbmodem213101}"
MERGED_BIN=".pio/build/$ENV/firmware.factory.bin"

VENV_DIR=".venv-esptool"
ESPTOOL_VERSION="5.3.0"
VENV_ESPTOOL="$VENV_DIR/bin/esptool"

if [ -n "$ESPTOOL_BIN" ]; then
    ESPTOOL="$ESPTOOL_BIN"
else
    if [ ! -x "$VENV_ESPTOOL" ]; then
        echo "Setting up a local esptool $ESPTOOL_VERSION (one-time, in $VENV_DIR)..."
        python3 -m venv "$VENV_DIR"
        "$VENV_DIR/bin/pip" install --quiet "esptool==$ESPTOOL_VERSION"
    fi
    ESPTOOL="$VENV_ESPTOOL"
fi

if [ ! -x "$ESPTOOL" ]; then
    echo "esptool not found (or not executable) at: $ESPTOOL"
    echo "Delete $VENV_DIR and re-run to retry the automatic setup,"
    echo "or set ESPTOOL_BIN to point at any other working esptool binary."
    exit 1
fi

echo "Building $ENV with PlatformIO..."
pio run -e "$ENV"

if [ ! -f "$MERGED_BIN" ]; then
    echo "Expected merged binary not found at $MERGED_BIN"
    echo "Check the build output above for the actual merged .bin filename/path."
    exit 1
fi

echo "Flashing $MERGED_BIN via $ESPTOOL on $PORT ..."
"$ESPTOOL" --chip esp32c6 --port "$PORT" --baud 1500000 \
    --before default-reset --after hard-reset \
    write-flash -z --flash-mode keep --flash-freq keep --flash-size keep \
    0x0 "$MERGED_BIN"
```

**Saved settings and flashing.** Saved radio settings live in NVS at 0x9000-0xDFFF (`default_8MB.csv`).
A *merged-image* flash like the workaround above **wipes them**: `esptool merge-bin` pads the gaps between
components with 0xFF (checked directly with esptool 5.3.0), and the merged image covers that region. A normal
`pio run -t upload` writes the bootloader, partition table and app at their own offsets, so it should leave
them alone - not yet confirmed on this board. To check once: `save`, re-upload, then `info` should still say
`settings: saved`. NVS is also where the settings go if the boot fallback clears a bad set (see below).

## Runtime radio settings

`board_config.h`'s `LORA_*` macros are only boot defaults; the live state is a `LoraConfig` (in
`lora_config.{h,cpp}`) that the CLI, the OLED and the radio all read through `loraActiveConfig()`.

- **Fields are plain `int`/`float`, validated at full width before narrowing.** Values come from text;
  narrowing first would turn `sf 300` into a valid-looking 44.
- **Ranges come from RadioLib 7.3.0's own source, not memory.** `SX1262::setFrequency` enforces 150-960 MHz;
  `setBandwidth` accepts the ten discrete values by switching on `bw/2`; spreading factor is 5-12;
  the coding-rate check accepts 4-8 but 4 maps to an invalid chip value, so the CLI allows 5-8;
  LoRa preamble length is not range-checked at all (6-65535 is our own sanity bound).
  `setSyncWord(uint8_t)` builds the 0x1424-style register pair itself. A first guess at some of these
  (e.g. that `readBuffer()` was public) had been wrong before, which is why it was checked.
- **Low-data-rate optimisation needs no handling:** RadioLib recomputes it on every modulation change
  (`ldroAuto`, on when symbol time >= 16 ms). Settings are applied bandwidth -> SF -> coding rate so the
  last call derives it from the final SF and BW.
- **Applying is all-or-nothing:** standby, set each value, and on any refusal re-apply the previous
  settings; always re-arm receive afterwards and drop any stale RX flag.
- **Boot fallback:** if the radio refuses *saved* settings, they're cleared and the defaults used, so a
  bad saved value can't leave the board permanently deaf.

## The monitor app (`monitor/`)

Design choices that aren't obvious from the code:

- **JSON mode with hex payloads** rather than parsing the human lines: lossless (the human line shows
  non-printable bytes as `.`), and no quoting problems with the quotes inside JSON payloads. A **failed**
  attempt carries no `len`/`hex` on purpose - its bytes were shown (by earlier testing) not to be real
  received data. Every event carries the settings it was received under, so a stored record stays
  meaningful after settings change. The app sends `json on` itself on connect and after every boot.
- **Storage:** SQLite, with each numeric payload field in a `readings(packet_id, key, value)` table, so
  graphing any field is an indexed query and a new field in a payload needs no schema change. The Log
  CSV gets one column per field.
- **Polling, not SSE/websockets:** the UI polls `/api/live` once a second. Packets arrive every ~30 s; the
  simplicity (and no long-lived connections to clean up) is worth more than the latency.
- **Reconnecting:** the ESP32's USB port vanishes and reappears on every reset, so the reader thread keeps
  retrying the wanted port. It treats the boot banner as the signal to re-initialise, because JSON mode
  doesn't survive a reboot. DTR/RTS are held low on open (some adapters reset the board on a toggle).
- **macOS lists each USB serial device twice** (`/dev/cu.X`, `/dev/tty.X`); only `cu.X` is shown, otherwise
  one board looks like two candidates and autodetect gives up.
- **`command()`'s timeout is a total budget** including time waiting for another command, and reports `busy`
  rather than hanging a UI request behind the init handshake.
- **Security posture:** payloads are untrusted input from anyone's transmitter. The UI builds DOM with
  `textContent` only (never `innerHTML`), the page sends a strict CSP, node names are restricted to
  `[A-Za-z0-9_.:-]`, writes require `Content-Type: application/json` (a cross-site HTML form can't send
  it, so a malicious page can't blindly hit e.g. `/api/config/defaults`), and it binds to localhost.
  There is no authentication - `--host 0.0.0.0` exposes everything.
- **Preset/graph names travel in the body, never the URL:** a `/` in a name broke routing.

## Traps worth remembering (found while testing the monitor)

- **pyserial's `open()` ends by discarding any input already waiting.** Anything a board prints in the
  instant a port opens is lost. Harmless here (the app handshakes on connect and on the boot banner), but
  it made a too-fast simulator randomly lose its banner - a real board needs hundreds of ms to boot.
- **Closing an fd/socket while another thread is blocked reading it does not hang up the peer** (sockets
  and ptys alike). Test fakes need a pollable reader, and `shutdown()` before `close()`.
- **State that readers poll must change together:** `connected=False` and its reason were set a moment
  apart, so a status poll could see "disconnected, no reason". Now set under one lock.
- **A slow older response can overwrite a newer chart** unless each request carries a sequence number and
  only the latest may draw.
- A `<script>` payload parsed as a node *named* "script" under the first, looser tag pattern.

## How this was verified, and what wasn't

- `firmware/test/host/run.sh`: the real `sc_gateway/main.cpp` and `lora_config.cpp` compiled on a computer
  against stand-ins for Arduino/RadioLib/Preferences (104 + config checks): every command's grammar and
  errors, state, save/load/boot fallback, both RX output formats, TX-buffer-before-begin ordering. It also
  captured the firmware's real output lines as fixtures for the app's tests.
- `monitor/tests/`: backend, serial link (against those fixtures, the simulator over TCP, and a pty
  standing in for a real device node), API, and the real page in jsdom with hostile payloads planted.
  Several tests were checked by deliberately breaking the code to confirm they fail.
- **Not verified:** anything on the actual board (the CLI changes, JSON mode, the TX buffer behaviour) or
  in a real browser (chart pixels, layout, dark mode). The first run on real hardware may well turn up
  things the simulator didn't predict.