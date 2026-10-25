"""The server's bus state: the open link with its devices identified, served to the commands it runs."""

import tempfile
import threading
import time
from pathlib import Path

import pytest

from expect import has, line_with, mentions

import test_pages
from mvconnect import state as st
from mvconnect.hidlink import LinkError
from mvconnect.masterbus import MasterBus
from mvconnect.pages import PAGE_BY_NAME, PageTable
from mvconnect.system import AddressError, System
from test_pages import CFG_ITEMS, COUNTS, SHUNT, VALUES, FakeShunt

pytestmark = pytest.mark.unix


@pytest.fixture
def short_path():
    """A directory short enough for a Unix socket path: macOS allows about a hundred bytes."""
    with tempfile.TemporaryDirectory(prefix="mv-", dir="/tmp") as d:
        yield Path(d)


def serve(path, **options):
    bus = MasterBus(FakeShunt(), reply_timeout=0.02, retries=0).open()
    system = System(bus, use_cache=False)
    system.devices(0.02)
    server = st.StateServer(system, path, timeout=0.02, **options)
    server.bind()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, bus, thread


@pytest.fixture
def served(short_path):
    server, bus, thread = serve(short_path / "state.sock")
    try:
        yield server, bus
    finally:
        server.stop()
        thread.join(5)
        bus.close()


def enumerations(bus):
    return sum(1 for f in bus.link.sent if f.kind == 0x0A)


def version_reads(bus):
    return sum(1 for f in bus.link.sent if f.kind == 0x0E and f.data[:1] == b"\x08" and f.data[1:2] in
               (b"\x1c", b"\x1d", b"\x1e", b"\x1f"))


def test_no_state_is_an_error_not_a_fallback(short_path, monkeypatch):
    with pytest.raises(LinkError):
        st.connect(short_path / "nothing.sock")
    monkeypatch.setenv(st.STATE_VARIABLE, str(short_path / "gone.sock"))
    with pytest.raises(LinkError):  # the server said where its state is; it must answer
        with st.open_system():
            pass


def test_bind_takes_the_socket_over(short_path):
    path = short_path / "state.sock"
    path.write_bytes(b"")  # left by an earlier server
    server = st.StateServer(System(None), path)
    server.bind()
    try:
        assert path.is_socket()
    finally:
        server.listener.close()


def test_the_devices_are_identified_once(served):
    server, bus = served
    system = st.connect(server.path)
    try:
        before = enumerations(bus)
        assert [d.product_name for d in system.devices()] == ["MSH House Bank"]
        device = system.device("house bank")  # resolved here, from the devices the server knows
        with pytest.raises(AddressError):
            system.device("nobody")
        assert enumerations(bus) == before
        cfg = PAGE_BY_NAME["config"]
        table = system.tables(device, [cfg])["config"]
        assert table.values[2].options == ["Gel", "AGM", "Wet", "Li-ion"] and table.version == 7
        # Reads and writes are single conversations: no page version is read along with them.
        versions = version_reads(bus)
        assert system.read_values(device, table, [0, 1]) == {0: 12.0, 1: 510.0}
        assert system.write_value(device, table, 0, 36.0) == 36.0 and VALUES[0x37][0] == 36.0
        VALUES[0x37][0] = 12.0
        with pytest.raises(ValueError):
            system.write_value(device, table, 0, 99.0)  # the device refuses
        assert system.read_text(device, table, 7) == (219, "MSH House Bank")
        assert system.read_texts(device, table, [0, 7]) == {7: "MSH House Bank"}
        assert system.write_text(device, table, 7, "MSH Bow") == "MSH Bow"
        bus.write_string(SHUNT, 219, "MSH House Bank")
        assert version_reads(bus) == versions
        assert system.resolve_value(device, "battery type", "config")[1].index == 2
    finally:
        system.close()


