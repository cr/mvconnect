"""Firmware files and the bootloader's identification queries.

Mastervolt ships firmware as Intel HEX. Somewhere in the image sits a
meta section, between two markers of 7 bytes each. ARM images hold it as plain bytes;
Microchip PIC24/dsPIC images, whose HEX files store 24-bit words in
32-bit cells, hold it two bytes per cell in the low half:

    4c 70 f0 7c 0f c0 3f   <record>   63 c1 f8 0f f0 07 fe

In the record, as verified against CHM1_V8_34.hex and the CHG it runs on:

    +0   u16  1 (format or processor number; meaning not confirmed)
    +2   u32  product id: the product number as a number (44010500)
    +6   u16  hardware version? (0x0100 on the CHG file)
    +8   u8   0
    +9   u16  firmware version, major in the high byte (0x0822 = 8.34)
    +11  u16  0
    +13  u32  image start address (0x08003000)
    +17  u32  image end address (0x08020000)
    +21  u16  0x0880 (meaning not confirmed)
    +23  ...  the rest is not decoded yet (likely a CRC)

A format-2 record (+0 is 2, 39 bytes) moves the image start two bytes up, to +11, as its files show: a
start inside the file's own range there, where +13 gives a number such as 0x801. Its end stays at +17.

Devices answer three bootloader queries, as plain requests (kind 0x0e):

    80            -> 80 u32 product id
    81 proc       -> 81 proc u16 hardware version
    82 proc       -> 82 proc u32 software version
"""

from __future__ import annotations

import enum
import logging
import struct
from dataclasses import dataclass, field
from pathlib import Path

from mvconnect.frame import Address
from mvconnect.masterbus import MasterBus, version_text

logger = logging.getLogger(__name__)

META_START = bytes.fromhex("4c70f07c0fc03f")
META_END = bytes.fromhex("63c1f80ff007fe")

MAX_SPAN = 64 << 20  # the widest span of addresses an image may cover; Mastervolt's widest is 44 MiB

class Loader(enum.IntEnum):
    """The bootloader's requests (kind 0x0e), by their first data byte.

    The first three only ask; the others take the device into flash mode and out of it (see
    mvconnect.flasher).
    """
    PRODUCT_ID = 0x80  # -> 80 u32 product id
    HARDWARE = 0x81  # + processor -> 81 proc u16 hardware version
    SOFTWARE = 0x82  # + processor -> 82 proc u32 software version
    ARM = 0x83  # + META_START: the first half of the way into flash mode
    ENTER = 0x84  # + META_END: the second half, once armed
    LEAVE = 0x85  # + processor + software version (LE): out of flash mode, once a 'done' armed it


class FirmwareError(Exception):
    """A firmware file cannot be read, or has no meta section this module understands."""


def parse_intel_hex(text: str) -> dict[int, int]:
    """Return a dict from address to byte for every data record of the Intel HEX *text*.

    Record types 1, 2 and 4 are honoured, others ignored. Raise FirmwareError as parse_intel_hex_image() does.
    """
    lo, hi, buf = parse_intel_hex_image(text)
    return {lo + i: b for i, b in enumerate(buf)}


def parse_intel_hex_image(text: str) -> tuple[int, int, bytes]:
    """Return (lowest address, highest address, image bytes) for the Intel HEX *text*.

    The image has 0xFF where the file defines nothing. Raise FirmwareError on a malformed line, a file without
    data records, or records that span more than MAX_SPAN bytes of addresses.
    """
    records: list[tuple[int, bytes]] = []
    base = 0
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if not line.startswith(":"):
            raise FirmwareError(f"line {n}: not an Intel HEX record")
        try:
            rec = bytes.fromhex(line[1:])
        except ValueError:
            raise FirmwareError(f"line {n}: bad hex digits") from None
        if len(rec) < 5 or len(rec) != rec[0] + 5:
            raise FirmwareError(f"line {n}: length mismatch")
        if sum(rec) & 0xFF:
            raise FirmwareError(f"line {n}: checksum mismatch")
        addr, typ, data = int.from_bytes(rec[1:3], "big"), rec[3], rec[4:-1]
        if typ == 0:
            records.append((base + addr, data))
        elif typ == 1:
            break
        elif typ == 4:
            base = int.from_bytes(data, "big") << 16
        elif typ == 2:
            base = int.from_bytes(data, "big") << 4
    if not records:
        raise FirmwareError("no data records")
    lo = min(a for a, _ in records)
    hi = max(a + len(d) for a, d in records) - 1
    if hi - lo + 1 > MAX_SPAN:  # the image is one buffer over the whole span, gaps filled
        raise FirmwareError(f"the records span {(hi - lo + 1) / 2**20:.0f} MiB of addresses, more than the "
                            f"{MAX_SPAN // 2**20} MiB a firmware image is taken to have")
    buf = bytearray(b"\xff" * (hi - lo + 1))
    for a, d in records:
        buf[a - lo:a - lo + len(d)] = d
    return lo, hi, bytes(buf)


