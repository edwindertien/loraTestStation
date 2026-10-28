# c6l-hub

Flexible LoRa/WiFi test hub: a Raspberry Pi 3 as the fixed brain, an M5Stack
Unit-C6L (ESP32-C6 + SX1262) as a swappable radio front-end.

```
firmware/   PlatformIO project for the Unit-C6L (VS Code + PlatformIO)
monitor/    Flask app: live view, radio settings, logging, reports, graphs - talks to the
            board over serial (see "Monitor app" below)
pi/         Flask app + scripts that run on the Pi and flash the module
            (built early in the project; not part of the recent work below)
```

## Current status

`sc_gateway` is a **working single-channel LoRa receiver**, confirmed
receiving and correctly decoding real sensor packets from a separate
Raspberry Pi Pico W + SX1262 node (the "SEN66" project - see
`context.md` for how the two projects relate and what it took to get
them talking to each other). Its radio settings can be changed live from
the serial CLI or the monitor app. It is not yet a real LoRaWAN gateway
(no MAC-layer decode, no packet-forwarder uplink to a network server) -
see "Where this is headed" below.

- `hello_world` - board bring-up / peripheral test scaffold (display,
  button, buzzer, RGB LED) - no radio
- `sc_gateway` - **the working one**: brings up the SX1262, listens
  continuously, decodes and displays received packets (serial CLI + OLED
  + button + RGB LED)
- `lora_node` - stub only; brings the radio up and idles. Transmit /
  LoRaWAN join-and-uplink logic is not implemented yet. Not tested on hardware since the radio rework
  (it compiles in the host checks, nothing more)

## Hardware

M5Stack Unit-C6L: ESP32-C6 + SX1262, a 64x48 SPI OLED, a PI4IOE5V6408 I2C
GPIO expander (handles the SX1262's reset/RF-switch/LNA-enable lines and
the user button - none of those are plain ESP32 GPIOs), an RGB LED, and a
buzzer. All pins are confirmed directly from the module and carrier
schematics (see `context.md`) - nothing in `board_config.h` is a guess.

**This firmware deliberately does not use M5Unified for the radio path.**
M5GFX's board autodetection for this board profile generates I2C traffic
that monopolizes the bus badly enough to block reliable access to the
expander the radio depends on. `src/common/pi4io_expander.{h,cpp}` is a
small, direct driver instead - see `context.md` for the full story and
why this matters if you're tempted to add M5Unified back for something
else. `hello_world` still uses M5Unified for the display/button/buzzer,
where this is cosmetic log noise rather than a functional blocker.

Meshtastic and MeshCore are **not** built from this project - they're
full upstream firmware projects with their own build systems, referenced
here only as working hardware/driver reference points (see `context.md`).

## Building and flashing