def test_a_device_value_written_through_the_server_is_its_entry(served):
    server, bus = served
    system = st.connect(server.path)
    try:
        device = system.device("house bank")
        table = system.tables(device, [PAGE_BY_NAME["config"]])["config"]
        test_pages.ADDRESS_FRAMES.clear()
        written = system.write(device, table, table.values[5], "msh", timeout=0.02)  # Event 1 target
        assert written.after == str(SHUNT) and VALUES[0x37][5] == 1.0 and not test_pages.ADDRESS_FRAMES
        test_pages.CFG_ITEMS[5][(0x20, 1)] = test_pages.NO_ENTRY  # the entries are read afresh, through the server
        with pytest.raises(AddressError, match=mentions("not among")):
            system.write(device, table, table.values[5], "msh", timeout=0.02)
    finally:
        system.close()


def test_a_device_that_does_not_answer_is_a_timeout_through_the_server_too(served):
    import dataclasses
    server, bus = served
    system = st.connect(server.path)
    try:
        silent = dataclasses.replace(system.device("house bank"), address=st.Address(0x1B, 0x00001))
        with pytest.raises(TimeoutError):
            system.login_level(silent)
        table = system.tables(system.device("house bank"), [PAGE_BY_NAME["config"]])["config"]
        assert system.read_texts(silent, table, [7]) == {}  # a timeout, which read_texts() warns of and leaves out
    finally:
        system.close()


def test_the_server_has_one_conversation_on_the_bus_at_a_time(served):
    server, bus = served
    inside, most = [0], [0]
    read_values = server.system.read_values

    def slow(*args, **options):
        inside[0] += 1
        most[0] = max(most[0], inside[0])
        time.sleep(0.05)
        try:
            return read_values(*args, **options)
        finally:
            inside[0] -= 1
    server.system.read_values = slow
    clients = [st.connect(server.path) for _ in range(3)]
    try:
        device = clients[0].device("house bank")
        table = clients[0].tables(device, [PAGE_BY_NAME["config"]])["config"]
        threads = [threading.Thread(target=c.read_values, args=(device, table, [0])) for c in clients]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        assert most[0] == 1
    finally:
        for c in clients:
            c.close()


def test_shutdown_lets_the_write_on_the_bus_finish_before_it_closes_the_link(served):
    server, bus = served
    system = st.connect(server.path)
    sent, closed = [], []
    send, close = bus.link.send, bus.link.close

    def slow(*frames, **options):
        time.sleep(0.02)  # a text goes out a chunk at a time
        sent.extend(frames)
        return send(*frames, **options)
    try:
        device = system.device("house bank")
        table = system.tables(device, [PAGE_BY_NAME["config"]])["config"]
        bus.link.send = slow
        bus.link.close = lambda: closed.append(len(sent)) or close()
        writing = threading.Thread(target=system.write_text, args=(device, table, 7, "A whole new name"))
        writing.start()
        deadline = time.monotonic() + 2
        while not [f for f in sent if f.data[:1] == b"\x30" and len(f.data) > 4] and time.monotonic() < deadline:
            time.sleep(0.005)
        server.shutdown()  # with the write under way
        writing.join(5)
        chunks = [f.data[3] for f in sent if f.data[:1] == b"\x30" and len(f.data) > 4]
        assert chunks == [0, 1, 2, 3, 4] and closed == [len(sent)]  # every chunk, the NUL's too, then the close
    finally:
        system.close()
        test_pages.STRINGS[219] = "MSH House Bank"


def test_listening_through_the_server_is_a_link_error_not_a_crash(served):
    server, bus = served
    system = st.connect(server.path)
    try:
        with pytest.raises(LinkError):
            system.listen()
    finally:
        system.close()