@dataclass
class Meta:
    """The meta record of a firmware file.

    It holds the record's *product_id*; the firmware *version*; the image's *start* and *end* addresses; the
    *hardware* version; the *offset* of the marker in the image; the record's *raw* bytes; its *format* word,
    1 for a 27-byte record and 2 for 39 bytes; and its *packing*, 'plain' bytes or 'low16', two bytes per
    32-bit cell as Microchip PIC24/dsPIC images hold it.
    """
    product_id: int
    version: int
    start: int
    end: int
    hardware: int
    offset: int
    raw: bytes = field(repr=False, default=b"", compare=False)
    format: int = 1
    packing: str = "plain"

    @property
    def product(self) -> str:
        """Return the product number as text, such as '44010500'."""
        return str(self.product_id)

    @property
    def version_text(self) -> str:
        """Return the version as text, or '?' for a record without one."""
        return version_text(self.version) if self.version else "?"


def find_meta(img: bytes, base: int) -> Meta | None:
    """Return the Meta record in *img*, an image starting at address *base*, or None when there is none.

    The record is looked for in plain bytes (ARM parts) and packed two bytes per 32-bit cell (Microchip parts,
    whose HEX files hold 24-bit words in 32-bit cells with the data in the low half); None is returned when
    neither packing shows the marker. Raise FirmwareError if the marker starts a record without an end or too
    short to read.
    """
    low16 = b"".join(img[k:k + 2] for k in range(0, len(img) - 3, 4))
    for packing, buf, scale in (("plain", img, 1), ("low16", low16, 2)):
        i = buf.find(META_START)
        if i < 0:
            continue
        j = buf.find(META_END, i)
        if j < 0:
            raise FirmwareError("meta section has no end marker")
        rec = buf[i + len(META_START):j]
        if len(rec) < 11:
            raise FirmwareError(f"meta section too short ({len(rec)} bytes)")
        if len(rec) < 21:
            # the TI DSP halves of Combi and ChargeMaster Plus: product id and a hardware word only;
            # the two bytes at +9 are not the version (CM2P_DSP_02_88.hex has 0x077a there)
            return Meta(product_id=struct.unpack_from("<I", rec, 2)[0], hardware=struct.unpack_from("<H", rec, 6)[0],
                        version=0, start=0, end=0, offset=base + i * scale, raw=rec,
                        format=struct.unpack_from("<H", rec, 0)[0], packing=packing)
        form = struct.unpack_from("<H", rec, 0)[0]
        return Meta(product_id=struct.unpack_from("<I", rec, 2)[0], hardware=struct.unpack_from("<H", rec, 6)[0],
                    version=struct.unpack_from("<H", rec, 9)[0],
                    start=struct.unpack_from("<I", rec, 11 if form == 2 else 13)[0],
                    end=struct.unpack_from("<I", rec, 17)[0], offset=base + i * scale, raw=rec,
                    format=form, packing=packing)
    return None


@dataclass
class Firmware:
    """A firmware file.

    It holds the file's *path*, the *image* bytes from *base* upwards with 0xFF where the file defines
    nothing, and its *meta* record.
    """
    path: Path
    image: bytes
    meta: Meta
    base: int = 0

    @classmethod
    def load(cls, path: str | Path) -> Firmware:
        """Return the Firmware read from the Intel HEX file at *path*.

        Raise FirmwareError if the file is malformed or has no meta section marker, and OSError if it cannot
        be read.
        """
        path = Path(path)
        lo, hi, img = parse_intel_hex_image(path.read_text(errors="replace"))
        meta = find_meta(img, lo)
        if meta is None:
            raise FirmwareError(f"{path.name}: no meta section marker")
        return cls(path, img, meta, lo)

    @property
    def size(self) -> int:
        """Return the image's length in bytes."""
        return len(self.image)


