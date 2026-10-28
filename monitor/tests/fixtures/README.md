`firmware_lines.txt` holds lines printed by the real sc_gateway firmware code
(RX events, `get`, command replies), captured by running main.cpp's own CLI/RX
code on a host with mocked hardware. They pin the serial protocol the app
parses: if the firmware's output format changes, regenerate these and the app's
tests will show what moved.