def test_a_call_stopped_before_its_reply_takes_the_connection_with_it(served, monkeypatch):
    server, bus = served
    system = st.connect(server.path)
    device = system.device("house bank")
    readline = system.file.readline

    def interrupted():
        monkeypatch.setattr(system.file, "readline", readline)
        raise KeyboardInterrupt
    monkeypatch.setattr(system.file, "readline", interrupted)
    with pytest.raises(KeyboardInterrupt):
        system.login_level(device)  # Ctrl-C while the server answers
    with pytest.raises(LinkError):
        system.read_values(device, PageTable(PAGE_BY_NAME["config"]), [0])  # not the login level's reply


def test_a_slow_page_read_through_the_server_is_waited_for(served):
    import time
    server, bus = served
    read = server.system.tables

    def slow(*args, **kwargs):
        time.sleep(0.3)  # a cold read of a big page
        return read(*args, **kwargs)
    server.system.tables = slow
    system = st.connect(server.path, timeout=0.1)  # less than the read takes
    try:
        assert "config" in system.tables(system.device("msh"), [PAGE_BY_NAME["config"]])
    finally:
        system.close()


def test_a_server_too_slow_to_answer_is_a_link_error_every_time(served):
    import time
    server, bus = served
    read = server.system.read_values

    def slow(*args, **kwargs):
        time.sleep(0.3)
        return read(*args, **kwargs)
    server.system.read_values = slow
    system = st.connect(server.path, timeout=0.1)
    try:
        device = system.device("msh")
        table = system.tables(device, [PAGE_BY_NAME["config"]])["config"]
        for _ in range(2):  # not a timeout the caller takes for a silent device, then a broken socket
            with pytest.raises(LinkError):
                system.read_values(device, table, [0])
    finally:
        system.close()


def test_a_command_waits_behind_an_abandoned_read_and_pages_come_one_at_a_time(served):
    import json
    import socket
    server, bus = served
    read, asked = server.system.tables, []

    def slow(device, pages=None, *args, **kwargs):
        asked.append([p.name for p in pages or []])
        time.sleep(0.3)  # a cold page read
        return read(device, pages, *args, **kwargs)
    server.system.tables = slow
    gone = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # a command stopped during its page read
    gone.connect(str(server.path))
    device = {"address": str(SHUNT), "serial": "E819L0421", "version": "2.1"}
    gone.sendall((json.dumps({"method": "tables", "params": {"device": device, "pages": ["config"]}}) + "\n").encode())
    gone.close()
    system = st.connect(server.path)  # the next command: it waits for the bus instead of failing
    try:
        assert system.sock.gettimeout() is None
        tables = system.tables(system.device("msh"))
        assert sorted(tables) == ["alarm", "config", "history", "monitor"]
        assert all(len(pages) == 1 for pages in asked[1:]) and len(asked) == 5  # the abandoned one, then one each
    finally:
        system.close()


@pytest.mark.parametrize("sent", [b'{"result": [{"address": "1b:0', b"", b"not json\n"])
def test_a_reply_cut_off_or_unreadable_is_a_link_error(sent):
    import socket
    ours, server = socket.socketpair()
    server.sendall(sent)
    server.close()  # the server's thread died mid-reply
    system = st.StateSystem(ours, Path("state.sock"))
    with pytest.raises(LinkError):
        system.devices()
    with pytest.raises(LinkError):  # and the connection is not used again
        system.devices(refresh=True)


def test_a_refresh_through_the_server_enumerates_the_bus_again(served):
    server, bus = served
    system = st.connect(server.path)
    try:
        system.devices()
        before = enumerations(bus)
        system.devices()
        assert enumerations(bus) == before  # remembered
        system.devices(refresh=True)
        assert enumerations(bus) == before + 1  # asked again, as System.devices promises
    finally:
        system.close()


