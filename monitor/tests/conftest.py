import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from c6lmon.device import Device          # noqa: E402
from c6lmon.sim import SimDevice          # noqa: E402
from c6lmon.store import Store            # noqa: E402


def load_fixtures():
    """Lines captured verbatim from the firmware's own code (see tests/fixtures/README)."""
    out = {}
    text = (Path(__file__).parent / "fixtures" / "firmware_lines.txt").read_text()
    for line in text.splitlines():
        key, _, val = line.partition("=")
        out[key] = val
    return out


def wait_for(cond, timeout=8.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(step)
    return None


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "t.db"))


@pytest.fixture
def idle_device(store, tmp_path):
    """A Device that is never connected - for feeding it lines directly."""
    d = Device(store, log_dir=str(tmp_path / "logs"))
    yield d
    d.stop()


@pytest.fixture
def sim():
    s = SimDevice(interval=0.25, seed=1, fail_pct=0).start()
    yield s
    s.stop()


@pytest.fixture
def live_device(store, sim, tmp_path):
    """A Device connected to the simulator, past its init handshake."""
    d = Device(store, log_dir=str(tmp_path / "logs"))
    d.start()
    d.connect(sim.url)
    assert wait_for(lambda: d.connected and d.config and d.json_mode), "device never completed its handshake"
    yield d
    d.stop()
