"""What a frame means, as far as it is known: the monitor's reading of the bus.

describe() turns one frame into words; line() lays a frame out as one line of
a listing: when, which way, the raw kind, address and data bytes, the node's
name, then the meaning. A login's password is never shown, neither as bytes
nor in words.
"""

from __future__ import annotations

import struct

from mvconnect.frame import KIND_ANNOUNCE, KIND_ENUMERATE, KIND_NA, KIND_REPLY, KIND_REQUEST, Address, Frame
from mvconnect.firmware import Loader
from mvconnect.flasher import ACK_KIND, FLASH_KIND, NAK_KIND, SEL_NAMES
from mvconnect.masterbus import AnnounceInfo, Command, Identity, Number, login_level_name, version_text
from mvconnect.pages import MASTER_ADDRESS_TYPE, PAGES, Attribute, entry_address

KIND_EVENT = 0x00  # an eventable value, broadcast by the device: [idx16, float32]
IDENTITY: dict[int, str] = {kind: kind.name.lower().replace("_", " ") for kind in Identity}
LOADER: dict[int, str] = {kind: f"loader {kind.name.lower().replace('_', ' ')}" for kind in Loader}

NUMBERS: dict[int, str] = {
    0x01: "monitor values", 0x02: "monitor groups", 0x03: "history values", 0x04: "history groups",
    0x05: "config values", 0x06: "config groups", 0x07: "alarm values", 0x08: "alarm groups",
    Number.VERSION: "version", Number.LOGIN: "login level", 0x1C: "monitor version", 0x1D: "history version",
    0x1E: "config version", 0x1F: "alarm version", Number.STATUS_VERSION: "status version"}
ATTRS: dict[int, str] = {attr: attr.name.lower().replace("_", " ") for attr in Attribute}
FLOAT_ATTRS = {Attribute.MINIMUM, Attribute.MAXIMUM, Attribute.STEP, Attribute.DEFAULT}
# Asked for by a sub index: a group's member, and the entries of a list, command and device value
SUB_ATTRS = {Attribute.MEMBERS, Attribute.OPTIONS, Attribute.COMMANDS, Attribute.TARGETS}

# kind -> (page, role, answer): role is read, info or group; answer is "" for the request,
# "reply" or "na" for what comes back (the request's kind - 0x20, - 0x10)
_PAGE_KINDS: dict[int, tuple[str, str, str]] = {
    kind + shift: (page.name, role, answer)
    for page in PAGES for role in ("read", "info", "group") if (kind := getattr(page, role)) is not None
    for shift, answer in ((0, ""), (-0x20, "reply"), (-0x10, "na"))
}


def _u16(data: bytes, at: int) -> int:
    return data[at] | data[at + 1] << 8


def _float(data: bytes, at: int) -> str:
    return f"{struct.unpack_from('<f', data, at)[0]:g}"


def _number(kind: int) -> str:
    name = NUMBERS.get(kind) or (f"eventable {kind - 0x40}" if 0x40 <= kind < 0x60 else "")
    return f"number {kind:#04x}" + (f" {name}" if name else "")


def _command(data: bytes) -> str:
    """Name the command in *data*, of a request or of the reply to one, by its first bytes, without its value."""
    cmd = data[0]
    if cmd == Command.NUMBER and len(data) > 1:
        return _number(data[1])
    if cmd == Command.IDENTITY and len(data) > 1:
        return f"identity {data[1]} {IDENTITY.get(data[1], '')}".rstrip()
    if cmd == Command.STRING and len(data) > 3:
        return f"string {_u16(data, 1):#06x} chunk {data[3]}"
    return LOADER.get(cmd, f"command {cmd:#04x}")


def _request(data: bytes) -> str:
    if not data:
        return "empty request"
    cmd = data[0]
    if cmd == Command.NUMBER and len(data) >= 4:
        if data[1] == Number.LOGIN:
            level = _u16(data, 2)
            return "logout" if level == 0 and len(data) == 4 else f"login {login_level_name(level)}, password ****"
        return f"{_number(data[1])} := {_u16(data, 2)}"
    if cmd == Command.STRING and len(data) > 4:
        return f"{_command(data)} := {data[4:].decode('latin-1')!r}"
    return _command(data) + ("?" if cmd in (Command.NUMBER, Command.IDENTITY, Command.STRING) else "")


def _reply(data: bytes) -> str:
    if not data:
        return "empty reply"
    cmd = data[0]
    if cmd == Command.NUMBER and len(data) >= 4:
        value = _u16(data, 2)
        if data[1] == Number.VERSION:
            shown = version_text(value)
        elif data[1] == Number.LOGIN:
            shown = login_level_name(value)
        else:
            shown = str(value)
        return f"{_number(data[1])} = {shown}"
    if cmd == Command.IDENTITY and len(data) >= 4:
        return f"{_command(data)} = string {_u16(data, 2):#06x}"
    if cmd == Command.STRING and len(data) >= 4:
        text = data[4:].split(b"\0")[0].decode("latin-1")  # outside the f-string: Python 3.11 allows no backslash
        return f"{_command(data)} = {text!r}"
    return f"reply to {_command(data)}"


