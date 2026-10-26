"""A device's property pages: monitor, alarm, history, configuration.

Every page is a table of values with an index each. Three request kinds
serve a page: one reads a value (DLC 2: index) and, with DLC 6 (index,
float32), writes it; one reads attributes of a value (DLC 3: attribute,
index; DLC 4 adds a sub-index); one reads attributes of a group. A device
answers with kind - 0x20, or kind - 0x10 when the value or attribute does
not exist or it refuses the value. Group attribute 0x03 with a sub-index
n gives the index of the group's n-th value.
"""

from __future__ import annotations

import datetime
import enum
import logging
import struct
from dataclasses import dataclass, field

from mvconnect.frame import Address, Frame
from mvconnect.masterbus import Identity, MasterBus, NotAvailable, Progress, sendable
from mvconnect.words import fold

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Page:
    """One of a device's four pages and the request kinds that serve it.

    *name* is the page's name. *read* reads a value and, with a float behind the index, writes it (None where
    values cannot be read); *info* reads a value's attributes; *group* reads a group's attributes.
    *count_values* and *count_groups* are the kinds of masterbus.Number for the numbers of values and groups,
    and *version* the kind for the page's version number, which changes when the page changes.
    """
    name: str
    read: int | None
    info: int
    group: int
    count_values: int
    count_groups: int
    version: int


PAGES = (
    Page("monitor", 0x30, 0x31, 0x32, 0x01, 0x02, 0x1C),
    Page("alarm", None, 0x33, 0x34, 0x07, 0x08, 0x1F),
    Page("history", 0x3B, 0x35, 0x36, 0x03, 0x04, 0x1D),
    Page("config", 0x37, 0x38, 0x39, 0x05, 0x06, 0x1E),
)
PAGE_BY_NAME = {p.name: p for p in PAGES}

class Attribute(enum.IntEnum):
    """The attributes a page's info and group requests ask for, by number.

    Each is named after the field of Value (or Group) it fills. A string's attribute gives the id of the
    string, read with Command.STRING.
    """
    VISUALIZATION = 0x02  # the VIS_* type: 1 number, 3 list of texts, 4 alarm...
    MEMBERS = 0x03  # a group's: with a sub-index n, the index of its n-th value
    MINIMUM = 0x06
    MAXIMUM = 0x07
    STEP = 0x08
    DEFAULT = 0x09
    WRITEABLE = 0x0B
    GRAY = 0x0C
    EVENTABLE = 0x0D
    COMMANDS = 0x0F  # with a sub-index n: the string id of entry n of a command value
    TARGETS = 0x20  # with a sub-index n: the address of entry n of a device value, in 4 bytes
    OPTIONS = 0x26  # with a sub-index n: the string id of option n
    NAME = 0x28  # the string id of the name
    LONG_NAME = 0x29  # the string id of a longer name
    DESCRIPTION = 0x2A  # the string id of the longest text, a description
    UNIT = 0x2C  # the string id of the unit

# Visualization types, as observed on MSH, CHG, APR and DIS devices and in how MasterAdjust
# shows and writes them: how a value is shown and, with it, how it may be written.
VIS_NUMBER = 1
VIS_FLAG = 2  # 0..2 status flags on the CHG
VIS_LIST = 3  # option texts via attribute 0x26
VIS_ALARM = 4
VIS_ACTION = 5  # 0..1 or 0..2 push buttons: "Reset setup", "Start test", "Clear history"
VIS_TEXT = 6  # a text such as "Device name"; the range is its length. MasterAdjust shows it, offers no edit
VIS_TIME = 7  # seconds: a time of day, or how long something lasted; read-only
VIS_DATE = 8
VIS_DEVICE = 9  # a device on the bus: "Event n target", "Master", "Slave n"; its entries are addresses (attribute 0x20)
VIS_COMMAND = 10  # "Event n command": a command of the target device; its entries are texts (attribute 0x0F)
LISTED = (VIS_LIST, VIS_DEVICE, VIS_COMMAND)  # values with entries
LIVE = (VIS_DEVICE, VIS_COMMAND)  # values whose entries follow the bus: read when needed, never kept in a table
NO_DEFAULT = 65535.0  # how devices say a value has no default
ENTRIES_MOST = 64  # how many entries a list is read for where its maximum does not say