Requires [PlatformIO](https://platformio.org/) (VS Code extension or CLI).

```
cd firmware
pio run -e sc_gateway -t upload        # build + flash  (-e matters: the default env is hello_world)
```

`platformio.ini` uses the pioarduino fork of the espressif32 platform (the official one has no Arduino
support for the ESP32-C6), tracking its `develop` branch. If an upload ever fails with a Python
`AttributeError` *after* a successful build, that's a known esptool bug that was fixed upstream once
already; `context.md` ("Flashing") has the history, how to pin the platform, and a tested workaround.

## Configuring the radio

The settings are **runtime state**. `firmware/include/board_config.h` only holds the
boot defaults:

```c
#define LORA_FREQ_MHZ       868.1f
#define LORA_BW_KHZ         125.0f
#define LORA_SF             7
#define LORA_CR             7      // 4/7
#define LORA_SYNC_WORD      0x12
#define LORA_PREAMBLE_LEN   8
```

They can be changed on the running board (CLI below, or the monitor app's Settings tab) and
kept across reboots with `save`. Saved settings live in the board's flash (NVS) and win over
`board_config.h` until you run `defaults`; `info` and the boot line say which one is active.
If the radio ever refuses saved settings at boot, the firmware falls back to the defaults and
clears them, so a bad saved value can't leave the board deaf.

A merged-image flash (the old workaround in `context.md`) erases saved settings; a normal
`pio run -t upload` writes the parts at their own offsets and should keep them (to check once: `save`,
re-upload, then `info` should still say `settings: saved`).

These **must exactly match** whatever's transmitting, or you'll get silence (wrong sync word or
bandwidth - the receiver won't even detect a preamble) or a stream of CRC failures (right
channel, wrong coding rate/SF - detected but garbled). The defaults above are confirmed
correct against the actual SEN66/PicoW sender's source - see `context.md` for how a
sync-word/coding-rate mismatch was eventually tracked down after a lot of otherwise-inexplicable
silence.

## Using sc_gateway

**Serial CLI** (115200 baud, one command per line, case-insensitive):

| Command | Does |
|---|---|
| `help` | list commands |
| `stats` | RX success/fail counts, uptime |
| `info` | radio init status, active radio settings, and where they came from |
| `last` | reprint the most recent RX attempt (success or fail) |
| `raw on` / `raw off` | also show hex bytes alongside the normal summary (default: off) |
| `json on` / `json off` | print each RX attempt as one JSON line, for programs (default: off) |
| `get` | all radio settings as one JSON line |
| `freq <MHz>` | frequency, e.g. `freq 868.3` (150-960) |
| `ch <0-7>` | EU868 channel: 0-2 = 868.1/.3/.5, 3-7 = 867.1/.3/.5/.7/.9 |
| `bw <kHz>` | 7.8 10.4 15.6 20.8 31.25 41.7 62.5 125 250 500 |
| `sf <5-12>` | spreading factor |
| `cr <5-8>` | coding rate: 5 = 4/5 ... 8 = 4/8 |
| `sync <hex>` | sync word, e.g. `sync 12` (0x12 "private", 0x34 LoRaWAN) |
| `pre <6-65535>` | preamble length in symbols |
| `save` | keep the current radio settings across reboots |
| `defaults` | back to `board_config.h`, and forget saved settings |

Settings commands apply immediately (a packet being received at that instant is lost), validate
before touching the radio, and answer `ok ...` or `err ...` - one line, easy for a script to read.
If RadioLib still refuses a combination, the previous settings are restored.

A received message prints as `[sc_gateway] <uptime>s  RX <n> bytes  RSSI <x> dBm  SNR <x> dB
"<decoded ascii>"`. A failed one shows the error code and RSSI/SNR without a decode - the bytes
of a failed packet are not real data (see `context.md`), so none are shown. The idle heartbeat
(`[sc_gateway] listening...`) prints every 2 s regardless.

**JSON mode** (`json on`) replaces that line with one JSON object per attempt, e.g.

```
{"ev":"rx","up_ms":12345,"ok":true,"err":0,"rssi":-4.0,"snr":12.8,"len":90,"hex":"3C53...","freq":868.100,"bw":125,"sf":7,"cr":7,"sync":"0x12"}
```

The payload is hex (lossless, no quoting problems), a failed attempt has no `len`/`hex`, and every
event carries the settings it was received under. It resets to off at boot; the monitor app turns it on.

**OLED** shows firmware name, frequency, spreading factor, and running OK/fail counts, one per
line, always reflecting the radio's actual (possibly changed) settings.

**Button** currently resets the OK/fail counters (the one useful action with a single screen) -
straightforward to repurpose into a menu/screen cycle once there's a second screen to switch to
(a last-packet detail view and a raw/debug view already exist in `oled_display.{h,cpp}` but aren't
wired up to the button yet).

**RGB LED** flashes white on a successfully decoded packet, dim cyan during idle listening.

## Monitor app

> Status: developed and tested against its built-in simulator, a pty standing in for a serial
> device node, and the real page in jsdom. A first run against the actual board is still to do -
> delete this note once that's happened.

A small web app (Flask + pyserial, SQLite for storage, Chart.js vendored so it works with no
internet) that watches the board over serial, stores everything it hears, and lets you change
the radio settings from a browser.

```
cd monitor
./run.sh                      # first run creates a venv; then http://localhost:5001
./run.sh --sim                # no hardware: a built-in simulated board
./run.sh --port /dev/cu.usbmodem1101    # pick the port yourself
```

It autodetects the board if exactly one ESP32 is attached; otherwise type or pick a port in the
top bar (any pyserial URL works, e.g. `socket://host:port` for a remote serial server). `C6L_PORT`
is honoured too. Only one program can own a serial port at a time, so **Disconnect** (or quit)
before flashing or opening another serial monitor. It reconnects by itself when the board resets
or is replugged.

| Tab | Does |
|---|---|
| **Live** | connection state, active radio settings, board heartbeat, the last decoded packet as tiles, a table of the latest packets, the device console, and a box to send any CLI line |
| **Settings** | edit and apply frequency / bandwidth / SF / coding rate / sync word / preamble, with an EU868 channel picker; shows per-setting results and the device's own error text; save to / restore defaults on the device; named presets (two built in: the SEN66 link and standard LoRaWAN) |
| **Log** | every packet ever received, filterable by time, node, status and text, with per-packet detail (hex, fields, the radio settings it was received under); CSV (one column per payload field) and JSON export; raw device log download; delete by age |
| **Reports** | per period: received / decoded / failed, success %, RSSI and SNR min/avg/max, and per node the typical interval, **missed packets** (gaps in the stream) and completeness, plus an activity timeline; CSV download |
| **Graphs** | any number of series: RSSI, SNR, packet counts, or **any numeric field in any payload** (`co2`, `t`, ... - discovered automatically), per node, avg/min/max/sum per time bucket, left or right axis, line or points, any period; save and reload graph configs; PNG download; optional auto-refresh |

Data lives in `monitor/data/` (`c6l.db`, and a rotating `logs/device.log`).

Things worth knowing:

- Received text is untrusted (anyone can transmit). Nothing received is ever rendered as HTML, and
  the page ships a strict Content-Security-Policy.
- It serves on `127.0.0.1` by default. `--host 0.0.0.0` makes it reachable from other machines, but
  there is **no login**: anyone who can reach it can change the radio settings and delete data.
- The board does the receiving and keeps no history; the app only listens. Packets heard while the
  app isn't running (or the port is closed) are not stored anywhere.
- Opening a serial port can restart some boards. The app handles that (it re-initialises when it sees
  the boot banner), but a restart returns the board to its saved settings (or the defaults) and turns
  `json` mode off again - save the settings you want to keep.

## Tests

```
firmware/test/host/run.sh      # firmware logic on your computer, no board: CLI, RX formats, config module
monitor/tests/                 # see monitor/tests/README.md: backend, serial link, API, and a jsdom UI test
```

The firmware host tests compile the real sources against small stand-ins for Arduino/RadioLib, so
they catch logic and typo errors but not RadioLib API drift or anything about real hardware - the
PlatformIO build and the board remain the final check.

## Where this is headed

- Give the button a second (and third) screen to cycle to, now that more
  than one exists
- Run the monitor against the real board and fix whatever the simulator didn't predict
- Implement `lora_node`'s transmit / LoRaWAN join logic (currently a stub)
- Add the Semtech UDP packet-forwarder layer so `sc_gateway` can actually
  forward to a real network server (ChirpStack/TTN) - right now it's a
  PHY-level receive-and-decode tool, not a real gateway
- **Multi-channel**: genuinely simultaneous multi-channel reception (like
  a real LoRaWAN gateway) needs different hardware - a Semtech
  concentrator chip (SX1301/1302/1303) or multiple discrete SX1262
  radios, since one SX1262 can only be tuned to one channel at a time. A
  software-only channel-hopping approach is possible on the existing
  hardware but will miss most traffic on channels it isn't currently
  tuned to - fine for controlled bench testing against a known node, not
  equivalent to a real gateway's coverage

## Pi side

**Parked.** `pi/` is a small firmware *flasher* (a Flask page with a Flash button per `.bin`, running
esptool over USB) from early in the project. It has no monitoring, settings or data, so it overlaps
nothing in the monitor app, and it hasn't been verified against the current firmware or toolchain.
The monitor app already covers the "Pi as the fixed brain" idea: it needs only Flask and pyserial, so it
should run on the Pi as it does on a Mac (not yet tried there).

```
cd pi
./setup.sh              # creates venv, installs Flask/pyserial/esptool
./run.sh                # starts the dashboard on http://<pi>:5000
```

Or flash from the command line instead of the web UI:

```
source pi/venv/bin/activate
./pi/flash_role.sh sc_gateway
```

Set `C6L_PORT` if the module doesn't show up as `/dev/ttyACM0`. This flasher and the monitor
both need the serial port, so use one at a time.