def _is_address_frame(data: bytes) -> bool:
    # The frame MasterAdjust sends after writing an eventable value: its own address, 0x14 then the low field.
    # As a float, these bytes would be a denormal no write uses: the top address byte is at most 3.
    return len(data) == 6 and data[2] == MASTER_ADDRESS_TYPE and data[5] <= 0x03


def _a_length(data: bytes) -> bool:
    """Tell whether *data*, a 6-byte reply on a group kind, is the group's length rather than one of its attributes.

    Both are 6 bytes, and a length is a whole number of members as a float, from 0 to 256, where an
    attribute's bytes read as a float are almost never one. All zero behind a first byte of 0x03 or 0x28 is
    taken as the attribute: a group's first member being value 0, or its name being string 0, is likelier than
    group 3 or 40 being empty.
    """
    if data[0] in (0x03, 0x28) and not any(data[1:]):
        return False
    length = struct.unpack_from("<f", data, 2)[0]
    return length == length and 0 <= length <= 256 and length == int(length)


def _page(frame: Frame, page: str, role: str, answer: str) -> str:
    data = frame.data
    if len(data) < 2:
        return f"{page} {role} {data.hex(' ')}"
    if role == "read":
        where = f"{page} #{_u16(data, 0)}"
        if answer == "na":
            return f"{where} not available"
        if _is_address_frame(data):
            low = int.from_bytes(data[3:6], "little")
            return f"{where} writer's address {'=' if answer else ':='} {MASTER_ADDRESS_TYPE:02x}:{low:05x}"
        if len(data) >= 6:
            return f"{where} {'=' if answer else ':='} {_float(data, 2)}"
        return f"{where}?"
    if role == "group" and (len(data) == 2 or (len(data) == 6 and answer == "reply" and _a_length(data))):
        where = f"{page} group #{_u16(data, 0)} length"
        if answer == "na":
            return f"{where} not available"
        return f"{where} = {_float(data, 2)}" if len(data) == 6 else f"{where}?"
    if len(data) < 3:
        return f"{page} {role} {data.hex(' ')}"
    attr, index = data[0], _u16(data, 1)
    sub = f" [{data[3]}]" if len(data) >= 4 and (attr in SUB_ATTRS or len(data) == 4 or data[3]) else ""
    where = f"{page} {'group ' if role == 'group' else ''}#{index} attr {attr:#04x} {ATTRS.get(attr, '')}".rstrip()
    if answer == "na":
        return f"{where}{sub} not available"
    if not answer:
        return f"{where}{sub}?"
    if len(data) >= 8 and attr in FLOAT_ATTRS:
        return f"{where}{sub} = {_float(data, 4)}"
    if len(data) >= 8 and attr == Attribute.TARGETS:
        return f"{where}{sub} = {entry_address(data) or 'none'}"
    if len(data) >= 6:
        return f"{where}{sub} = {_u16(data, 4):#06x}"
    return f"{where}{sub} = {data[4:].hex(' ')}"


def describe(frame: Frame) -> str:
    """Return what *frame* means, as far as it is known; '?' where it is not."""
    kind, data = frame.kind, frame.data
    if kind == KIND_ENUMERATE:
        return "enumerate"
    if kind == KIND_ANNOUNCE and len(data) == 8:
        ann = AnnounceInfo.parse(data)
        return f"announce {ann.address} info {ann.info.hex(' ')}{', flash mode' if ann.flash_mode else ''}"
    if kind == KIND_REQUEST:
        return _request(data)
    if kind == KIND_REPLY:
        return _reply(data)
    if kind == KIND_NA:
        return f"not available: {_command(data)}" if data else "not available"
    if kind == KIND_EVENT and len(data) >= 6:
        return f"event #{_u16(data, 0)} = {_float(data, 2)}"
    if kind == FLASH_KIND:
        return f"flash {SEL_NAMES.get(frame.address.hi, f'selector {frame.address.hi}')}"
    if kind == ACK_KIND:
        return "flash ack"
    if kind == NAK_KIND:
        return "flash nak"
    if kind in _PAGE_KINDS:
        return _page(frame, *_PAGE_KINDS[kind])
    return "?"


def raw(frame: Frame) -> str:
    """Return the data bytes of *frame* in hex, a login's password masked (see Frame.data_text())."""
    return frame.data_text()


def line(frame: Frame, sent: bool, elapsed: float, names: dict[Address, str]) -> str:
    """Return *frame* as one line of a listing.

    The line holds *elapsed*, the seconds since the start; tx if this computer sent the frame (*sent*), rx if
    not; the kind, address and data; the node's name from *names*, which maps addresses to names; and the
    meaning, as describe() gives it.
    """
    return (f"{elapsed:8.3f}  {'tx' if sent else 'rx'} {frame.kind:02x} {frame.address}  {raw(frame):<23}  "
            f"{names.get(frame.address, ''):<22} {describe(frame)}")
