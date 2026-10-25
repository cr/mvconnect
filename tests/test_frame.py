"""Frame codec pinned to bytes captured from a live bus."""

import pytest

from expect import mentions

from mvconnect.frame import (KIND_ANNOUNCE, KIND_ENUMERATE, KIND_REPLY, KIND_REQUEST, Address, Frame, pack_report,
                        unpack_report)

# One input report as read from the link: one record, stale data in the other slots.
CAPTURED = bytes.fromhex(
    "01c368cad90201005ac812420000ffc768cad903080500000100003d02c368cad90205000086be40000002c368cad9"
    "02020000010a0300000200120000000000")


def test_no_log_shows_a_login_password(caplog):
    import logging
    import struct

    from mvconnect.hidlink import HidLink
    password = struct.pack("<f", -2222.0)
    login = Frame(0x0E, Address(0x1B, 0x0CAD9), bytes([0x08, 0x19, 2, 0]) + password)
    assert login.secret and password.hex(" ") not in str(login) and "08 19 02 00" in str(login)
    echo = Frame(0x0C, Address(0x1B, 0x0CAD9), login.data)
    assert echo.secret and password.hex(" ") not in str(echo)
    assert not Frame(0x37, Address(0x1B, 0x0CAD9), login.data).secret  # a page write is no login

    class Device:
        def __init__(self):
            self.reports = [bytes(pack_report([echo]))]

        def write(self, report):
            return len(report)

        def read(self, size, timeout_ms):
            if self.reports:
                return list(self.reports.pop())
            raise OSError("done")
    link = HidLink()
    link._dev = Device()
    with caplog.at_level(logging.DEBUG):
        link.send(login)
        link._read_loop()  # the echo comes in, then the device goes away
    assert caplog.records and not any(password.hex(" ") in r.getMessage() or password.hex() in r.getMessage()
                                      for r in caplog.records)


def test_a_report_s_stale_slots_are_never_logged(caplog):
    import logging
    import struct

    from mvconnect.hidlink import HidLink
    password = struct.pack("<f", 1234.0)
    old = Frame(0x0C, Address(0x1B, 0x0CAD9), bytes([0x08, 0x19, 1, 0]) + password)  # an earlier login's echo
    new = Frame(0x0C, Address(0x1B, 0x0CAD9), bytes([0x08, 0x0B, 1, 2]))
    payload = bytearray(pack_report([new, old]))
    payload[0] = 1  # one record now; the second slot still holds the old one

    class Device:
        def __init__(self):
            self.reports = [bytes(payload), b"\x09" + bytes(63)]  # and then one the parser refuses

        def read(self, size, timeout_ms):
            if self.reports:
                return list(self.reports.pop(0))
            raise OSError("done")
    link = HidLink()
    link._dev = Device()
    with caplog.at_level(logging.DEBUG):
        link._read_loop()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert new.data.hex(" ") in text  # the record in use is shown
    assert password.hex(" ") not in text and password.hex() not in text


def test_a_link_whose_reader_failed_says_so_instead_of_timing_out():
    from mvconnect.hidlink import HidLink, LinkError

    class Unplugged:
        def read(self, size, timeout_ms):
            raise OSError("read error")
    link = HidLink()
    link._dev = Unplugged()
    link._read_loop()  # returns at the failure
    with pytest.raises(LinkError, match=mentions("read error")):
        link.send(Frame(0x0E, Address(0x1B, 0x0CAD9), b"\x08\x0b"))


def test_the_link_sends_no_more_records_than_its_credits_allow():
    import threading
    import time

    from mvconnect.hidlink import HidLink, LinkError
    credits = bytearray(64)
    credits[58] = 4  # a report from the link: no records, four slots free

    class Device:
        def __init__(self):
            self.written, self.told = [], False

        def write(self, report):
            self.written.append(report[1])  # the record count, behind the report id
            return len(report)

        def read(self, size, timeout_ms):
            time.sleep(0.01)
            if self.written and not self.told:  # the slots come free once something was sent
                self.told = True
                return list(credits)
            return []
    link = HidLink()
    link._dev = device = Device()
    link._credits = 2
    reader = threading.Thread(target=link._read_loop, daemon=True)
    reader.start()
    try:
        link.send(*[Frame(0x0E, Address(0x1B, 0x0CAD9), b"\x08\x0b")] * 5, timeout=2)
    finally:
        link._stop.set()
        reader.join(1)
    assert device.written == [2, 3] and link._credits == 1  # two while two were free, then three of the four
    link._credits, device.written = 0, []
    with pytest.raises(LinkError):
        link.send(Frame(0x0E, Address(0x1B, 0x0CAD9), b"\x08\x0b"), timeout=0.05)  # no slot comes free
    assert device.written == []


def test_address_str_roundtrip():
    a = Address(0x1B, 0x0CAD9)
    assert str(a) == "1b:0cad9"
    assert Address.parse("1b:0cad9") == a
    assert Address.parse("1B:CAD9") == a


