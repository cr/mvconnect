"""The monitor's reading of frames: raw bytes beside what they mean, where that is known."""

import struct

import pytest

from expect import has
from mvconnect.decode import describe, line
from mvconnect.frame import Address, Frame
from mvconnect.masterbus import HOST_ADDRESS

MSH = Address(0x1B, 0x0CAD9)
NAMES = {MSH: "MSH House Bank", HOST_ADDRESS: "this computer"}


def f32(v):
    return struct.pack("<f", v)


@pytest.mark.parametrize(
    "kind, data, things",
    [
        (0x0A, b"", ("enumerate",)),
        (0x0E, bytes([0x08, 0x0B]), ("number", "0x0b", "version")),
        (0x0E, bytes([0x83]) + bytes(7), ("loader", "arm")),
        (0x0E, bytes([0x80]), ("loader", "product id")),
        (0x0C, bytes([0x08, 0x0B, 0x22, 0x08]), ("version", "8.34")),
        (0x0C, bytes([0x08, 0x1E, 7, 0]), ("config", "version", 7)),
        (0x0E, bytes([0x08, 0x19]), ("login",)),
        (0x0C, bytes([0x08, 0x19, 1, 0]), ("login", "installer")),
        (0x0E, bytes([0x08, 0x19, 0, 0]), ("logout",)),
        (0x0E, bytes([0x09, 0x02]), ("identity", "serial")),
        (0x0C, bytes([0x09, 0x02, 0x00, 0x00]), ("serial", "0x0000")),
        (0x0E, bytes([0x30, 0xDB, 0x00, 0x01]), ("0x00db", 1)),
        (0x0C, bytes([0x30, 0xDB, 0x00, 0x01]) + b"Hous", ("0x00db", "Hous")),
        (0x0E, bytes([0x30, 0xDB, 0x00, 0x00]) + b"Ho", ("0x00db", "Ho")),
        (0x0D, bytes([0x08, 0x19, 0, 0]), ("not available", "login")),
        (0x30, bytes([0x00, 0x00]), ("monitor", "#0")),
        (0x10, bytes([0x00, 0x00]) + f32(84.5), ("monitor", "#0", 84.5)),
        (0x20, bytes([0x05, 0x00]), ("monitor", "#5", "not available")),
        (0x37, bytes([0x20, 0x00]) + f32(7.0), ("config", "#32", 7)),
        # the frame MasterAdjust sends after writing an eventable value, to the next index: its own address
        (0x37, bytes([0x53, 0x00, 0x14, 0x60, 0xA7, 0x01]), ("config", "#83", "14:1a760")),
        # the entries of a device value: addresses, or none
        (0x18, bytes([0x20, 0x52, 0x00, 0x01, 0x1B, 0xD9, 0xCA, 0x00]), ("config", "#82", "targets", "[1]", "1b:0cad9")),
        (0x18, bytes([0x20, 0x52, 0x00, 0x00, 0xFF, 0xFF, 0xFF, 0xFF]), ("config", "#82", "targets", "[0]", "none")),
        (0x38, bytes([0x0F, 0x53, 0x00, 0x01]), ("config", "#83", "commands", "[1]")),
        (0x31, bytes([0x28, 0x03, 0x00]), ("monitor", "#3", "name")),
        (0x11, bytes([0x28, 0x03, 0x00, 0x00, 0x25, 0x00]), ("monitor", "#3", "0x0025")),
        (0x38, bytes([0x26, 0x02, 0x00, 0x01]), ("config", "#2", "options")),
        (0x11, bytes([0x07, 0x00, 0x00, 0x00]) + f32(100.0), ("monitor", "#0", "maximum", 100)),
        (0x39, bytes([0x01, 0x00]), ("config", "group", "#1", "length")),
        (0x19, bytes([0x01, 0x00]) + f32(2.0), ("config", "group", "#1", "length", 2)),
        (0x39, bytes([0x03, 0x01, 0x00, 0x02]), ("config", "group", "#1")),
        # a group's member, answered: as long as a length reply, and no length
        (0x19, bytes([0x03, 0x01, 0x00, 0x02, 0x10, 0x00]), ("config", "group", "#1", "0x03", "0x0010")),
        (0x19, bytes([0x28, 0x01, 0x00, 0x00, 0x96, 0x00]), ("config", "group", "#1", "name", "0x0096")),
        # all zero: group 0's first member is value 0, and its name string 0; neither is a length
        (0x19, bytes([0x03, 0x00, 0x00, 0x00, 0x00, 0x00]), ("config", "group", "#0", "0x03", "[0]", "0x0000")),
        (0x19, bytes([0x28, 0x00, 0x00, 0x00, 0x00, 0x00]), ("config", "group", "#0", "name", "0x0000")),
        (0x18, bytes([0x26, 0x02, 0x00, 0x00, 0xE0, 0x00]), ("config", "#2", "options", "[0]", "0x00e0")),
        (0x00, bytes([0x04, 0x00]) + f32(13.25), ("event", "#4", 13.25)),
    ],
)
def test_describe_names_what_a_frame_is_and_what_it_carries(kind, data, things):
    assert has(describe(Frame(kind, MSH, data)), *things)


def test_an_unknown_frame_is_a_question_mark():
    assert describe(Frame(0x3F, MSH, bytes([1, 2]))) == "?"  # describe()'s documented answer for the unknown


def test_announce_names_the_device_and_its_flash_mode():
    normal = describe(Frame(0x08, MSH, bytes([0x1B, 0xD9, 0xCA, 0x00, 0x00, 0x02, 0x00, 0x00])))
    flashing = describe(Frame(0x08, MSH, bytes([0x1B, 0xD9, 0xCA, 0x00, 0x00, 0x02, 0x20, 0x00])))
    assert has(normal, "1b:0cad9") and not has(normal, "flash")
    assert has(flashing, "1b:0cad9", "flash")


def test_flash_frames():
    assert has(describe(Frame(0x3D, Address(2, 0), b"\x00\x01")), "bank-select")
    assert has(describe(Frame(0x1D, MSH, b"")), "ack")
    assert has(describe(Frame(0x2D, MSH, b"")), "nak")


def test_a_login_never_shows_its_password():
    frame = Frame(0x0E, MSH, bytes([0x08, 0x19, 2, 0]) + f32(1234.0))
    text = line(frame, sent=True, elapsed=1.5, names=NAMES)
    assert has(describe(frame), "distributor") and "1234" not in describe(frame)
    assert f32(1234.0).hex(" ") not in text and "1234" not in text
    assert "08 19 02 00" in text  # the bytes before the password are shown


def test_a_line_carries_time_direction_kind_address_bytes_name_and_meaning():
    frame = Frame(0x10, MSH, bytes([0x00, 0x00]) + f32(84.5))
    text = line(frame, sent=False, elapsed=12.25, names=NAMES)
    assert has(text, "rx", "10", "1b:0cad9", "00 00 00 00 a9 42", "MSH House Bank", 84.5) and "12.25" in text
    sent = line(Frame(0x30, MSH, b"\x00\x00"), sent=True, elapsed=0.0, names=NAMES)
    assert has(sent, "tx") and sent.index("MSH House Bank") == text.index("MSH House Bank")  # in columns


def test_an_unknown_node_is_shown_by_its_address_only():
    text = line(Frame(0x0A, Address(1, 1), b""), sent=False, elapsed=0, names=NAMES)
    assert has(text, "01:00001", "enumerate")