# How a write reaches a value: the ordinary write, kind = page.read, DLC 6 [idx16, float32], for every type
# but text; MasterAdjust writes times and dates as numbers too, and device and command values as the
# entry's index. A text value holds the id of a device string; the text is written through the General
# String request, four characters per frame (MasterBus.write_string).
# After writing a switch, MasterAdjust sends one more frame to the NEXT index: kind = page.read, DLC 6
# [idx+1 as u16, 0x14, the low three bytes of its own address]: it names itself as the writer, and a relay
# or an action does not act until it has come (so another MasterBus implementation found). The EasyView
# lists the register at idx+1 as a value, unnamed, writeable, no range, 65535 as maximum, step and default,
# and echoes the frame. PageReader.name_writer() sends it, System.write() after every switch.
MASTER_ADDRESS_TYPE = 0x14  # the high field of a master's address (MasterAdjust, an EasyView): data[2] of that frame
VIS_NAMES = {VIS_NUMBER: "number", VIS_FLAG: "flag", VIS_LIST: "list", VIS_ALARM: "alarm", VIS_ACTION: "action",
             VIS_TEXT: "text", VIS_TIME: "time", VIS_DATE: "date", VIS_DEVICE: "device",
             VIS_COMMAND: "command"}
# One letter per type, for the attribute column of a listing (ls -l style)
VIS_LETTERS = {VIS_NUMBER: "n", VIS_FLAG: "f", VIS_LIST: "l", VIS_ALARM: "a", VIS_ACTION: "b", VIS_TEXT: "t",
               VIS_TIME: "T", VIS_DATE: "d", VIS_DEVICE: "D", VIS_COMMAND: "C"}


DATE_YEAR = 416  # a date value is year * 416 + month * 32 + day, 416 being 13 * 32


def date_text(value: float) -> str | None:
    """Return the date *value* holds as 'YYYY-MM-DD', or None if it holds no date.

    A date value is year * 416 + month * 32 + day; a value that is not a whole number or whose month or day
    is out of range holds no date.
    """
    # As another MasterBus implementation documents it; a MasterShunt's Date reads right.
    if value != value or not 0 < value < 1e9 or value != int(value):
        return None
    year, rest = divmod(int(value), DATE_YEAR)
    month, day = divmod(rest, 32)
    if not (year >= 1 and 1 <= month <= 12 and 1 <= day <= 31):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def date_value(text: str) -> float:
    """Return the number a date value holds for *text*, a date written 'YYYY-MM-DD'.

    Raise ValueError if *text* is not a date of that form, or no day of the calendar.
    """
    try:
        year, month, day = (int(part) for part in text.strip().split("-"))
        datetime.date(year, month, day)
    except ValueError:
        raise ValueError(f"{text!r} is not a date, expected YYYY-MM-DD") from None
    return float(year * DATE_YEAR + month * 32 + day)


def duration_text(seconds: float) -> str:
    """Return *seconds*, a whole number of them, as 'H:MM:SS', with the days in front as 'Nd ' past a day."""
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    return (f"{days}d " if days else "") + f"{hours}:{minutes:02d}:{secs:02d}"


def _slack(bound: float) -> float:
    """Return how far a typed number may lie beyond *bound*, a float32 off the bus, and still be the bound.

    The slack is a millionth of the bound's size, or of 1 where that is smaller: well above a float32's rounding
    and well below any step.
    """
    return max(abs(bound), 1.0) * 1e-6


