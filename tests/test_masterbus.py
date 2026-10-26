"""The protocol layer against a fake link that answers like the devices on a real bus did."""

import time

import pytest
from expect import logged, mentions

from mvconnect.frame import KIND_ANNOUNCE, KIND_ENUMERATE, KIND_REPLY, KIND_REQUEST, Address, Frame
from mvconnect.hidlink import LinkInfo
from mvconnect.masterbus import HOST_ADDRESS, AnnounceInfo, MasterBus

# Announcements captured on a live bus
ANNOUNCEMENTS = {
    Address(0x0A, 0x1062B): "0a2b06010a030000",
    Address(0x0C, 0x1A760): "0c60a70109030000",
    Address(0x14, 0x08559): "1459850002010000",
    Address(0x1B, 0x0CAD9): "1bd9ca0000020000",
}

STRINGS = {Address(0x1B, 0x0CAD9): {0: "E819L0421", 1: "House Bank", 0x11: "Mastervolt", 0x12: "77020100",
                                     0x16: "Ah", 0x19: "% SOC", 219: "MSH House Bank"},
           Address(0x0C, 0x1A760): {0: "D903J1063", 5: "AA0400", 7: "45512000", 225: "APR Generator"}}
# Identity strings: which -> string id, and the version number, as the devices answered
GENERAL = {Address(0x1B, 0x0CAD9): {1: 0x12, 2: 0, 3: 219}, Address(0x0C, 0x1A760): {1: 7, 2: 0, 3: 225, 4: 5}}
VERSION = {Address(0x1B, 0x0CAD9): 0x0201, Address(0x0C, 0x1A760): 0x0400}


class FakeLink:
    """Answers ENUMERATE with the captured announcements and string requests 4 characters at a time."""

    def __init__(self, drop_first=0):
        self.listener = None
        self.sent = []
        self.drop = drop_first

    def open(self, listener):
        self.listener = listener
        return self

    def close(self):
        pass

    def send(self, *frames, timeout=1.0):
        for f in frames:
            self.sent.append(f)
            if f.kind == KIND_ENUMERATE:
                for addr, data in ANNOUNCEMENTS.items():
                    self.listener(Frame(KIND_ANNOUNCE, addr, bytes.fromhex(data)))
                # a device whose announcement is malformed
                self.listener(Frame(KIND_ANNOUNCE, Address(1, 1), b"\x01"))
            elif f.kind == KIND_REQUEST and f.data[0] == 0x30:
                if self.drop:
                    self.drop -= 1
                    continue
                sid = f.data[1] | f.data[2] << 8
                text = STRINGS.get(f.address, {}).get(sid)
                if text is None:
                    continue
                chunk = (text.encode("latin-1") + b"\0")[4 * f.data[3]:4 * f.data[3] + 4]
                self.listener(Frame(KIND_REPLY, f.address, f.data[:4] + chunk))
            elif f.kind == KIND_REQUEST and f.data[0] == 0x09:
                sid = GENERAL.get(f.address, {}).get(f.data[1])
                if sid is not None:
                    self.listener(Frame(KIND_REPLY, f.address, f.data[:2] + sid.to_bytes(2, "little")))
            elif f.kind == KIND_REQUEST and f.data[0] == 0x08:
                # a stale reply for another type first: the matcher must skip it
                self.listener(Frame(KIND_REPLY, f.address, bytes([0x08, 0x1F, 0, 0])))
                if f.data[1] == 0x0B and f.address in VERSION:
                    self.listener(Frame(KIND_REPLY, f.address, f.data[:2] + VERSION[f.address].to_bytes(2, "little")))


def test_announce_parse():
    ann = AnnounceInfo.parse(bytes.fromhex("0a2b06010a030000"))
    assert ann.address == Address(0x0A, 0x1062B)
    assert ann.info == bytes.fromhex("0a030000")
    with pytest.raises(ValueError):
        AnnounceInfo.parse(b"\x0a\x2b")


def test_enumerate_sends_broadcast_from_host_and_sorts():
    bus = MasterBus(FakeLink(), reply_timeout=0.05).open()
    devices = bus.enumerate(timeout=0.05)
    assert bus.link.sent[0] == Frame(KIND_ENUMERATE, HOST_ADDRESS)
    assert [d.address for d in devices] == sorted(ANNOUNCEMENTS)
    assert devices[-1].info == bytes.fromhex("00020000")