@dataclass
class LibraryEntry:
    """A file in the library as the index remembers it.

    It holds the file's *path*, its *meta* record (None when it has no readable meta section) and the *error*
    that says why.
    """

    path: Path
    meta: Meta | None
    error: str = ""


INDEX_FORMAT = 2  # of index.json: raised whenever decoding a file changes what it gives


def library(directory: str | Path, with_errors: bool = False) -> list[LibraryEntry]:
    """Return the firmware files of *directory*, newest version first per product.

    Decoding is cached in index.json next to the files, keyed by size and mtime, since decoding 80 HEX files
    takes seconds; an index of another INDEX_FORMAT, written before the decoding changed, is not used. With
    *with_errors* the files without a readable meta section come too, as LibraryEntry with *meta* None;
    otherwise only the decoded files.
    """
    import json
    directory = Path(directory)
    index_path = directory / "index.json"
    try:
        stored = json.loads(index_path.read_text())
        index = stored["files"] if stored.get("format") == INDEX_FORMAT else {}
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        index = {}
    entries = []
    changed = False
    for p in sorted(p for p in directory.rglob("*") if p.suffix.lower() == ".hex" and p.is_file()):  # .HEX too
        st = p.stat()
        key = f"{st.st_size}:{int(st.st_mtime)}"
        cached = index.get(str(p.relative_to(directory)))
        if cached and cached.get("key") == key:
            try:
                meta = Meta(**cached["meta"]) if cached.get("meta") else None
                entries.append(LibraryEntry(p, meta, cached.get("error", "")))
                continue
            except TypeError:  # fields this Meta does not have: decode the file again
                pass
        try:
            fw = Firmware.load(p)
            meta, error = fw.meta, ""
        except (FirmwareError, OSError) as e:
            meta, error = None, str(e)
            logger.debug(f"{p.name}: {e}")
        entries.append(LibraryEntry(p, meta, error))
        index[str(p.relative_to(directory))] = {"key": key, "error": error,
                         "meta": None if meta is None else {k: v for k, v in vars(meta).items() if k != "raw"}}
        changed = True
    for name in list(index):
        if not (directory / name).exists():
            del index[name]
            changed = True
    if changed:
        try:
            index_path.write_text(json.dumps({"format": INDEX_FORMAT, "files": index}))
        except OSError as e:
            logger.debug(f"cannot write {index_path}: {e}")
    skipped = sum(1 for e in entries if e.meta is None)
    if skipped:
        logger.debug(f"{skipped} of {len(entries)} files have no meta section mvconnect can read")
    if not with_errors:
        entries = [e for e in entries if e.meta is not None]
    entries.sort(key=lambda e: (e.meta.product_id if e.meta else 1 << 40, -(e.meta.version if e.meta else 0),
                                str(e.path)))
    return entries


# -- the bootloader's identification queries

@dataclass
class Processor:
    """One processor of a device as the bootloader reports it.

    It holds the processor's *number*, and its *hardware* and *software* versions, None where unanswered.
    """
    number: int
    hardware: int | None = None
    software: int | None = None


@dataclass
class Identification:
    """What the bootloader's queries say about a device.

    It holds the device's *product_id*, None when unanswered, and its *processors*.
    """
    product_id: int | None = None
    processors: list[Processor] = field(default_factory=list)


def identify(bus: MasterBus, address: Address, max_processors: int = 8) -> Identification:
    """Return the Identification of the device at *address* on *bus*.

    It holds the device's product id and the hardware and software version of each of up to *max_processors*
    processors; the processors end at the first that does not answer. Read-only; a device that does not
    answer the product id query gives an empty one.
    """
    ident = Identification()
    try:
        reply = bus.request(address, bytes([Loader.PRODUCT_ID]), echo=1)
        if len(reply) >= 5:
            ident.product_id = struct.unpack_from("<I", reply, 1)[0]
    except TimeoutError:
        logger.info(f"{address}: no answer to the product id query")
        return ident
    for n in range(max_processors):
        proc = Processor(n)
        try:
            reply = bus.request(address, bytes([Loader.HARDWARE, n]), echo=2)
            if len(reply) >= 4:
                proc.hardware = struct.unpack_from("<H", reply, 2)[0]
            reply = bus.request(address, bytes([Loader.SOFTWARE, n]), echo=2)
            if len(reply) >= 6:
                proc.software = struct.unpack_from("<I", reply, 2)[0]
            elif len(reply) >= 4:
                proc.software = struct.unpack_from("<H", reply, 2)[0]
        except TimeoutError:
            break
        ident.processors.append(proc)
    return ident