def test_a_changed_page_is_read_again(served):
    server, bus = served
    system = st.connect(server.path)
    try:
        device = system.device("msh")
        cfg = PAGE_BY_NAME["config"]
        assert system.tables(device, [cfg])["config"].values[0].writeable is True
        COUNTS[0x1E], CFG_ITEMS[0][0x0B] = 8, 0  # the page's version moved: the lock flipped, say
        try:
            table = system.tables(device, [cfg])["config"]
            assert table.version == 8 and table.values[0].writeable is False
        finally:
            COUNTS[0x1E], CFG_ITEMS[0][0x0B] = 7, 1
    finally:
        system.close()


def test_nocache_reaches_the_server(served, tmp_path, monkeypatch):
    server, bus = served
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    server.system.use_cache = True
    cfg = PAGE_BY_NAME["config"]
    plain = st.connect(server.path)
    try:
        device = plain.device("msh")
        plain.tables(device, [cfg])  # into the cache
        reads = sum(1 for f in bus.link.sent if f.kind == 0x38)
        plain.tables(device, [cfg])
        assert sum(1 for f in bus.link.sent if f.kind == 0x38) == reads  # from the cache
    finally:
        plain.close()
    system = st.connect(server.path, refresh=True)
    try:
        system.tables(device, [cfg])
        afresh = sum(1 for f in bus.link.sent if f.kind == 0x38)
        assert afresh > reads  # read afresh, not from the cache
        system.tables(device, [cfg])
        assert sum(1 for f in bus.link.sent if f.kind == 0x38) == afresh  # once per command, then the new cache
        assert server.system.refresh is False  # the server's own setting is untouched
    finally:
        system.close()


def test_a_command_the_server_runs_uses_its_state(served, monkeypatch, capsys):
    server, bus = served
    import mvconnect.cli as cli
    monkeypatch.setenv(st.STATE_VARIABLE, str(server.path))  # as the server sets it for its commands
    monkeypatch.setattr(st, "MasterBus", lambda: (_ for _ in ()).throw(AssertionError("opened the link")))
    monkeypatch.setattr(cli, "setup_logging", lambda debug=False: None)
    before = enumerations(bus)
    cli.Tool().data("house", "bank", "config", "0", timeout=0.02)
    out = capsys.readouterr().out
    assert line_with(out, "Nominal voltage") and has(out, 12)
    cli.Tool().data("house", "bank", "config", timeout=0.02)
    assert enumerations(bus) == before  # neither command listened for the devices again


def test_login_and_logout_go_through_the_state(served, monkeypatch, caplog):
    server, bus = served
    import logging

    import mvconnect.cli as cli
    monkeypatch.setenv(st.STATE_VARIABLE, str(server.path))
    monkeypatch.setattr(st, "MasterBus", lambda: (_ for _ in ()).throw(AssertionError("opened the link")))
    monkeypatch.setattr(cli, "setup_logging", lambda debug=False: None)
    caplog.set_level(logging.INFO, logger="mvconnect.state")
    try:
        cli.Tool().login(level="service", password=test_pages.PASSWORDS[3], timeout=0.02)
        assert COUNTS[0x19] == 3
        with pytest.raises(SystemExit):
            cli.Tool().login(level="installer", password=12345, timeout=0.02)  # the wrong password
        cli.Tool().logout(timeout=0.02)
        assert COUNTS[0x19] == 0
    finally:
        COUNTS[0x19] = 0
    state = [r.getMessage() for r in caplog.records if r.name == "mvconnect.state"]
    assert len(state) == 3 and has(state[0], SHUNT, "service", "ok") and has(state[1], SHUNT, "installer", "failed")
    assert has(state[2], SHUNT, "logout", "ok")
    assert not any(str(test_pages.PASSWORDS[3]).rstrip("0").rstrip(".") in m or "12345" in m for m in state)