def test_read_string_assembles_chunks():
    bus = MasterBus(FakeLink(), reply_timeout=0.05).open()
    addr = Address(0x1B, 0x0CAD9)
    assert bus.read_string(addr, 1) == "House Bank"
    assert bus.read_string(addr, 0x16) == "Ah"
    assert bus.read_string(addr, 0) == "E819L0421"
    # 'E819L0421' + NUL is 10 bytes: three round trips
    assert sum(1 for f in bus.link.sent if f.data[1:3] == b"\0\0") == 3


def test_an_interrupt_waits_until_the_whole_text_is_written(monkeypatch):
    import signal
    bus = MasterBus(FakeLink(), reply_timeout=0.05).open()
    send = bus.link.send

    def interrupted(*frames, **options):
        if frames[0].data[3] == 1:
            signal.raise_signal(signal.SIGINT)  # Ctrl-C after the first chunk
        return send(*frames, **options)

    monkeypatch.setattr(bus.link, "send", interrupted)
    with pytest.raises(KeyboardInterrupt):
        bus.write_string(Address(0x1B, 0x0CAD9), 1, "Bow Bank")
    assert [f.data[3] for f in bus.link.sent] == [0, 1, 2]  # every chunk, the NUL in the last, then the interrupt
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_a_short_answer_for_an_identity_string_leaves_it_empty(monkeypatch):
    bus = MasterBus(FakeLink(), reply_timeout=0.02, retries=0).open()
    listener = bus.link.listener

    def short_identities(frame):  # the string id cut off
        listener(Frame(frame.kind, frame.address, frame.data[:2]) if frame.data[0] == 0x09 else frame)

    monkeypatch.setattr(bus.link, "listener", short_identities)
    device = bus.identify(next(d for d in bus.enumerate(timeout=0.05) if d.address == Address(0x1B, 0x0CAD9)))
    assert (device.product, device.serial, device.version) == ("", "", "2.1")


def test_a_send_that_waits_for_a_slot_does_not_eat_the_reply_time():
    class Slow(FakeLink):
        def send(self, *frames, timeout=1.0):
            super().send(*frames, timeout=timeout)  # the device answers at once
            time.sleep(0.05)  # but the link took longer than the reply timeout to take the frame

    bus = MasterBus(Slow(), reply_timeout=0.02, retries=0).open()
    assert bus.read_number(Address(0x1B, 0x0CAD9), 0x0B) == 0x0201
    assert len(bus.link.sent) == 1  # answered the first time, not sent again


def test_a_frame_queued_in_time_is_handed_over_however_late_it_is_looked_for():
    from mvconnect.hidlink import FrameQueue
    q = FrameQueue()
    frame = Frame(KIND_REPLY, Address(0x1B, 0x0CAD9), b"\x08\x0b")
    q(frame)
    assert list(q.until(time.monotonic() - 1)) == [frame]


def test_a_kill_or_a_hangup_also_waits_until_the_whole_text_is_written(monkeypatch):
    import signal

    from mvconnect.masterbus import uninterrupted
    got, before = [], signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, lambda signum, frame: got.append(signum))
    try:
        with uninterrupted():
            signal.raise_signal(signal.SIGTERM)
            assert got == []  # held
        assert got == [signal.SIGTERM]  # and delivered once the block is done
        assert signal.getsignal(signal.SIGTERM) is not None and signal.getsignal(signal.SIGTERM) != before
    finally:
        signal.signal(signal.SIGTERM, before)


def test_a_string_chunk_or_a_login_is_sent_once_however_many_retries_a_read_gets():
    from mvconnect.masterbus import Number
    bus = MasterBus(FakeLink(drop_first=1), reply_timeout=0.02, retries=2).open()
    with pytest.raises(TimeoutError):
        bus.write_string(Address(0x1B, 0x0CAD9), 1, "Bow")
    assert len([f for f in bus.link.sent if f.data[:1] == b"\x30"]) == 1  # the lost answer is not asked again
    bus = MasterBus(FakeLink(), reply_timeout=0.02, retries=2).open()  # this link does not answer a login
    with pytest.raises(TimeoutError):
        bus.login(Address(0x1B, 0x0CAD9), 1, 1234.0)
    assert len([f for f in bus.link.sent if f.data[:2] == bytes([0x08, Number.LOGIN])]) == 1


