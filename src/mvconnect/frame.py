"""MasterBus CAN records as the MasterBus USB Link carries them.

One HID report of 64 payload bytes holds up to four 14-byte records:

    payload[0]        number of records, 1..4
    payload[1+14*i]   record i
    payload[58]       in reports from the link: free transmit slots in the link
    payload[59]       in reports from the link: status flags
    payload[60+i]     in reports from the link: low timestamp byte of record i,
                      whose own timestamp byte is then the high one

A record is a big-endian 32-bit id word, a data length, 8 data bytes and a
timestamp byte. The id word packs a 6-bit kind, a 5-bit high address field and
an 18-bit low address field:

    bits 31..26  kind
    bits 25..21  address high (5 bits)
    bits 20..18  unused
    bits 17..0   address low (18 bits)
"""

from __future__ import annotations

from dataclasses import dataclass, field

RECORD_SIZE = 14
MAX_RECORDS = 4
PAYLOAD_SIZE = 64

# Record kinds seen on the bus
KIND_ANNOUNCE = 0x08  # reply to ENUMERATE: data = hi, lo (3 bytes LE), 4 info bytes
KIND_ENUMERATE = 0x0A  # DLC 0 broadcast: every device answers with ANNOUNCE
KIND_REPLY = 0x0C  # reply to a REQUEST, same command byte in data[0]
KIND_NA = 0x0D  # "not available" reply to a REQUEST
KIND_REQUEST = 0x0E  # data[0] = command, see masterbus.py

# A login is the device's number of kind 0x19 written (masterbus.Number.LOGIN; see masterbus.MasterBus.login):
# the command, the kind, the level in two bytes, then the password as a float, which no log may show.
LOGIN = bytes([0x08, 0x19])


@dataclass(frozen=True, order=True)
class Address:
    """A MasterBus device address: a 5-bit high field and an 18-bit low field.

    Raise ValueError if either field is out of range.
    """

    hi: int
    lo: int

    def __post_init__(self):
        if not 0 <= self.hi < 0x20:
            raise ValueError(f"address high field out of range: {self.hi:#x}")
        if not 0 <= self.lo < 0x40000:
            raise ValueError(f"address low field out of range: {self.lo:#x}")

    def __str__(self) -> str:
        return f"{self.hi:02x}:{self.lo:05x}"

    @classmethod
    def parse(cls, text: str) -> Address:
        """Return the Address *text* names, written as str() writes it: 'hh:lllll' in hex.

        Raise ValueError if *text* is not of that form or a field is out of range.
        """
        hi, _, lo = text.partition(":")
        if not lo:
            raise ValueError(f"not an address: {text!r}, expected hh:lllll")
        return cls(int(hi, 16), int(lo, 16))


@dataclass
class Frame:
    """One CAN record.

    It holds its 6-bit *kind*, the *address* of the device it is for or from, up to 8 *data* bytes and a
    *timestamp*, one byte in a record and two in a report from the link (unpack_report() adds the low byte).
    Raise ValueError if the kind or the data length is out of range.
    """
    kind: int
    address: Address
    data: bytes = b""
    timestamp: int = 0

    def __post_init__(self):
        if not 0 <= self.kind < 0x40:
            raise ValueError(f"kind out of range: {self.kind:#x}")
        self.data = bytes(self.data)
        if len(self.data) > 8:
            raise ValueError(f"data longer than 8 bytes: {self.data.hex()}")

    @property
    def secret(self) -> bool:
        """Return whether the frame carries a login's password: a login request, or a reply echoing one."""
        return self.kind in (KIND_REQUEST, KIND_REPLY, KIND_NA) and self.data[:2] == LOGIN and len(self.data) > 4

    def data_text(self) -> str:
        """Return the data bytes in hex, a login's password as ** for each of its bytes."""
        shown = [f"{b:02x}" for b in self.data]
        if self.secret:
            shown[4:] = ["**"] * (len(shown) - 4)
        return " ".join(shown)

    @property
    def word(self) -> int:
        """Return the 32-bit id word: the kind and the address packed as the link carries them."""
        return (self.kind << 26) | (self.address.hi << 21) | self.address.lo

    def pack(self) -> bytes:
        """Return the 14 bytes of the record as the link carries it."""
        return (self.word.to_bytes(4, "big") + bytes([len(self.data)]) + self.data.ljust(8, b"\0")
                + bytes([self.timestamp & 0xFF]))

    @classmethod
    def unpack(cls, record: bytes) -> Frame:
        """Return the Frame a 14-byte *record* holds.

        Raise ValueError if the record is short or its data length exceeds 8.
        """
        if len(record) < RECORD_SIZE:
            raise ValueError(f"record too short: {record.hex()}")
        word = int.from_bytes(record[:4], "big")
        dlc = record[4]
        if dlc > 8:
            raise ValueError(f"DLC out of range: {dlc}")
        return cls(kind=word >> 26, address=Address((word >> 21) & 0x1F, word & 0x3FFFF),
                   data=record[5:5 + dlc], timestamp=record[13])

    def __str__(self) -> str:
        return f"{self.kind:#04x} {self.address} [{self.data_text()}]"  # what every log shows of a frame


@dataclass
class Report:
    """The decoded content of one report from the link.

    It holds the report's *frames*, the link's count of free transmit slots (*credits*) and its status *flags*
    byte.
    """

    frames: list[Frame] = field(default_factory=list)
    credits: int = 0
    flags: int = 0


def pack_report(frames: list[Frame]) -> bytes:
    """Return the 64 payload bytes of an output report carrying *frames*, 1 to 4 of them.

    Raise ValueError for any other number of frames.
    """
    if not 1 <= len(frames) <= MAX_RECORDS:
        raise ValueError(f"a report carries 1 to {MAX_RECORDS} frames, not {len(frames)}")
    payload = bytes([len(frames)]) + b"".join(f.pack() for f in frames)
    return payload.ljust(PAYLOAD_SIZE, b"\0")


def unpack_report(payload: bytes) -> Report:
    """Return the Report decoded from *payload*, the 64 payload bytes of an input report.

    Slots beyond the record count hold stale data and are ignored. Each frame's timestamp gets its low byte
    from the report, the record's own byte becoming the high one. Raise ValueError if the payload is short,
    the count exceeds 4 or a record's data length exceeds 8.
    """
    if len(payload) < PAYLOAD_SIZE:
        raise ValueError(f"report payload too short: {len(payload)} bytes")
    count = payload[0]
    if count > MAX_RECORDS:
        raise ValueError(f"record count out of range: {count}")
    frames = []
    for i in range(count):
        frame = Frame.unpack(payload[1 + RECORD_SIZE * i:1 + RECORD_SIZE * (i + 1)])
        frame.timestamp = frame.timestamp << 8 | payload[60 + i]
        frames.append(frame)
    return Report(frames=frames, credits=payload[58], flags=payload[59])