def test_the_server_starts_on_the_link_and_points_its_commands_at_itself(short_path, monkeypatch):
    monkeypatch.setattr(st, "MasterBus", lambda: MasterBus(FakeShunt(), reply_timeout=0.02, retries=0))
    monkeypatch.delenv(st.STATE_VARIABLE, raising=False)
    server = st.start_in_thread(short_path / "state.sock", use_cache=False, timeout=0.02)
    try:
        assert st.os.environ[st.STATE_VARIABLE] == str(server.path)
        with st.open_system() as system:  # what a command the server runs gets
            assert isinstance(system, st.StateSystem) and system.device("msh").address == SHUNT
    finally:
        server.stop()
        server.system.bus.close()
        st.os.environ.pop(st.STATE_VARIABLE, None)


def test_a_server_that_cannot_listen_is_a_link_error(short_path, monkeypatch):
    monkeypatch.setattr(st, "MasterBus", lambda: MasterBus(FakeShunt(), reply_timeout=0.02, retries=0))
    with pytest.raises(LinkError):
        st.start_in_thread(short_path / "no" / "such" / "dir" / ("x" * 200) / "state.sock", timeout=0.02)


def test_an_error_accepting_a_client_does_not_end_the_server(short_path):
    import errno
    bus = MasterBus(FakeShunt(), reply_timeout=0.02, retries=0).open()
    server = st.StateServer(System(bus, use_cache=False), short_path / "state.sock", timeout=0.02)
    server.bind()
    listener, failed = server.listener, []

    class OnceOutOfFiles:
        def accept(self):
            if not failed:
                failed.append(True)
                raise OSError(errno.EMFILE, "Too many open files")
            return listener.accept()

        def __getattr__(self, name):
            return getattr(listener, name)
    server.listener = OnceOutOfFiles()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        system = st.connect(server.path, timeout=5)
        try:
            assert failed and thread.is_alive() and system.device("msh").address == SHUNT
        finally:
            system.close()
    finally:
        server.stop()
        thread.join(5)
        bus.close()


def test_a_monitor_that_stops_reading_is_let_go_and_costs_no_memory(short_path, monkeypatch):
    import json
    import socket
    from mvconnect.frame import Frame
    monkeypatch.setattr(st, "STREAM_TIMEOUT", 0.2)
    server, bus, thread = serve(short_path / "state.sock")
    try:
        stalled = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stalled.connect(str(server.path))
        stalled.sendall((json.dumps({"method": "traffic", "params": {}}) + "\n").encode())
        deadline = time.monotonic() + 1
        while len(bus._taps) == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert bus._taps  # the stream is listening, so that its going away below means something
        frame = Frame(0x10, SHUNT, bytes(6))
        deadline = time.monotonic() + 10
        while bus._taps and time.monotonic() < deadline:  # it never reads: its stream must end
            for _ in range(2000):
                bus._receive(frame)
            time.sleep(0.01)
        assert bus._taps == []  # the stream let go of the bus
        stalled.close()
    finally:
        server.stop()
        thread.join(5)
        bus.close()


@pytest.mark.parametrize("sent", [b"", b'{"time": 1.0, "kind": 16, "address": "1b:0', b"not json\n"])
def test_a_stream_of_traffic_that_ends_or_breaks_is_a_link_error(short_path, sent):
    import socket
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(short_path / "s.sock"))
    listener.listen(1)

    def server():
        conn, _ = listener.accept()
        conn.recv(1000)  # the request for the traffic
        conn.sendall(b'{"time": 0.5, "kind": 16, "address": "1b:0cad9", "data": "00", "sent": false}\n' + sent)
        conn.close()
    threading.Thread(target=server, daemon=True).start()
    system = st.connect(short_path / "s.sock")
    frames = system.traffic()
    assert next(frames)[1].address == SHUNT
    with pytest.raises(LinkError):
        next(frames)
    listener.close()