def test_request_retries_then_times_out():
    bus = MasterBus(FakeLink(drop_first=1), reply_timeout=0.02, retries=2).open()
    addr = Address(0x1B, 0x0CAD9)
    assert bus.read_string(addr, 0x19) == "% SOC"
    bus = MasterBus(FakeLink(), reply_timeout=0.02, retries=1).open()
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        bus.read_string(Address(0x0A, 0x1062B), 1)
    assert time.monotonic() - t0 < 0.5
    assert len(bus.link.sent) == 2


def test_list_devices_identifies_and_reports_progress():
    bus = MasterBus(FakeLink(), reply_timeout=0.02, retries=0).open()
    steps = []
    devices = bus.list_devices(timeout=0.05, progress=lambda done, total: steps.append((done, total)))
    assert steps == [(0, 4), (1, 4), (2, 4), (3, 4), (4, 4)]
    by_addr = {d.address: d for d in devices}
    bank = by_addr[Address(0x1B, 0x0CAD9)]
    assert (bank.product_name, bank.kind, bank.name) == ("MSH House Bank", "MSH", "House Bank")
    assert (bank.product, bank.serial, bank.revision, bank.version) == ("77020100", "E819L0421", "", "2.1")
    assert bank.selector == "1b:0cad9"
    gen = by_addr[Address(0x0C, 0x1A760)]
    assert (gen.kind, gen.name, gen.revision, gen.version) == ("APR", "Generator", "AA0400", "4.0")
    # a silent device keeps empty strings instead of failing the listing
    assert by_addr[Address(0x0A, 0x1062B)].product_name == ""


def test_identity_string_and_number():
    bus = MasterBus(FakeLink(), reply_timeout=0.02, retries=0).open()
    addr = Address(0x1B, 0x0CAD9)
    assert bus.read_identity(addr, 1) == "77020100"
    assert bus.read_number(addr, 0x0B) == 0x0201
    with pytest.raises(TimeoutError):
        bus.read_number(addr, 0x0A)


def test_ambiguous_device_name_is_an_error(caplog):
    import logging

    from mvconnect.system import AddressError, System
    link = FakeLink()
    bus = MasterBus(link, reply_timeout=0.05).open()
    system = System(bus)
    # two devices answer to the same given name
    STRINGS[Address(0x0C, 0x1A760)][225] = "APR House Bank"
    try:
        with caplog.at_level(logging.INFO, logger="mvconnect.system"):
            with pytest.raises(AddressError, match=mentions("house bank", 2)):
                system.device("house bank", timeout=0.05)
        assert logged(caplog, "INFO", "0c:1a760") and logged(caplog, "INFO", "1b:0cad9")  # the candidates
        assert system.device("1b:0cad9", timeout=0.05).address == Address(0x1B, 0x0CAD9)
        # by type: one shunt on the bus, two APR-typed products would be ambiguous
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="mvconnect.system"):
            assert system.device("MSH", timeout=0.05).address == Address(0x1B, 0x0CAD9)
            assert logged(caplog, "INFO", "MSH", "1b:0cad9")
            caplog.clear()
            system.device("1b:0cad9", timeout=0.05)
            assert caplog.records == []  # an address needs no explanation
        assert system.device("apr", timeout=0.05).address == Address(0x0C, 0x1A760)
        with pytest.raises(AddressError, match=mentions("apr 98")):
            system.device("apr 98", timeout=0.05)  # device() takes the words whole
        with pytest.raises(AddressError, match=mentions("xyz 98")):
            system.device("xyz 98", timeout=0.05)
        # locate() splits them: the longest leading run that is one device, the rest for its values
        sel = system.locate(["apr", "98"], timeout=0.05)
        assert sel.device.address == Address(0x0C, 0x1A760) and sel.words == "98"
        assert system.locate(["msh"], timeout=0.05).words == ""
        assert system.locate(["1b:0cad9", "batt", "cap"], timeout=0.05).words == "batt cap"
        with pytest.raises(AddressError, match=mentions("house bank", 2)):
            system.locate(["house", "bank", "batt"], timeout=0.05)  # two House Banks: no run is one device
        with pytest.raises(AddressError, match=mentions("xyz 98")):
            system.locate(["xyz", "98"], timeout=0.05)
    finally:
        STRINGS[Address(0x0C, 0x1A760)][225] = "APR Generator"