@dataclass
class Value:
    """One value of a page, as its attributes describe it.

    It holds the value's *index*, *name* and *unit*; its *visualization* type, one of VIS_*; its *minimum*,
    *maximum*, *step* and *default*; whether the device marks it *writeable*; the *options* of a list value,
    the entries' texts by value; whether it is shown *gray* (greyed out, not in effect right now) and whether
    it is *eventable* (the device broadcasts the value); and its *long_name* and *description*.

    A device or command value has entries too (a device value's are the addresses as str() writes them, or
    'none' for an empty entry), but they follow the bus, so a table keeps none: PageReader.read_options()
    reads them when they are shown or written, and they stand in *options* only in a copy made then.
    """
    index: int
    name: str = ""
    unit: str = ""
    visualization: int = VIS_NUMBER
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    default: float | None = None
    writeable: bool = False
    options: list[str] = field(default_factory=list)
    gray: bool = False
    eventable: int = 0
    long_name: str = ""
    description: str = ""

    def parse(self, text: str) -> float:
        """Return the number *text* means for this value.

        A value with options takes only their names, compared without regard to case, and gives the option's
        index: a number, an index among them, is no name. A switch, an alarm value or an action or flag value
        of range 0..1, takes on and off (or true and false, yes and no) as 1 and 0. A date value takes a date
        written 'YYYY-MM-DD' as well as a number (see date_value()). Any other value takes a number. A number
        past a bound by less than a millionth of it gives the bound, since the bounds arrive as float32.

        Raise ValueError, which names the options where there are any, if *text* is none of those, is a number
        the bus cannot carry (see sendable()) or lies outside the range.
        """
        t = text.strip()
        if self.options:
            for n, option in enumerate(self.options):
                if fold(option) == fold(t):
                    return float(n)
            raise ValueError(f"{text!r} is not one of {', '.join(repr(o) for o in self.options)}")
        if self.visualization == VIS_DATE and t.count("-") == 2 and not t.startswith("-"):
            return date_value(t)
        low = t.lower()
        if self.is_switch and low in ("on", "off", "true", "false", "yes", "no"):
            return 1.0 if low in ("on", "true", "yes") else 0.0
        try:
            value = float(t)
            if not sendable(value):
                raise ValueError
        except ValueError:
            raise ValueError(f"{text!r} is not a number") from None
        # The bounds arrive as float32: 0.1 as 0.100000001490116, so 0.1 typed is the minimum, not below it.
        if self.minimum is not None and value < self.minimum - _slack(self.minimum):
            raise ValueError(f"{text} is below the minimum {self.format(self.minimum)}")
        if self.maximum is not None and value > self.maximum + _slack(self.maximum):
            raise ValueError(f"{text} is above the maximum {self.format(self.maximum)}")
        # Within the slack but past the bound: the bound is meant, and the bound is what is sent, not a float32
        # just past it that the device would refuse or keep.
        if self.minimum is not None and value < self.minimum:
            return self.minimum
        if self.maximum is not None and value > self.maximum:
            return self.maximum
        return value

    @property
    def is_switch(self) -> bool:
        """Tell whether the value is on or off: an alarm value, or an action or flag value of range 0..1."""
        return self.visualization == VIS_ALARM or (self.visualization in (VIS_ACTION, VIS_FLAG)
                                                   and self.minimum == 0 and self.maximum == 1)

    @property
    def is_writer_register(self) -> bool:
        """Tell whether the value is the register after a switch that takes the writer's address.

        Such a register has no name, is writeable, has no minimum, and has 65535 as its maximum, its step and
        its default; an EasyView lists one after each of its eventable switches.
        """
        no_minimum = self.minimum is None or self.minimum != self.minimum
        return (not self.name and self.writeable and no_minimum
                and self.maximum == self.step == self.default == 65535)

    @property
    def writer(self) -> str:
        """Return how a write reaches this value: 'float' or 'text'."""
        return "text" if self.visualization == VIS_TEXT else "float"

    @property
    def text_length(self) -> int:
        """Return the most characters a text value takes.

        That is its range's maximum, but no more than the 16 that MasterBus.write_string() writes, and 16
        without a maximum or with one of 0 or less.
        """
        if self.maximum is None or self.maximum <= 0:
            return MasterBus.STRING_MAX
        return min(int(self.maximum), MasterBus.STRING_MAX)

    def option_for(self, *names: str) -> int | None:
        """Return the index of the option equal to one of *names*, compared without regard to case, or None."""
        wanted = {fold(n) for n in names if n and n.strip()}
        for n, option in enumerate(self.options):
            if fold(option) in wanted:
                return n
        return None

    @property
    def default_value(self) -> float | None:
        """Return the default the device gives, or None where it gives none.

        Devices mark 'no default' with 65535 or another value outside the range, 65535 also where there is no
        range, and a list's default must name one of its entries.
        """
        d = self.default
        if d is None or not self._in_range(d) or (d == NO_DEFAULT and self.maximum is None):
            return None
        if self.visualization in LISTED and self.options and not (0 <= d < len(self.options) and d == int(d)):
            return None
        return d

    @property
    def type_name(self) -> str:
        """Return the visualization type's name, such as 'number' or 'list'."""
        return VIS_NAMES.get(self.visualization, f"type {self.visualization}")

    @property
    def flags(self) -> str:
        """Return the attributes in four characters, as ls -l shows a mode.

        The characters are the type's letter, then w for writeable, e for eventable and g for greyed out, or -
        for each that is not.
        """
        return (VIS_LETTERS.get(self.visualization, "?") + ("w" if self.writeable else "-")
                + ("e" if self.eventable else "-") + ("g" if self.gray else "-"))

    def describe(self, value: float | None, text: str | None = None) -> list[tuple[str, str]]:
        """Return (label, text) pairs describing the value.

        The pairs say what it holds (*value*, and *text* for a text value), its unit, type, long name and
        description, its options or range, its default, whether and how it may be written, and whether it is
        eventable or greyed out.
        """
        lines = [("value", self.format(value))]
        if text is not None:
            lines.append(("text", text))
        if self.unit:
            lines.append(("unit", self.unit))
        lines.append(("type", self.type_name))
        if self.long_name:
            lines.append(("long name", self.long_name))
        if self.description:
            lines.append(("description", self.description))
        if self.options:
            lines.append(("options", ", ".join(self.options)))
        elif self.minimum is not None or self.maximum is not None:
            span = f"{self.format(self.minimum)} .. {self.format(self.maximum)}"
            if self.step and self.step != 1.0:
                span += f" step {self.step:g}"
            lines.append(("range", span))
        elif self.visualization == VIS_ALARM:
            lines.append(("range", "on / off"))
        default = self.default_value
        lines.append(("default", self.format(default) if default is not None else "none"))
        if not self.writeable:
            how = "no"
        elif self.writer == "text":
            how = f"yes, takes a text of up to {self.text_length} characters"
        elif self.visualization == VIS_DEVICE:
            how = "yes, takes a device among the entries: its name, type or address; or none"
        else:
            how = "yes"
        lines.append(("writeable", how))
        if self.eventable:
            lines.append(("eventable", f"yes, {self.eventable}" if self.eventable != 1 else "yes"))
        if self.gray:
            lines.append(("greyed out", "yes"))
        return lines

    def _in_range(self, value: float) -> bool:
        """Tell whether *value* lies within the value's range and is a number, not NaN.

        Devices mark "no default" with 65535 or another value outside the value's range.
        """
        if self.minimum is not None and value < self.minimum:
            return False
        if self.maximum is not None and value > self.maximum:
            return False
        return value == value

    @property
    def _decimals(self) -> int:
        """Return the decimals a step of less than one needs, from its own digits: 2 for 0.25 or 0.05, 1 for 0.5.

        The step is a float32 off the bus, 0.1 arriving as 0.10000000149..., so six significant digits decide.
        There are never more than 6.
        """
        text = f"{self.step:.6g}"
        if "e" in text:  # 1e-05
            return min(6, -int(text.partition("e")[2]))
        return min(6, len(text.partition(".")[2]))

    def format(self, value: float | None) -> str:
        """Return *value* as text for this value.

        That is the entry's name for a list, device or command value whose entries are known, '-' for None,
        'n/a' for NaN, else the number with the step's precision. Behind the number, in parentheses, a date
        value shows its date (see date_text()), and a time value of whole seconds, 0 aside, the seconds as a
        duration (see duration_text()).
        """
        if value is None:
            return "-"
        if value != value:  # NaN: the device has no value yet
            return "n/a"
        if self.visualization == VIS_DATE and (date := date_text(value)):
            return f"{int(value)} ({date})"
        if self.visualization == VIS_TIME and 0 < value < 1e9 and value == int(value):
            return f"{int(value)} ({duration_text(value)})"
        if self.visualization in LISTED and 0 <= value < len(self.options) and value == int(value):
            return self.options[int(value)]
        if self.step and 0 < self.step < 1:
            return f"{value:.{self._decimals}f}"
        if abs(value) < 1e15 and value == int(value):  # inf is no whole number, and int() refuses it
            return f"{int(value)}"
        return f"{value:.2f}".rstrip("0").rstrip(".")


