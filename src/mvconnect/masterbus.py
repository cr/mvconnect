"""The MasterBus request/reply protocol, as far as it is understood.

Every device has an address (frame.Address). A request is a frame of
KIND_REQUEST addressed to the device, data[0] being the command; the
device answers with a frame of KIND_REPLY carrying its own address and
the same command byte. A broadcast KIND_ENUMERATE with no data makes
every device send a KIND_ANNOUNCE with its address and four info bytes.

A reply of kind 0x0d instead of 0x0c means "not available".

Requests by their first data byte, the Command:

    0x08 kind                -> 0x08 kind lo hi      (Command.NUMBER)
        one of the device's 16-bit numbers; see Number. Number.VERSION
        answers the firmware version as major.minor bytes.
    0x08 kind lo hi [f32]    -> reply                (Command.NUMBER)
        writes one; mvconnect writes only the login (Number.LOGIN).
    0x09 which               -> 0x09 which lo hi     (Command.IDENTITY)
        the id of one of the device's identity strings; see Identity.
    0x30 id_lo id_hi chunk   -> 0x30 id_lo id_hi chunk c c c c   (Command.STRING)
        4 characters of string `id`, from character 4*chunk; the string
        ends at a NUL or at a chunk shorter than 4 characters. The table
        is laid out differently on every device type: fetch the ids of
        the identity strings with 0x09, the rest are units and the names
        of the device's values, groups and alarms.
"""

from __future__ import annotations

import enum
import logging
import math
import signal
import struct
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from mvconnect.frame import KIND_ANNOUNCE, KIND_ENUMERATE, KIND_NA, KIND_REPLY, KIND_REQUEST, Address, Frame
from mvconnect.hidlink import FrameQueue, HidLink, LinkInfo

logger = logging.getLogger(__name__)

# This computer's address on the bus: the high field 0x14, which MasterAdjust's frames carry too, and a low
# field of 0x1234.
HOST_ADDRESS = Address(0x14, 0x1234)
LINK_PRODUCT = "77030100"  # the product number of the MasterBus USB Link
_YEAR_LETTERS = {"O": 10, "N": 11, "D": 12}  # the letters that stand for a year in a serial number


def host_address(serial: str, product: str = LINK_PRODUCT) -> Address:
    """Return the address this computer takes on the bus through the link with the *serial* number.

    The high field is 0x14. The low field is computed from the serial number, nine characters of the form
    'LYWWLNNNN', and the link's *product* number the way a device computes its own, so that two links get two
    addresses. Return HOST_ADDRESS if *serial* is not of that form.
    """
    # As another MasterBus implementation documents it; it reproduces the address of every device on my bus
    # from its serial and product numbers.
    if len(serial) != 9 or not (serial[2:4].isdigit() and serial[5:9].isdigit() and product.isdigit()):
        return HOST_ADDRESS
    year = _YEAR_LETTERS.get(serial[1].upper(), int(serial[1]) if serial[1].isdigit() else None)
    if year is None:
        return HOST_ADDRESS
    week, number = int(serial[2:4]), int(serial[5:9])
    groups = sum(int(product[max(0, i - 3):i]) for i in range(len(product), 0, -3))  # 3-digit groups, from the right
    return Address(HOST_ADDRESS.hi, (number + (week + 32 * (year & 3)) * 2048 + 64 * groups) & 0x3FFFF)


class Command(enum.IntEnum):
    """What a request (KIND_REQUEST) asks for: its first data byte."""
    NUMBER = 0x08  # one of the device's 16-bit numbers, of a kind of Number; with a value behind it, a write
    IDENTITY = 0x09  # the id of one of the device's identity strings, of a kind of Identity
    STRING = 0x30  # four characters of a string, by its id; with characters behind them, a write


class Identity(enum.IntEnum):
    """The device's identity strings, which Command.IDENTITY gives the ids of."""
    PRODUCT = 1  # the product number, such as 77020100
    SERIAL = 2
    PRODUCT_NAME = 3  # "<type> <name>", e.g. "MSH House Bank"
    REVISION = 4  # not every device has one


class Number(enum.IntEnum):
    """The kinds of the device's 16-bit numbers that Command.NUMBER reads, those used here.

    A page's own version number, which changes when its layout or attributes change (the writeable flags after
    a lock, for one), is a kind too: 0x1c monitor, 0x1d history, 0x1e config and 0x1f alarm; see pages.PAGES.
    So are the numbers of a page's values and groups.
    """
    VERSION = 0x0B  # firmware version, major in the high byte, minor in the low byte
    LOGIN = 0x19  # the login level, 0 when logged out; written to log in (see MasterBus.login)
    STATUS_VERSION = 0x60  # changes whenever something about the device changed; MasterAdjust polls it