@pytest.mark.parametrize("ending, cause", [("ctrl-c", "Ctrl-C"), ("term", "SIGTERM")])
def test_a_link_closed_by_an_interrupt_says_so(caplog, ending, cause):
    import logging
    import signal

    from mvconnect.masterbus import Ended
    exc = KeyboardInterrupt() if ending == "ctrl-c" else Ended(signal.SIGTERM)
    with caplog.at_level(logging.INFO, logger="mvconnect.masterbus"), pytest.raises(type(exc)):
        with MasterBus(FakeLink(), reply_timeout=0.02):
            raise exc
    assert logged(caplog, "WARNING", cause, "closing the link")


def test_a_link_closed_by_an_error_or_at_the_end_logs_no_wind_down(caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="mvconnect.masterbus"):
        with MasterBus(FakeLink(), reply_timeout=0.02):
            pass
        with pytest.raises(ValueError), MasterBus(FakeLink(), reply_timeout=0.02):
            raise ValueError("an error, which has its own line")
    assert not any("closing the link" in r.getMessage() for r in caplog.records)


def test_a_signal_held_during_a_write_is_logged_as_it_is_delivered(monkeypatch, caplog):
    import logging
    import signal
    bus = MasterBus(FakeLink(), reply_timeout=0.05).open()
    send = bus.link.send

    def interrupted(*frames, **options):
        if frames[0].data[3] == 1:
            signal.raise_signal(signal.SIGINT)  # Ctrl-C after the first chunk
        return send(*frames, **options)
    monkeypatch.setattr(bus.link, "send", interrupted)
    with caplog.at_level(logging.INFO, logger="mvconnect.masterbus"), pytest.raises(KeyboardInterrupt):
        bus.write_string(Address(0x1B, 0x0CAD9), 1, "Bow Bank")
    assert logged(caplog, "WARNING", "Ctrl-C", "write of several frames", "finished")


def test_the_first_line_about_a_signal_is_a_warning_and_the_rest_info(caplog):
    """A Ctrl-C held through a write, then the link closed, then the command's last line: one warning, then info."""
    import logging
    import signal

    from mvconnect.masterbus import uninterrupted, winding_down
    caplog.set_level(logging.INFO, logger="mvconnect")
    bus = MasterBus(FakeLink(), reply_timeout=0.02)
    with pytest.raises(KeyboardInterrupt) as e:
        with bus.open():
            with uninterrupted():
                signal.raise_signal(signal.SIGINT)  # held until the block ends
    winding_down(e.value, "wound down, exiting with status 130")
    levels = [(r.levelname, r.getMessage().split(": ")[1][:20]) for r in caplog.records if "Ctrl-C" in r.getMessage()]
    assert [level for level, _ in levels] == ["WARNING", "INFO", "INFO"], levels


def test_host_address_follows_the_link_serial():
    from mvconnect.masterbus import host_address
    assert host_address("F226E0144") == Address(0x14, 0x30450)  # year 2 & 3, week 26, number 144, groups 207
    assert host_address("K712Q2000", "77030500") == Address(0x14, 0x3FF90)  # year 7 & 3 = 3, week 12, groups 607
    assert host_address("FO26E0144") == host_address("F226E0144")  # O stands for 10, and 10 & 3 == 2 & 3
    for bad in ("", "F226E014", "F226E01441", "FX26E0144", "F2A6E0144", "F226E01x1"):
        assert host_address(bad) == HOST_ADDRESS
    assert host_address("F226E0144", "abc") == HOST_ADDRESS


def test_the_bus_takes_its_address_from_the_link():
    class LinkWithSerial(FakeLink):
        info = LinkInfo(b"/dev/hidraw0", "F226E0144", "MasterBus USB Link", "Mastervolt")

    bus = MasterBus(LinkWithSerial(), reply_timeout=0.05).open()
    bus.enumerate(timeout=0.05)
    assert bus.link.sent[0] == Frame(KIND_ENUMERATE, Address(0x14, 0x30450))
    given = MasterBus(LinkWithSerial(), host=Address(0x14, 0x7), reply_timeout=0.05).open()
    assert given.host == Address(0x14, 0x7)
    assert MasterBus(FakeLink(), reply_timeout=0.05).open().host == HOST_ADDRESS  # a link without a serial