def test_stop_returns_once_every_client_is_let_go(short_path):
    import json
    import socket
    server, bus, thread = serve(short_path / "state.sock")
    idle, watching = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM), socket.socket(socket.AF_UNIX)
    try:
        idle.connect(str(server.path))  # connected, and never asks anything
        watching.connect(str(server.path))
        watching.sendall((json.dumps({"method": "traffic", "params": {}}) + "\n").encode())
        deadline = time.monotonic() + 1
        while not bus._taps and time.monotonic() < deadline:
            time.sleep(0.01)
        assert bus._taps  # the stream is listening
        server.stop()
        assert not thread.is_alive() and bus._taps == []  # the stream let go of the bus before stop() returned
        assert idle.recv(1) == b""  # and the idle client was hung up on
    finally:
        idle.close(), watching.close()
        bus.close()


def test_a_link_that_fails_ends_the_monitor_locally_and_through_the_server(served):
    server, bus = served
    try:
        local = System(bus).traffic()
        bus.link.failure = "read from MasterBus USB Link failed: device gone"
        with pytest.raises(LinkError, match=mentions("device gone")):
            next(local)
        system = st.connect(server.path)
        try:
            with pytest.raises(LinkError, match=mentions("device gone")):
                next(system.traffic())
        finally:
            system.close()
    finally:
        bus.link.failure = None


def test_enumerating_again_finds_a_new_device(short_path):
    server, bus, thread = serve(short_path / "state.sock", re_enumerate=0.05)
    try:
        before = enumerations(bus)
        deadline = time.monotonic() + 3
        while enumerations(bus) == before and time.monotonic() < deadline:
            time.sleep(0.01)
        assert enumerations(bus) > before
    finally:
        server.stop()
        thread.join(5)
        bus.close()


def test_a_connection_to_the_server_closed_by_ctrl_c_says_so(served, monkeypatch, caplog):
    import logging
    server, bus = served
    monkeypatch.setenv(st.STATE_VARIABLE, str(server.path))
    with caplog.at_level(logging.INFO, logger="mvconnect.state"), pytest.raises(KeyboardInterrupt):
        with st.open_system():
            raise KeyboardInterrupt
    assert any("Ctrl-C" in r.getMessage() and "closing the connection" in r.getMessage() for r in caplog.records)


def test_a_server_shutting_down_says_what_it_winds_down(short_path, caplog):
    import logging
    server, bus, thread = serve(short_path / "state.sock")
    with caplog.at_level(logging.INFO, logger="mvconnect.state"):
        server.shutdown()
    thread.join(5)
    said = [r.getMessage() for r in caplog.records if r.name == "mvconnect.state"]
    assert has(said[-2], "ending", "on the bus") and has(said[-1], "ended", "link", "closed")


def test_ctrl_c_during_a_page_read_through_the_server_stays_ctrl_c(served, monkeypatch, caplog):
    """As on r5 on 2026-10-25: Ctrl-C while the command waits for a page closes the connection, and the command
    winds down on the KeyboardInterrupt, not on an error about the closed socket."""
    import logging

    import mvconnect.cli as cli
    server, bus = served
    monkeypatch.setenv(st.STATE_VARIABLE, str(server.path))
    monkeypatch.setattr(cli, "setup_logging", lambda debug=False: None)
    real_call = st.StateSystem.call

    def call(self, method, **params):
        if method == "tables":  # Ctrl-C while the reply is awaited
            monkeypatch.setattr(self.file, "readline", lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
        return real_call(self, method, **params)
    monkeypatch.setattr(st.StateSystem, "call", call)
    with caplog.at_level(logging.INFO), pytest.raises(KeyboardInterrupt):
        cli.Tool().data("house", "bank", timeout=0.02)
    assert any("Ctrl-C" in r.getMessage() and "closing the connection" in r.getMessage() for r in caplog.records)


def test_a_closed_connection_is_a_link_error_for_a_page_read_too(served):
    server, bus = served
    system = st.connect(server.path)
    device = system.device("msh")
    system.close()
    with pytest.raises(LinkError, match=mentions("closed")):
        system.tables(device, [PAGE_BY_NAME["config"]])