FLOAT32_MAX = 3.4028234663852886e38  # values, and a login's password, travel as float32


def sendable(number: float) -> bool:
    """Return whether *number* can travel on the bus as a float32: finite, and within its range."""
    return math.isfinite(number) and abs(number) <= FLOAT32_MAX


BOOTLOADER_BIT = 0x20  # in the third info byte of an announcement: the device is in flash mode


def version_text(v: int) -> str:
    """Return the version word *v* as MasterAdjust shows it: major.minor, without a zero prefix."""
    return f"{(v >> 8) & 0xFF}.{v & 0xFF}"


# The login levels as MasterAdjust names them; 0 is logged out. Each level has its password, which the
# user gives.
LOGIN_LEVELS = {"installer": 1, "distributor": 2, "service": 3}


def login_level_name(level: int | None) -> str:
    """Return the login level *level* as Mastervolt names it.

    The name is one of LOGIN_LEVELS for a level that has one, 'logged out' for 0, 'level N' for a level it has
    no name for, and '?' for None.
    """
    if level is None:
        return "?"
    if level == 0:
        return "logged out"
    return next((name for name, n in LOGIN_LEVELS.items() if n == level), f"level {level}")

# Called with the number of steps done and the total, for a progress bar.
Progress = Callable[[int, int], None]


# What ends a process from outside: Ctrl-C, kill, and a closed terminal or ssh connection (no SIGHUP on Windows).
ENDING = tuple(getattr(signal, name) for name in ("SIGINT", "SIGTERM", "SIGHUP") if hasattr(signal, name))


@contextmanager
def uninterrupted() -> Iterator[None]:
    """Hold off the signals that end a process until the block is done, then deliver them.

    The signals are those of ENDING: SIGINT for Ctrl-C, SIGTERM and SIGHUP. Holding them off means a write of
    several frames is never cut off between two of them. Each signal that arrived is delivered once, when the
    block ends, after the previous handlers are back in place. Only the main thread receives signals; elsewhere
    the block runs as it is.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    held: list[int] = []
    previous = {s: signal.signal(s, lambda signum, _frame: held.append(signum)) for s in ENDING}
    try:
        yield
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler if handler is not None else signal.SIG_DFL)
        if held:  # logged here, not in the handler, where writing to a stream is not safe
            causes = ", ".join(_signal_name(s) for s in dict.fromkeys(held))
            _log_ending(f"{causes} came during a write of several frames: the write was finished first, "
                        "ending now")
        for s in dict.fromkeys(held):
            signal.raise_signal(s)


def _signal_name(signum: int) -> str:
    """Return how a log line names the signal *signum*: Ctrl-C for SIGINT, else its name, such as SIGTERM."""
    return "Ctrl-C" if signum == signal.SIGINT else signal.Signals(signum).name


class Ended(SystemExit):
    """The process ends on the signal *signum*, SIGTERM or SIGHUP, as an exit, so that its cleanups run.

    Its status is 128 plus *signum*. A SystemExit with that status that is not an Ended, a remote command's
    relayed status among them, says nothing about a signal here.
    """

    def __init__(self, signum: int):
        super().__init__(128 + signum)
        self.signum = signum


def ending_cause(exc: BaseException | None) -> str | None:
    """Return what ends the process, as a log line names it, or None where *exc* is no interrupt.

    That is 'interrupted by Ctrl-C' for a KeyboardInterrupt, and 'ended by SIGTERM' and so on for an Ended. An
    error, an ordinary exit or no exception at all is None.
    """
    if isinstance(exc, KeyboardInterrupt):
        return "interrupted by Ctrl-C"
    if isinstance(exc, Ended):
        return f"ended by {_signal_name(exc.signum)}"
    return None


_announced = False  # whether this process has logged what ends it: the first such line is a warning


def winding_down(exc: BaseException | None, step: str, log: logging.Logger | None = None) -> None:
    """Log that *step* is wound down because *exc* ends the process, where it is an interrupt.

    The line names the cause first (see ending_cause()), then the step: 'interrupted by Ctrl-C: closing the
    link to the bus'. The first such line of the process is a warning, since it is the one that says a signal
    came; the steps after it are at info level. It goes to *log*, the logger of the module that winds down,
    this module's by default. Nothing is logged for an error or an ordinary end, which have their own lines.
    """
    cause = ending_cause(exc)
    if cause:
        _log_ending(f"{cause}: {step}", log)


def _log_ending(message: str, log: logging.Logger | None = None) -> None:
    """Log *message* about what ends the process to *log*: a warning the first time, at info level after."""
    global _announced
    (log or logger).log(logging.INFO if _announced else logging.WARNING, message)
    _announced = True


class NotAvailable(TimeoutError):
    """The device answers 'not available' (kind 0x0d): it has no such thing to give.

    It is a TimeoutError still, as for a caller one is as good as the other, unless it has to tell an answer
    from silence.
    """


@dataclass
class Device:
    """A device on the bus.

    It holds its *address*; the four *info* bytes of its announcement, of which only the flash-mode bit is
    understood (see flash_mode); its
    *product* number such as 77020100; its *serial* number; its *product_name* as the device reports it,
    '<type> <name>'; its hardware *revision*; and its firmware *version*, 'major.minor'. Everything but the
    address and the info bytes is filled in by MasterBus.identify().
    """
    address: Address
    info: bytes = b""
    product: str = ""
    serial: str = ""
    product_name: str = ""
    revision: str = ""
    version: str = ""

    @property
    def selector(self) -> str:
        """Return the device's selector, the name no other device shares: its address as hh:lllll."""
        return str(self.address)

    @property
    def kind(self) -> str:
        """Return the device type in front of the product name, such as MSH, CHG or DIS."""
        head, _, _ = self.product_name.partition(" ")
        return head

    @property
    def name(self) -> str:
        """Return the name given to the device: the product name without its type."""
        _, _, tail = self.product_name.partition(" ")
        return tail

    @property
    def flash_mode(self) -> bool:
        """Return whether the device announced itself in flash mode, its bootloader running."""
        return len(self.info) > 2 and bool(self.info[2] & BOOTLOADER_BIT)