@dataclass
class Group:
    """A group of a page: its *index*, *name* and the indices of its *values*."""
    index: int
    name: str = ""
    values: list[int] = field(default_factory=list)


@dataclass
class PageTable:
    """The layout of one page as read from a device.

    It holds its *page*, its *values* by index, its *groups*, and the page's *version* number when it was
    read, None where unknown.
    """
    page: Page
    values: dict[int, Value] = field(default_factory=dict)
    groups: list[Group] = field(default_factory=list)
    version: int | None = None

    def ungrouped(self) -> list[int]:
        """Return the indices of the values no group holds, in index order."""
        grouped = {i for g in self.groups for i in g.values}
        return [i for i in self.values if i not in grouped]


def entry_address(data: bytes) -> Address | None:
    """Return the address in *data*, the 8-byte reply for an entry of a device value, or None for an empty entry.

    The high field is data[4], the low field the three bytes after it, little-endian, in 18 bits; a high
    field above 0x1F marks an empty entry.
    """
    if data[4] > 0x1F:
        return None
    return Address(data[4], int.from_bytes(data[5:8], "little") & 0x3FFFF)


class PageReader:
    """Read and write a device's pages through a MasterBus.

    Create one for the device at *address* on *bus*, waiting *reply_timeout* seconds (0.25 by default) for
    each reply and re-sending a request *retries* times (2 by default) before giving up.
    """

    def __init__(self, bus: MasterBus, address: Address, reply_timeout: float = 0.25, retries: int = 2):
        self.bus = bus
        self.address = address
        self.reply_timeout = reply_timeout
        self.retries = retries
        self.serial_sid: int | None = None  # the serial number's string id, junk where a text is expected
        self._asked_serial = False  # whether the device was asked for it
        self._attributes: dict[tuple[int, int, int, int | None], int | None] = {}  # 16-bit attributes read
        self._strings: dict[int, str] = {}  # the names, units and option texts read, by string id

    def request(self, kind: int, data: bytes, retries: int | None = None) -> bytes | None:
        """Send a page request of *kind* with *data* and return the reply's data.

        A reply repeats the request's data. Return None when the device answers 'not available'. *retries*
        defaults to the reader's own. Raise TimeoutError if no reply comes.
        """
        answers = (kind - 0x20, kind - 0x10)  # the reply, and 'not available'
        try:
            reply = self.bus.exchange(Frame(kind, self.address, data),
                                      lambda f: f.address == self.address and f.data[:len(data)] == data
                                      and f.kind in answers, timeout=self.reply_timeout,
                                      retries=self.retries if retries is None else retries)
        except TimeoutError:
            raise TimeoutError(f"no reply from {self.address} to kind {kind:#04x} {data.hex(' ')}") from None
        return reply.data if reply.kind == answers[0] else None

    # -- values

    def read_value(self, page: Page, index: int) -> float | None:
        """Return the value at *index* of *page*.

        Return None where the page has no read kind, the device answers 'not available' or the reply is short.
        Raise TimeoutError if the device does not answer.
        """
        if page.read is None:
            return None
        reply = self.request(page.read, index.to_bytes(2, "little"))
        if reply is None or len(reply) < 6:
            why = "not available" if reply is None else "short reply"
            logger.debug(f"{self.address} {page.name} #{index}: no value ({why})")
            return None
        value = struct.unpack_from("<f", reply, 2)[0]
        logger.debug(f"{self.address} {page.name} #{index} = {value!r}")
        return value

    def write_value(self, page: Page, index: int, value: float) -> float:
        """Write *value* at *index* of *page* and return what the device echoes.

        Raise ValueError if *page* has no read kind, for a number the bus cannot carry (see sendable()) or if
        the device refuses (out of range, read-only, or not logged in), TimeoutError if it does not answer.

        A write is sent once and never repeated: when its reply is lost, the device may have acted on it, and a
        button such as 'Complete' would be pressed again. The caller reads the value back instead.

        The reply to a write has the kind and index of the reply to a read, so another master polling the
        value answers alike. A refusal or an echo of exactly the value sent decides; a reply with another value,
        the device's own when it stores the value rounded, counts only when neither comes in time.
        """
        kind = page.read
        if kind is None:
            raise ValueError(f"{page.name} values cannot be written")
        if not sendable(value):
            raise ValueError(f"{value} cannot be sent: a value travels as a float32")
        data = index.to_bytes(2, "little") + struct.pack("<f", value)
        logger.info(f"{self.address} {page.name} #{index} <- {value}")
        others: list[Frame] = []  # replies for this index with another value

        def decides(f: Frame) -> bool:
            if f.address != self.address or f.data[:2] != data[:2]:
                return False
            if f.kind == kind - 0x10 or (f.kind == kind - 0x20 and f.data[:6] == data):
                return True
            if f.kind == kind - 0x20 and len(f.data) >= 6:
                others.append(f)
            return False
        try:
            reply = self.bus.exchange(Frame(kind, self.address, data), decides, timeout=self.reply_timeout, retries=0)
        except TimeoutError:
            if not others:
                raise TimeoutError(f"no reply from {self.address} to the write of {page.name} #{index}") from None
            reply = others[-1]
        if reply.kind == kind - 0x10:
            raise ValueError(f"{self.address} refused {page.name} #{index} = {value}")
        echoed = struct.unpack_from("<f", reply.data, 2)[0]
        logger.debug(f"{self.address} {page.name} #{index} -> device echoes {echoed!r}")
        return echoed

    def name_writer(self, page: Page, index: int) -> bool | None:
        """Name this computer as the writer of the value at *index* of *page*, and return how the device took it.

        The frame goes to the index after *index* and carries the bus's host address, as a master names itself
        after writing a switch; a device may act on the switch only once it has come. Return True if the device
        echoes the frame, False if it answers 'not available', None if it does not answer. Raise ValueError if
        *page* has no read kind.

        Send it only where the index after *index* is no value of the page, which would be overwritten, or is
        the register for the writer's address (see Value.is_writer_register).
        """
        kind = page.read
        if kind is None:
            raise ValueError(f"{page.name} values cannot be written")
        host = self.bus.host
        data = (index + 1).to_bytes(2, "little") + bytes([host.hi]) + host.lo.to_bytes(3, "little")
        logger.info(f"{self.address} {page.name} #{index}: naming {host} as the writer, at #{index + 1}")
        try:
            reply = self.bus.exchange(Frame(kind, self.address, data),
                                      lambda f: f.address == self.address and f.data[:2] == data[:2]
                                      and f.kind in (kind - 0x20, kind - 0x10),
                                      timeout=self.reply_timeout, retries=0)
        except TimeoutError:
            logger.debug(f"{self.address} {page.name} #{index + 1}: no answer to the writer's address")
            return None
        return reply.kind == kind - 0x20

    def read_text(self, page: Page, index: int) -> tuple[int | None, str]:
        """Return (string id, text) for the text value at *index* of *page*.

        That is the id its value holds and the string behind it, or (None, '') where the value is not an id.
        Raise TimeoutError if the device does not answer for either.
        """
        sid = self._string_id(page, index)
        if sid is None:
            return None, ""
        return sid, self.bus.read_string(self.address, sid)

    def _string_id(self, page: Page, index: int) -> int | None:
        value = self.read_value(page, index)
        if value is None or value != value or value < 0 or value != int(value):
            return None
        return int(value)

    def write_text(self, page: Page, index: int, text: str) -> str:
        """Write *text* as the string the text value at *index* of *page* names, and return the text read back.

        The text goes through MasterBus.write_string(). Raise ValueError if the value names no string,
        TimeoutError if the device does not answer.
        """
        sid = self._string_id(page, index)
        if sid is None:
            raise ValueError(f"{self.address} {page.name} #{index} names no string to write")
        logger.info(f"{self.address} {page.name} #{index} string {sid:#06x} <- {text!r}")
        self.bus.write_string(self.address, sid, text)
        return self.bus.read_string(self.address, sid)

    # -- attributes

    def attr_u16(self, kind: int, attr: int, index: int, sub: int | None = None) -> int | None:
        """Return the 16-bit attribute *attr* of value *index*, or of its option *sub*, read with request *kind*.

        Return None if the device has none. Each is read once in this reader's life: the junk check of
        read_page() and read_attributes() ask for the same name and unit. Raise TimeoutError if the device
        does not answer.
        """
        key = (kind, attr, index, sub)
        if key not in self._attributes:
            self._attributes[key] = self._read_u16(kind, attr, index, sub)
        return self._attributes[key]

    def _read_u16(self, kind: int, attr: int, index: int, sub: int | None) -> int | None:
        data = bytes([attr]) + index.to_bytes(2, "little") + (b"" if sub is None else bytes([sub]))
        reply = self.request(kind, data)
        where = f"#{index}" + ("" if sub is None else f"[{sub}]")
        if reply is None or len(reply) < 6:
            logger.debug(f"attribute {attr:#04x} {where}: none")
            return None
        value = reply[4] | reply[5] << 8
        logger.debug(f"attribute {attr:#04x} {where} = {value} ({value:#06x})")
        return value

    def attr_float(self, kind: int, attr: int, index: int) -> float | None:
        """Return the float attribute *attr* of value *index*, read with request kind *kind*.

        Return None if the device has none. Raise TimeoutError if it does not answer.
        """
        reply = self.request(kind, bytes([attr]) + index.to_bytes(2, "little"))
        if reply is None or len(reply) < 8:
            logger.debug(f"attribute {attr:#04x} #{index}: none")
            return None
        value = struct.unpack_from("<f", reply, 4)[0]
        logger.debug(f"attribute {attr:#04x} #{index} = {value!r}")
        return value

    def attr_string(self, kind: int, attr: int, index: int, sub: int | None = None) -> str:
        """Return the string attribute *attr* of value *index*, or of its option *sub*, read with request *kind*.

        Return '' if the device has none or does not answer for it.
        """
        try:
            sid = self.attr_u16(kind, attr, index, sub)
            return "" if sid is None else self._string(sid)
        except TimeoutError as e:
            logger.debug(str(e))
            return ""

    def _string(self, sid: int, retries: int | None = None) -> str:
        """Return string *sid* of the device, read once in this reader's life.

        Units and option texts repeat from value to value, and a string the attributes name does not change
        while a page is read. The texts of text values are read live, through read_text(). *retries* defaults
        to the bus's own. Raise TimeoutError if the device does not answer.
        """
        if sid not in self._strings:
            self._strings[sid] = self.bus.read_string(self.address, sid, retries=retries)
        return self._strings[sid]

    def read_attributes(self, page: Page, index: int) -> Value:
        """Return the Value at *index* of *page* with every attribute read, options included.

        Some firmware answers for options it has no text for with junk (the CHG with ids it never answers a
        read of, the APR with its serial number's id); the options stop at the first such answer (see
        read_options()). Raise TimeoutError if the device does not answer for a number attribute.
        """
        kind = page.info
        value = Value(index)
        value.name = self.attr_string(kind, Attribute.NAME, index)
        value.unit = self.attr_string(kind, Attribute.UNIT, index)
        value.visualization = self.attr_u16(kind, Attribute.VISUALIZATION, index) or VIS_NUMBER
        value.minimum = self.attr_float(kind, Attribute.MINIMUM, index)
        value.maximum = self.attr_float(kind, Attribute.MAXIMUM, index)
        value.step = self.attr_float(kind, Attribute.STEP, index)
        value.default = self.attr_float(kind, Attribute.DEFAULT, index)
        value.writeable = bool(self.attr_u16(kind, Attribute.WRITEABLE, index))
        value.gray = bool(self.attr_u16(kind, Attribute.GRAY, index))
        value.eventable = self.attr_u16(kind, Attribute.EVENTABLE, index) or 0
        value.long_name = self.attr_string(kind, Attribute.LONG_NAME, index)
        value.description = self.attr_string(kind, Attribute.DESCRIPTION, index)
        if value.visualization == VIS_LIST:  # the entries of a device or command value follow the bus: not kept
            value.options = self.read_options(page, value)
        logger.debug(f"{self.address} {page.name} value #{index}: {value.name!r} unit {value.unit!r} "
                     f"vis {value.visualization} range {value.minimum}..{value.maximum} step {value.step} "
                     f"default {value.default} flags {value.flags} eventable {value.eventable} "
                     f"long {value.long_name!r} description {value.description!r} options {value.options}")
        return value

    def serial_id(self) -> int | None:
        """Return the string id of the device's serial number, asked for once, or None where the device does not say.

        Some firmware answers for texts it has none for with this id (see read_options()).
        """
        if not self._asked_serial:
            self._asked_serial = True
            try:
                self.serial_sid = self.bus.identity_string_id(self.address, Identity.SERIAL)
            except (TimeoutError, ValueError):
                self.serial_sid = None
        return self.serial_sid

    def read_options(self, page: Page, value: Value) -> list[str]:
        """Return the entries of *value*, a list, device or command value of *page*, their texts by index.

        A device holds as many entries as the value's maximum says, from 0, and is asked for no more, as
        MasterAdjust asks. A list value's maximum is *value*'s, and without one between 1 and 255 the device is
        asked until it has none, for at most ENTRIES_MOST. A device or command value's maximum is read afresh,
        since it follows the bus as the entries do (the CHG's event targets list the current target first, then
        the other devices, and an empty entry only while no target is set, one more then), and without one
        there are no entries, as for MasterAdjust: asked past its maximum, the CHG answers with whatever lies
        beyond its table.

        The entries of a list value are the texts behind attribute 0x26, those of a command value the texts
        behind attribute 0x0F. Some firmware answers for texts it has none for with junk: the CHG (44010500)
        with ids it then never answers a read of, the APR with its serial number's id. The texts stop at the
        first such entry, whether the list is read with its page or on its own, as for a write. An empty text
        is an entry (the shunt's config lock lists '' first), but empty ones at the end are dropped. A text the
        device does not answer for ends the list too.

        The entries of a device value are the addresses behind attribute 0x20, as str() writes them, 'none'
        where the entry is empty: a high field above 0x1F, or an entry the device calls not available, which
        MasterAdjust counts as one. An answer that is no address ends them.

        The list is empty where the device lists none. Raise TimeoutError if the device does not answer.
        """
        kind = page.info
        assert kind is not None
        index = value.index
        maximum = value.maximum if value.visualization == VIS_LIST else self.attr_float(kind, Attribute.MAXIMUM, index)
        counted = maximum is not None and 0 < maximum < 256 and maximum == int(maximum)
        options: list[str] = []
        if not counted and value.visualization in LIVE:
            return options
        most = int(maximum) if counted and maximum is not None else ENTRIES_MOST
        if value.visualization == VIS_DEVICE:
            for n in range(most):
                reply = self.request(kind, bytes([Attribute.TARGETS]) + index.to_bytes(2, "little") + bytes([n]))
                if reply is None:
                    options.append("none")
                elif len(reply) < 8:
                    logger.debug(f"{self.address} {page.name} #{index} entry {n}: {reply.hex(' ')} is no address")
                    break
                else:
                    address = entry_address(reply)
                    options.append("none" if address is None else str(address))
            return options
        attr = Attribute.COMMANDS if value.visualization == VIS_COMMAND else Attribute.OPTIONS
        serial = self.serial_id()
        for n in range(most):
            sid = self.attr_u16(kind, attr, index, n)
            if sid is None or sid == serial:
                break
            try:
                options.append(self._string(sid, retries=0))
            except TimeoutError:
                break
        while options and not options[-1]:
            options.pop()
        return options

    def group_length(self, page: Page, index: int) -> int | None:
        """Return how many values the group at *index* of *page* has.

        The length is the group's own value, read as a value is but on the group kind. MasterAdjust reads it
        before the members. Return None if the device gives none, or a length that is NaN, negative or above
        256. Raise TimeoutError if the device does not answer.
        """
        reply = self.request(page.group, index.to_bytes(2, "little"))
        if reply is None or len(reply) < 6:
            return None
        length = struct.unpack_from("<f", reply, 2)[0]
        if length != length or length < 0 or length > 256:
            logger.debug(f"group #{index} answers a length of {length!r}, ignored")
            return None
        return int(length)

    def read_group(self, page: Page, index: int, n_values: int | None = None) -> Group:
        """Return the Group at *index* of *page*: its name and value indices.

        The group holds as many members as its length says or, where a device has no length to give, the
        members until 'not available', at most 256 since the member number is one byte. Some firmware answers
        beyond the end with garbage, so an index out of range (at or above *n_values*) or seen before ends the
        group too. Raise TimeoutError if the device does not answer.
        """
        group = Group(index, name=self.attr_string(page.group, Attribute.NAME, index))
        length = self.group_length(page, index)
        # The member number is one byte, so a group holds at most 256 values whatever the page's count.
        most = min(n_values, 256) if n_values is not None else 256
        for n in range(most if length is None else min(length, most)):
            member = self.attr_u16(page.group, Attribute.MEMBERS, index, n)
            if member is None or member in group.values or (n_values is not None and member >= n_values):
                logger.debug(f"group #{index} ends at member {n}: "
                             f"{'not available' if member is None else f'index {member} repeated or out of range'}")
                break
            group.values.append(member)
        else:
            if length is None:
                logger.debug(f"group #{index} fills every member number; a byte holds no more")
        logger.debug(f"{self.address} {page.name} group #{index}: {group.name!r} values {group.values}")
        return group

    def page_version(self, page: Page) -> int | None:
        """Return the version number of *page*, or None where the device has none to give.

        Raise TimeoutError when it does not answer at all: that it has no version is not known then.
        """
        try:
            return self.bus.read_number(self.address, page.version)
        except (NotAvailable, ValueError) as e:
            logger.debug(f"{self.address} {page.name}: no version number: {e}")
            return None

    def read_page(self, page: Page, progress: Progress | None = None) -> PageTable:
        """Return the PageTable of *page*: every value and group, names and attributes included.

        This is slow: it takes many round trips. *progress*, if given, is called with the values and groups
        read so far and the total. Firmware that counts more values than it has and answers junk for the
        phantoms is cut at the first two junk values in a row. A version number the device does not answer
        for is kept as None.

        A page the device answers 'not available' for the number of values or groups of, as an EasyView does
        for its alarm page, is returned empty. Raise TimeoutError if the device does not answer, ValueError if
        it gives no number of values or groups.
        """
        try:
            n_values = self.bus.read_number(self.address, page.count_values)
            n_groups = self.bus.read_number(self.address, page.count_groups)
        except NotAvailable:
            logger.info(f"{self.address} {page.name}: the device has no such page")
            return PageTable(page)
        logger.debug(f"{self.address} {page.name}: reading {n_values} values and {n_groups} groups "
                     f"(kinds info {page.info:#04x} group {page.group:#04x} read {page.read})")
        try:
            version = self.page_version(page)
        except TimeoutError:  # kept as unknown: the next look at the cache reads the page again
            version = None
        table = PageTable(page, version=version)
        total = n_values + n_groups
        # Some firmware (the APR) counts more values than it has and answers for the phantoms with
        # junk whose string ids are the serial number's: two of those in a row end the page.
        serial = self.serial_id()
        junk: list[int] = []
        for i in range(n_values):
            if serial is not None and self.attr_u16(page.info, Attribute.NAME, i) == serial \
                    and self.attr_u16(page.info, Attribute.UNIT, i) == serial:
                junk.append(i)
                if len(junk) >= 2:
                    logger.info(f"{self.address} {page.name}: the device counts {n_values} values but answers "
                                f"junk from #{junk[0]} on; keeping {junk[0]}")
                    for j in junk[:-1]:
                        table.values.pop(j, None)
                    n_values = junk[0]
                    total = n_values + n_groups  # the bar counts what is read, not the phantoms
                    break
            else:
                junk.clear()
            table.values[i] = self.read_attributes(page, i)
            if progress:
                progress(i + 1, total)
        for g in range(n_groups):
            table.groups.append(self.read_group(page, g, n_values))
            if progress:
                progress(n_values + g + 1, total)
        logger.info(f"{self.address} {page.name}: {n_values} values in {n_groups} groups")
        return table


# -- serialisation of the static part, for a cache

def table_to_dict(table: PageTable) -> dict:
    """Return *table* as a dict of plain values, for JSON."""
    return {"page": table.page.name, "version": table.version,
            "values": [vars(i) for i in table.values.values()],
            "groups": [vars(g) for g in table.groups]}


def table_from_dict(d: dict) -> PageTable:
    """Return the PageTable the dict *d*, from table_to_dict(), describes."""
    table = PageTable(PAGE_BY_NAME[d["page"]], version=d.get("version"))
    for i in d["values"]:
        value = Value(**i)
        table.values[value.index] = value
    table.groups = [Group(**g) for g in d["groups"]]
    return table
