"""The monitor: every frame the link sends or receives, locally and through the server's state."""

import tempfile
import time
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from expect import has, line_with

from mvconnect import state as st
from mvconnect.frame import KIND_REPLY, KIND_REQUEST, Address, Frame
from mvconnect.masterbus import MasterBus
from mvconnect.system import System
from test_pages import SHUNT, FakeShunt

OTHER = Address(0x0A, 0x1062B)


def test_a_tap_sees_what_is_sent_and_received():
    bus = MasterBus(FakeShunt(), reply_timeout=0.02, retries=0).open()
    seen = []
    remove = bus.tap(lambda frame, sent: seen.append((frame.kind, sent)))
    bus.read_number(SHUNT, 0x0B)
    remove()
    bus.read_number(SHUNT, 0x0B)
    assert seen == [(KIND_REQUEST, True), (KIND_REPLY, False)]


@pytest.mark.unix
def test_the_state_streams_the_traffic():
    with tempfile.TemporaryDirectory(prefix="mv-", dir="/tmp") as d:
        bus = MasterBus(FakeShunt(), reply_timeout=0.02, retries=0).open()
        system = System(bus, use_cache=False)
        system.devices(0.02)
        server = st.StateServer(system, Path(d) / "state.sock", timeout=0.02)
        server.bind()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        monitor, other = st.connect(server.path), st.connect(server.path)
        seen, started = [], threading.Event()

        def watch():
            frames = monitor.traffic()
            started.set()
            for t, frame, sent in frames:
                seen.append((frame.kind, frame.address, sent))
                if len(seen) == 2:
                    return
        thread = threading.Thread(target=watch, daemon=True)
        thread.start()
        started.wait(1)
        deadline = time.monotonic() + 1
        while not bus._taps and time.monotonic() < deadline:  # the server listens before anything is sent,
            time.sleep(0.01)                                   # so the first frame seen is a request
        for _ in range(50):
            other.login_level(other.device("msh"))
            thread.join(0.05)
            if not thread.is_alive():
                break
        server.stop()
        monitor.close(), other.close()
        assert seen == [(KIND_REQUEST, SHUNT, True), (KIND_REPLY, SHUNT, False)]


class Canned(System):
    """A system whose traffic is a fixed list of frames, the way a short session would see it."""

    FRAMES = [(100.0, Frame(KIND_REQUEST, SHUNT, bytes([0x08, 0x0B])), True),
              (100.25, Frame(KIND_REPLY, SHUNT, bytes([0x08, 0x0B, 0x01, 0x02])), False),
              (100.5, Frame(0x30, OTHER, b"\x00\x00"), False)]

    def traffic(self):
        yield from self.FRAMES


@pytest.fixture
def tool(monkeypatch):
    import mvconnect.cli as cli

    @contextmanager
    def open_system(refresh=False):
        yield Canned(MasterBus(FakeShunt(), reply_timeout=0.02, retries=0).open(), use_cache=False)
    monkeypatch.setattr(cli, "open_system", open_system)
    monkeypatch.setattr(cli.sys, "argv", ["mvconnect", "monitor"])
    return cli.Tool()


def test_every_frame_is_a_line(tool, capsys):
    tool.monitor(timeout=0.02)
    err = capsys.readouterr().err
    assert line_with(err, "tx", "1b:0cad9", "08 0b", "version")
    assert line_with(err, "rx", "1b:0cad9", "08 0b 01 02", "MSH House Bank")
    assert line_with(err, "rx", "0a:1062b") and line_with(err, 3, "frames")


@pytest.mark.parametrize("given", ["msh", "1b:0cad9", ["msh"], "msh, 1b:0cad9"])
def test_filter_keeps_the_named_devices(tool, capsys, given):
    tool.monitor(filter=given, timeout=0.02)
    err = capsys.readouterr().err
    assert line_with(err, "1b:0cad9") and not line_with(err, "0a:1062b") and line_with(err, 2, "frames")


def test_filter_takes_several_comma_separated(tool, capsys):
    tool.monitor(filter="msh,0a:1062b", timeout=0.02)
    assert line_with(capsys.readouterr().err, 3, "frames")


def test_a_repeated_filter_is_an_error_not_a_loss(tool, monkeypatch, capsys):
    import mvconnect.cli as cli
    monkeypatch.setattr(cli.sys, "argv", ["mvconnect", "monitor", "--filter", "msh", "--filter", "apr"])
    with pytest.raises(SystemExit):
        tool.monitor(filter="apr", timeout=0.02)
    assert line_with(capsys.readouterr().err, "ERROR", "--filter")


def test_a_bare_filter_is_an_error_not_a_device_named_true(tool, monkeypatch, capsys):
    import mvconnect.cli as cli
    monkeypatch.setattr(cli.sys, "argv", ["mvconnect", "monitor", "--filter"])
    with pytest.raises(SystemExit):
        tool.monitor(filter=True, timeout=0.02)
    err = capsys.readouterr().err
    assert line_with(err, "ERROR", "--filter") and not has(err, "True")


def test_an_unknown_device_in_the_filter_is_an_error(tool, capsys):
    with pytest.raises(SystemExit):
        tool.monitor(filter="nosuch", timeout=0.02)
    assert line_with(capsys.readouterr().err, "ERROR", "nosuch")


def test_ctrl_c_stops_the_monitor_and_says_so(tool, monkeypatch, capsys):
    def interrupted(self):
        yield from Canned.FRAMES[:2]
        raise KeyboardInterrupt
    monkeypatch.setattr(Canned, "traffic", interrupted)
    tool.monitor(timeout=0.02)
    assert line_with(capsys.readouterr().err, "WARNING", "Ctrl-C", "monitor stops", 2, "frames")