@dataclass
class AnnounceInfo:
    """The data of a KIND_ANNOUNCE frame: the device's *address* and its four *info* bytes."""

    address: Address
    info: bytes

    @classmethod
    def parse(cls, data: bytes) -> AnnounceInfo:
        """Return the AnnounceInfo in *data*, the data of an announcement.

        Raise ValueError if *data* is not 8 bytes.
        """
        if len(data) != 8:
            raise ValueError(f"announcement needs 8 data bytes: {data.hex()}")
        return cls(Address(data[0] & 0x1F, int.from_bytes(data[1:4], "little") & 0x3FFFF), bytes(data[4:8]))

    @property
    def flash_mode(self) -> bool:
        """Return whether the device announces itself in flash mode, its bootloader running."""
        return bool(self.info[2] & BOOTLOADER_BIT)


class MasterBus:
    """A MasterBus reached through one link.

    Create one on *link*, a HidLink (the first link found, by default), with *host* as this computer's
    address (by default the one host_address() computes from the link's serial number when it is opened, or
    HOST_ADDRESS), waiting *reply_timeout* seconds for each reply (0.3 by default) and re-sending a request
    *retries* times (2 by default) before giving up. Every method that sends raises LinkError once the link
    has failed.
    """

    def __init__(self, link: HidLink | None = None, host: Address | None = None, reply_timeout: float = 0.3,
                 retries: int = 2):
        self.link = link or HidLink()
        self._host = host
        self.reply_timeout = reply_timeout
        self.retries = retries
        self._queue = FrameQueue()
        self._listening = 0  # conversations waiting for frames: only while one is are frames queued
        self._taps: list[Callable[[Frame, bool], None]] = []

    @property
    def host(self) -> Address:
        """Return this computer's address on the bus: the one given, or else the one open() found."""
        return self._host or HOST_ADDRESS

    def open(self) -> MasterBus:
        """Open the link and return the bus.

        Without a host address given, the bus takes the one host_address() computes from the link's serial
        number, where the link says one.
        """
        self.link.open(self._receive)
        info = getattr(self.link, "info", None)  # a HidLink's, once open; a stand-in for a link may have none
        if self._host is None and isinstance(info, LinkInfo) and info.serial:
            self._host = host_address(info.serial)
            logger.debug(f"this computer's address is {self._host}, from the link's serial number")
        return self

    def _receive(self, frame: Frame) -> None:
        if self._listening:
            self._queue(frame)
        self._tell(frame, False)

    @contextmanager
    def _conversation(self) -> Iterator[None]:
        """Queue the frames that arrive while the block runs, and only those.

        A frame from before it, another master's poll answered an hour ago for one, is never taken for a
        reply, and an idle bus queues nothing.
        """
        self._listening += 1
        self._queue.drain()
        try:
            yield
        finally:
            self._listening -= 1

    def send(self, *frames: Frame) -> None:
        """Send *frames* on the link.

        Every frame for the bus goes through here, so the taps see it. Raise LinkError if the link cannot send
        them; see HidLink.send().
        """
        for frame in frames:
            self._tell(frame, True)
        self.link.send(*frames)

    def tap(self, listener: Callable[[Frame, bool], None]) -> Callable[[], None]:
        """Have *listener*(frame, sent) called for every frame this bus sends or receives.

        A received frame is passed from the link's reader thread. An exception the listener raises is logged
        and goes no further. Return a function that removes the tap.
        """
        self._taps.append(listener)
        return lambda: self._taps.remove(listener)

    def _tell(self, frame: Frame, sent: bool) -> None:
        for listener in list(self._taps):
            try:
                listener(frame, sent)
            except Exception:
                logger.exception("a bus tap failed")

    def close(self) -> None:
        """Close the link."""
        self.link.close()

    def reopen(self) -> MasterBus:
        """Close the link and open it again, as after it was unplugged; return the bus.

        Raise LinkError while the link cannot be opened. The address open() found is kept.
        """
        self.link.close()
        return self.open()

    def __enter__(self) -> MasterBus:
        return self.open()

    def __exit__(self, *exc) -> None:
        winding_down(exc[1], "closing the link to the bus")
        self.close()

    # -- discovery

    def enumerate(self, timeout: float = 1.5, settle: float = 1.0) -> list[Device]:
        """Ask every device to announce itself and return those that did.

        Listen *timeout* seconds (1.5 by default) for announcements, and longer while devices keep appearing:
        until *settle* seconds (1.0 by default) have passed since the last new device announced itself, so
        that a slow one is not the last heard. Return the devices in address order, each a Device with its
        address and info bytes only. An announcement that does not parse is logged and left out.
        """
        logger.info("enumerating the bus")
        found: dict[Address, Device] = {}
        deadline = time.monotonic() + timeout
        with self._conversation():
            self.send(Frame(KIND_ENUMERATE, self.host))
            while time.monotonic() < deadline:  # moved on by every new device, so until() is entered again
                for frame in self._queue.until(deadline):
                    device = self._announced(frame)
                    if device is not None and device.address not in found:
                        found[device.address] = device
                        deadline = max(deadline, time.monotonic() + settle)
        logger.info(f"{len(found)} devices announced themselves")
        reports = getattr(self.link, "reports_in", None)  # a HidLink counts them; another link may not
        if not found and reports is not None:
            if reports == 0:
                logger.debug("the link sent nothing at all: is it plugged into a powered MasterBus network, "
                             "and is this the right HID interface?")
            else:
                logger.debug(f"the link sent {reports} reports with {self.link.frames_in} frames, none an "
                             "announcement: is the MasterBus network powered, and the link's bus cable connected?")
        return [found[a] for a in sorted(found)]

    @staticmethod
    def _announced(frame: Frame) -> Device | None:
        """Return the Device *frame* announces, or None for any other frame or one that does not parse."""
        if frame.kind != KIND_ANNOUNCE:
            return None
        try:
            ann = AnnounceInfo.parse(frame.data)
        except ValueError as e:
            logger.warning(f"ignoring announcement from {frame.address}: {e}")
            return None
        if ann.address != frame.address:
            logger.warning(f"announcement from {frame.address} names {ann.address}")
        logger.debug(f"device {ann.address} info {ann.info.hex(' ')}")
        return Device(ann.address, info=ann.info)

    # -- requests

    def request(self, address: Address, data: bytes, echo: int = 2, retries: int | None = None) -> bytes:
        """Send a request to the device at *address* and return the data of its reply.

        *data* is the request's data, the command byte first. A reply is a KIND_REPLY frame from the device
        whose first *echo* data bytes (2 by default) repeat the request's (the command byte and its selector).
        Raise TimeoutError if no reply comes within reply_timeout after *retries* re-sends (the bus's own count
        by default), and NotAvailable, a TimeoutError, if the device answers 'not available'.
        """
        frame = Frame(KIND_REQUEST, address, data)
        try:
            reply = self.exchange(frame, lambda f: f.address == address and f.data[:echo] == data[:echo]
                                  and f.kind in (KIND_REPLY, KIND_NA), retries=retries)
        except TimeoutError:
            raise TimeoutError(f"no reply from {address} to request {frame.data_text()}") from None
        if reply.kind == KIND_NA:
            raise NotAvailable(f"{address} answers 'not available' to request {frame.data_text()}")
        return reply.data

    def exchange(self, frame: Frame, match: Callable[[Frame], bool], *, timeout: float | None = None,
                 retries: int | None = None) -> Frame:
        """Send *frame* and return the first reply that *match* accepts.

        The reply is looked for among the frames that arrive within *timeout* seconds after the frame is sent,
        and the frame is sent again up to *retries* times; a late reply to an earlier attempt counts.
        *timeout* and *retries* default to the bus's.

        Every conversation on the bus goes through here: request() and the page requests match a reply by
        its address, echo and kind, and the flasher matches the bootloader's ACK (kind 0x1d) and NAK (kind
        0x2d) to its 0x3d transfer frames. Raise TimeoutError if nothing is accepted.
        """
        to = self.reply_timeout if timeout is None else timeout
        rt = self.retries if retries is None else retries
        with self._conversation():
            for attempt in range(rt + 1):
                logger.debug(f"exchange {frame}, {to * 1000:.0f} ms, attempt {attempt + 1}")
                self.send(frame)
                started = time.monotonic()  # once sent: the link may hold a frame back until it has a slot free
                others = 0
                for reply in self._queue.until(started + to):
                    if match(reply):
                        logger.debug(f"accepted {reply} after {(time.monotonic() - started) * 1000:.0f} ms, "
                                     f"{others} other frames")
                        return reply
                    others += 1
                logger.debug(f"no accepted reply to {frame} in {to * 1000:.0f} ms, {others} other frames, "
                             f"attempt {attempt + 1}")
        raise TimeoutError(f"no reply to {frame}")

    def read_string(self, address: Address, string_id: int, encoding: str = "latin-1",
                    retries: int | None = None) -> str:
        """Return string *string_id* of the device at *address*.

        The string is read four characters per round trip, in at most 64 round trips, up to its first NUL or a
        chunk shorter than four characters, and decoded with *encoding* (latin-1 by default), a character it cannot
        decode replaced. *retries* is passed to request(). Raise TimeoutError, or NotAvailable, as request()
        does.
        """
        chars = b""
        for chunk in range(64):
            req = bytes([Command.STRING, string_id & 0xFF, string_id >> 8, chunk])
            reply = self.request(address, req, echo=4, retries=retries)
            part = reply[4:]
            chars += part
            if len(part) < 4 or b"\0" in part:
                break
        text = chars.split(b"\0")[0].decode(encoding, "replace")
        logger.debug(f"string {string_id:#06x} of {address}: {text!r} in {chunk + 1} chunks")
        return text

    STRING_MAX = 16  # MasterAdjust cuts a string to 16 characters before writing it

    def write_string(self, address: Address, string_id: int, text: str, encoding: str = "latin-1",
                     retries: int = 0) -> None:
        """Write *text* as string *string_id* of the device at *address*, as MasterAdjust does.

        The request is the same as a read, with the characters behind the chunk number, four per round trip and
        a NUL at the end. The text is encoded with *encoding* (latin-1 by default), a character it cannot
        encode replaced, and cut to its first STRING_MAX bytes, as many characters in latin-1; the NUL is added
        unless the cut text ends in one. An interrupt waits until the last chunk is written.

        Like every write, each chunk is sent once (*retries* 0): a chunk whose answer is lost raises TimeoutError,
        and reading the string back tells what the device holds. A chunk the device answers 'not available' to
        raises NotAvailable.
        """
        chars = text.encode(encoding, "replace")[: self.STRING_MAX]
        if not chars.endswith(b"\0"):
            chars += b"\0"
        with uninterrupted():  # half a text is a text nobody wrote
            for chunk in range((len(chars) + 3) // 4):
                part = chars[4 * chunk:4 * chunk + 4]
                req = bytes([Command.STRING, string_id & 0xFF, string_id >> 8, chunk]) + part
                logger.debug(f"string {string_id:#06x} of {address} chunk {chunk} <- {part!r}")
                self.request(address, req, echo=4, retries=retries)
        logger.debug(f"string {string_id:#06x} of {address} written: {text!r}")

    def read_number(self, address: Address, kind: int) -> int:
        """Return the 16-bit number of *kind* of the device at *address*.

        *kind* is a Number, a page's version, or the number of a page's values or groups. Raise ValueError on
        a short reply, and TimeoutError, or NotAvailable, as request() does.
        """
        reply = self.request(address, bytes([Command.NUMBER, kind]))
        if len(reply) < 4:
            raise ValueError(f"short number reply from {address}: {reply.hex(' ')}")
        number = reply[2] | reply[3] << 8
        logger.debug(f"number {kind:#04x} of {address}: {number}")
        return number

    def login(self, address: Address, level: int, password: float = 0.0) -> None:
        """Log the device at *address* in at *level* with *password*, or out with level 0.

        The login is Number.LOGIN written with the level and the password as a float behind it; a password of
        0, the default, is left out (DLC 4), as MasterAdjust does. It is sent once, and its answer must repeat
        the level. Raise ValueError for a password that cannot travel as a float32 (see sendable()) or a level
        that is neither 0 nor one of LOGIN_LEVELS, TimeoutError if the device does not answer or refuses.
        """
        if not sendable(password):
            raise ValueError(f"a password of {password} cannot be sent: it travels as a float32")
        if level != 0 and level not in LOGIN_LEVELS.values():
            raise ValueError(f"login level {level} is none of 0 (logged out), "
                             + ", ".join(f"{n} {name}" for name, n in LOGIN_LEVELS.items()))
        req = bytes([Command.NUMBER, Number.LOGIN]) + level.to_bytes(2, "little")
        if password:
            req += struct.pack("<f", password)
        logger.debug(f"login {address} at level {level}")
        self.request(address, req, echo=4, retries=0)  # another master's poll of the level is no answer

    def login_level(self, address: Address) -> int:
        """Return the level the device at *address* is logged in at, 0 when logged out."""
        return self.read_number(address, Number.LOGIN)

    def identity_string_id(self, address: Address, which: int) -> int:
        """Return the string id behind one of the device's identity strings, *which* being an Identity.

        Raise ValueError on a short reply, and TimeoutError, or NotAvailable, as request() does.
        """
        reply = self.request(address, bytes([Command.IDENTITY, which]))
        if len(reply) < 4:
            raise ValueError(f"short identity reply from {address}: {reply.hex(' ')}")
        logger.debug(f"identity {which:#04x} of {address} is string {reply[2] | reply[3] << 8:#06x}")
        return reply[2] | reply[3] << 8

    def read_identity(self, address: Address, which: int) -> str:
        """Return one of the device's identity strings, *which* being an Identity, looked up through its id.

        Raise ValueError and TimeoutError as identity_string_id() and read_string() do.
        """
        return self.read_string(address, self.identity_string_id(address, which))

    # -- identification

    IDENTITY = {"product": Identity.PRODUCT, "serial": Identity.SERIAL, "product_name": Identity.PRODUCT_NAME,
                "revision": Identity.REVISION}

    def identify(self, device: Device) -> Device:
        """Read the identity of *device* into it and return it.

        The identity is its product number, serial number, product name, revision and firmware version. A value
        the device does not answer for, or answers for too briefly, is left empty, with a warning, except the
        revision, which not every device has.
        """
        for attr, which in self.IDENTITY.items():
            try:
                setattr(device, attr, self.read_identity(device.address, which))
            except (TimeoutError, ValueError) as e:
                if attr != "revision":
                    logger.warning(f"device {device.address} does not answer for its {attr}: {e}")
        try:
            device.version = version_text(self.read_number(device.address, Number.VERSION))
        except (TimeoutError, ValueError) as e:
            logger.warning(f"device {device.address} does not answer for its version: {e}")
        logger.debug(f"device {device.address}: {device.product_name!r} {device.product} {device.serial} "
                     f"v{device.version}")
        return device

    def list_devices(self, timeout: float = 1.5, progress: Progress | None = None) -> list[Device]:
        """Return every device on the bus, identified.

        The devices are those that announce themselves within *timeout* seconds (see enumerate()), each read
        with identify(). *progress*, if given, is called with the number of devices identified so far and the
        total.
        """
        devices = self.enumerate(timeout)
        if progress:
            progress(0, len(devices))
        for i, device in enumerate(devices):
            self.identify(device)
            if progress:
                progress(i + 1, len(devices))
        return devices