def test_address_range():
    with pytest.raises(ValueError):
        Address(0x20, 0)
    with pytest.raises(ValueError):
        Address(0, 0x40000)
    with pytest.raises(ValueError):
        Address.parse("1b0cad9")


def test_unpack_captured_record():
    frame = Frame.unpack(bytes.fromhex("c368cad90201005ac812420000ff"))
    assert frame.kind == 0x30
    assert frame.address == Address(0x1B, 0x0CAD9)
    assert frame.data == bytes.fromhex("0100")
    assert frame.timestamp == 0xFF


def test_pack_roundtrip_all_fields():
    frame = Frame(KIND_REQUEST, Address(0x1F, 0x3FFFF), bytes.fromhex("30150000"), timestamp=0x42)
    packed = frame.pack()
    assert len(packed) == 14
    assert packed[:4] == bytes.fromhex("3be3ffff")  # bits 20..18 stay clear
    assert Frame.unpack(packed) == frame


def test_word_layout():
    assert Frame(KIND_ENUMERATE, Address(0x14, 0x08559)).word == 0x2A808559
    assert Frame(KIND_ANNOUNCE, Address(0x0A, 0x1062B)).word == 0x2141062B


def test_frame_limits():
    with pytest.raises(ValueError):
        Frame(0x40, Address(0, 0))
    with pytest.raises(ValueError):
        Frame(0, Address(0, 0), bytes(9))
    with pytest.raises(ValueError):
        Frame.unpack(bytes.fromhex("c368cad909") + bytes(9))


def test_unpack_captured_report():
    report = unpack_report(CAPTURED)
    assert len(report.frames) == 1
    assert report.frames[0] == Frame(0x30, Address(0x1B, 0x0CAD9), bytes.fromhex("0100"), timestamp=0xFF)
    assert report.credits == 18
    assert report.flags == 0


def test_unpack_report_extended_timestamp():
    payload = bytearray(CAPTURED)
    payload[60] = 0x12
    assert unpack_report(bytes(payload)).frames[0].timestamp == 0x12FF


def test_unpack_report_count_limit():
    payload = bytearray(CAPTURED)
    payload[0] = 5
    with pytest.raises(ValueError):
        unpack_report(bytes(payload))


def test_pack_report():
    enumerate_ = Frame(KIND_ENUMERATE, Address(0x14, 0x1234))
    payload = pack_report([enumerate_])
    assert len(payload) == 64
    assert payload[0] == 1
    assert payload[1:15] == bytes.fromhex("2a801234") + bytes(10)
    assert payload[15:] == bytes(49)
    assert unpack_report(payload).frames == [enumerate_]


def test_pack_report_limits():
    f = Frame(KIND_REPLY, Address(0, 0))
    with pytest.raises(ValueError):
        pack_report([])
    with pytest.raises(ValueError):
        pack_report([f] * 5)
    four = pack_report([f] * 4)
    assert four[0] == 4 and len(four) == 64


def test_a_link_without_readable_strings_is_named_by_its_path():
    """libusb leaves a link's strings empty where this user may not open it: no empty serial, no '()'."""
    from mvconnect.hidlink import LinkInfo, describe_link
    unreadable = describe_link(LinkInfo(b"2-1:1.0", "", "", ""))
    assert "()" not in unreadable and "  " not in unreadable and "serial" not in unreadable
    assert "2-1:1.0" in unreadable and "unreadable" in unreadable and "MasterBus USB Link" in unreadable
    known = describe_link(LinkInfo(b"/dev/hidraw0", "F226", "MasterBus USB Link", "Mastervolt"))
    assert known == "MasterBus USB Link serial F226 (Mastervolt) at /dev/hidraw0"


@pytest.mark.parametrize("path", [b"2-1:1.0", b"/dev/hidraw1"])
def test_a_link_that_cannot_be_opened_is_one_concise_line(monkeypatch, caplog, path):
    """The error names the link and the reason the system gives, and no more: who may open what, and why, is for
    --debug, and no remedy is offered that may not fit the system."""
    import logging

    from mvconnect import hidlink
    from mvconnect.hidlink import HidLink, LinkError, LinkInfo

    class Unopenable:
        def open_path(self, path):
            raise OSError("open failed")
    monkeypatch.setattr(hidlink.hid, "device", Unopenable)
    monkeypatch.setattr(hidlink, "_access", lambda path: "this process may read and NOT write it")
    for platform in ("linux", "darwin"):
        monkeypatch.setattr(hidlink.sys, "platform", platform)
        with caplog.at_level(logging.DEBUG, logger="mvconnect.hidlink"), pytest.raises(LinkError) as e:
            HidLink(LinkInfo(path, "", "", "")).open(lambda frame: None)
        assert str(e.value) == f"cannot open the MasterBus USB Link at {path.decode()}: open failed"
        assert not hasattr(e.value, "hint")
    assert any("NOT write" in r.getMessage() for r in caplog.records if r.levelname == "DEBUG")  # the details
