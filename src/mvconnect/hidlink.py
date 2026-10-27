"""The MasterBus USB Link as a USB HID device.

The link is a composite USB device with one HID interface (usage page 1,
usage 0x3A), 64-byte input and output reports and no report ids. Every
report carries up to four CAN records, see frame.py. Reports from the
link also tell how many transmit slots it has free; the host must not
send more records than that.
"""

from __future__ import annotations

import logging
import os
import queue
import re
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import hid

from mvconnect.frame import MAX_RECORDS, PAYLOAD_SIZE, RECORD_SIZE, Frame, pack_report, unpack_report

logger = logging.getLogger(__name__)

VENDOR_ID = 0x1A64  # Mastervolt International B.V.
PRODUCT_ID = 0x0000  # MasterBus USB Link

REPORT_ID = 0  # the interface has no report ids; HID APIs still want the byte in front

USAGE_PAGE = 0x01
USAGE = 0x3A

# libusb names an interface by its place on the bus: bus-port[.port...]:config.interface
LIBUSB_PATH = re.compile(r"^(\d+-[\d.]+):\d+\.(\d+)$")
SYSFS_USB = "/sys/bus/usb/devices"

SILENCE = 2.0  # seconds without a report from the link before saying so


@dataclass(frozen=True)
class LinkInfo:
    """One MasterBus USB Link as enumerated.

    It holds the link's HID *path*, its *serial* number, and its *product* and *manufacturer* strings; a string
    hidapi does not give is empty.
    """
    path: bytes
    serial: str
    product: str
    manufacturer: str


def _describe(d: dict) -> str:
    """Return the HID device *d*, as hidapi enumerates it, as text for the log."""
    path = d.get("path", b"")
    path = path.decode(errors="replace") if isinstance(path, bytes) else str(path)
    return (f"{d.get('vendor_id', 0):04x}:{d.get('product_id', 0):04x} interface {d.get('interface_number', -1)} "
            f"usage {d.get('usage_page', 0):#06x}/{d.get('usage', 0):#04x} bus {d.get('bus_type', '?')} "
            f"{d.get('manufacturer_string') or '-'!r} {d.get('product_string') or '-'!r} "
            f"serial {d.get('serial_number') or '-'!r} release {d.get('release_number', 0):#06x} at {path}")


def find_links() -> list[LinkInfo]:
    """Return a LinkInfo for every MasterBus USB Link plugged in.

    What else was seen, and whether this user may open each link, is logged at debug level.
    """
    version = getattr(hid, "version_str", lambda: "unknown")()
    everything = hid.enumerate()
    ours = [d for d in everything if d.get("vendor_id") == VENDOR_ID]
    logger.debug(f"hidapi {version} on {sys.platform}: {len(everything)} HID devices, "
                 f"{len(ours)} with vendor id {VENDOR_ID:04x}")
    for d in ours:
        logger.debug(f"  {_describe(d)}")
    links = []
    for d in ours:
        if d.get("product_id") != PRODUCT_ID:
            logger.debug(f"  skipping product id {d.get('product_id', 0):04x}, not a MasterBus USB Link")
            continue
        usage = d.get("usage_page"), d.get("usage")
        if usage not in ((USAGE_PAGE, USAGE), (0, 0), (None, None)):
            # Another interface of the link, if it ever grows one; Linux lists each.
            logger.debug(f"  skipping interface {d.get('interface_number')} with usage {usage}")
            continue
        links.append(LinkInfo(path=d["path"], serial=d.get("serial_number") or "",
                              product=d.get("product_string") or "", manufacturer=d.get("manufacturer_string") or ""))
    if not links and not ours:
        logger.debug("no device of Mastervolt's vendor id is visible; is the link plugged in, and does "
                     "`lsusb` (Linux) or System Information (macOS) show it?")
    for link in links:
        logger.debug(f"link {link.serial or '-'} at {os.fsdecode(link.path)}: {_access(link.path)}")
        if LIBUSB_PATH.match(os.fsdecode(link.path)) and not link.product:
            logger.debug("hidapi uses libusb here, and could not read the device's strings: that is what "
                         "it looks like when this user may not open the USB device")
    return links


def find_link() -> LinkInfo:
    """Return the MasterBus USB Link to use.

    That is the only one plugged in, or the first one found, with a warning naming the others. Raise LinkError
    if there is none.
    """
    links = find_links()
    if not links:
        raise LinkError("no MasterBus USB Link found (USB VID 1a64, PID 0000)")
    if len(links) > 1:
        logger.warning(f"{len(links)} MasterBus USB Links plugged in; using {_name(links[0])}, not "
                       + ", ".join(_name(link) for link in links[1:]))
    return links[0]


def _name(link: LinkInfo) -> str:
    """Return *link* as a log line names it: its serial number, where it says one, and its path."""
    return f"{link.serial} at {os.fsdecode(link.path)}" if link.serial else f"at {os.fsdecode(link.path)}"


def describe_link(link: LinkInfo) -> str:
    """Return *link* as `mvconnect link` shows it.

    The text holds its product, serial number and manufacturer as it names them, and its path. A link whose
    strings this user cannot read is said to be one, as libusb leaves them empty then.
    """
    known = " ".join(part for part in (link.product or "MasterBus USB Link",
                                       f"serial {link.serial}" if link.serial else "",
                                       f"({link.manufacturer})" if link.manufacturer else "") if part)
    unreadable = ("" if link.product or link.serial
                  else "; its strings are unreadable, as when this user may not open it")
    return f"{known} at {os.fsdecode(link.path)}{unreadable}"


def _usb_node(path: str) -> str | None:
    """Return the /dev/bus/usb node behind the libusb interface *path*, found through sysfs.

    Return None if *path* is not a libusb interface path or sysfs does not tell.
    """
    m = LIBUSB_PATH.match(path)
    if not m:
        return None
    device = os.path.join(SYSFS_USB, m.group(1))
    try:
        with open(os.path.join(device, "busnum")) as f:
            bus = int(f.read())
        with open(os.path.join(device, "devnum")) as f:
            dev = int(f.read())
    except (OSError, ValueError):
        return None
    return f"/dev/bus/usb/{bus:03d}/{dev:03d}"


def _kernel_driver(path: str) -> str:
    """Return the name of the kernel driver that holds the libusb interface *path*, from sysfs, or 'none'."""
    try:
        return os.path.basename(os.readlink(os.path.join(SYSFS_USB, path, "driver")))
    except OSError:
        return "none"


def _access(path: bytes) -> str:
    """Return what this process may do with the device node at *path*, as far as the file system tells.

    A libusb interface path is followed to its /dev/bus/usb node. The text is for the log.
    """
    try:
        name = os.fsdecode(path)
    except ValueError:
        return "not a file system path, cannot tell permissions"
    about = ""
    node = _usb_node(name)
    if node is not None:
        about = f"libusb interface path, device node {node}, kernel driver {_kernel_driver(name)}; "
        name = node
    elif LIBUSB_PATH.match(name):
        return "libusb interface path, but its device is not in sysfs, cannot tell permissions"
    elif not os.path.isabs(name):
        return "not a file system path, cannot tell permissions"
    try:
        st = os.stat(name)
    except OSError as e:
        return f"{about}cannot stat {name}: {e.strerror}"
    kind = "character device" if stat.S_ISCHR(st.st_mode) else "not a character device"
    mode = stat.filemode(st.st_mode)
    readable = os.access(name, os.R_OK)
    writable = os.access(name, os.W_OK)
    return (f"{about}{kind}, {mode} uid {st.st_uid} gid {st.st_gid}; this process "
            f"(uid {getattr(os, 'getuid', lambda: '?')()}) "
            f"may {'read' if readable else 'NOT read'} and {'write' if writable else 'NOT write'} it")


class LinkError(Exception):
    """The link cannot be found, opened, written to or read."""


class HidLink:
    """One open MasterBus USB Link.

    Create one for *info*, or for the first link found when open() is called without one. A reader thread
    turns every input report into frames and hands them to the listener given to open(); send() respects the
    link's transmit credits.
    """

    def __init__(self, info: LinkInfo | None = None):
        self.info = info
        self._found = info is None  # open() found the link itself, and finds it again when opened again
        self._dev: hid.device | None = None
        self._listener: Callable[[Frame], None] | None = None
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self._credits = MAX_RECORDS
        self._credits_changed = threading.Condition()
        self.failure: str | None = None  # why the reader stopped, when the link failed under it
        self.reports_in = 0
        self.frames_in = 0
        self.frames_out = 0

    # -- lifecycle

    def open(self, listener: Callable[[Frame], None]) -> HidLink:
        """Open the link, start the reader thread and return the link.

        The reader thread hands every frame received to *listener*. Without a LinkInfo, the first link found
        is opened, with a warning when there are several, and found again when a closed link is opened again:
        plugged in again, it may be at another path. Raise LinkError if no link is found or it cannot be
        opened.
        """
        if self._found or self.info is None:
            self.info = find_link()
        info = self.info
        dev = hid.device()
        logger.debug(f"opening {info.path!r} ({info.product!r} serial {info.serial!r})")
        try:
            dev.open_path(info.path)
        except (OSError, ValueError) as e:
            logger.debug(f"open failed: {e}; {_access(info.path)}")  # who may open it: the details, for --debug
            raise LinkError(f"cannot open the MasterBus USB Link {_name(info)}: {e}") from e
        dev.set_nonblocking(0)
        self._dev = dev
        try:
            logger.debug(f"opened: manufacturer {dev.get_manufacturer_string()!r} product "
                         f"{dev.get_product_string()!r} serial {dev.get_serial_number_string()!r}")
            descriptor = bytes(dev.get_report_descriptor())
            logger.debug(f"report descriptor, {len(descriptor)} bytes: {descriptor.hex(' ')}")
        except (OSError, ValueError, AttributeError) as e:
            logger.debug(f"device strings or report descriptor not readable: {e}")
        self._listener = listener
        self.failure = None
        with self._credits_changed:  # a link opened again has every slot free, whatever the last one spent
            self._credits = MAX_RECORDS
        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop, name="mvconnect-hid-reader", daemon=True)
        self._reader.start()
        logger.info(f"opened the MasterBus USB Link {_name(info)}")
        return self

    def close(self) -> None:
        """Stop the reader thread and close the device.

        Closing a closed link does nothing.
        """
        self._stop.set()
        if self._reader is not None:
            self._reader.join(timeout=1.0)
            self._reader = None
        if self._dev is not None:
            self._dev.close()
            self._dev = None
            logger.debug(f"closed link: {self.reports_in} reports, {self.frames_in} frames in, "
                         f"{self.frames_out} frames out")

    def __enter__(self) -> HidLink:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- sending

    def send(self, *frames: Frame, timeout: float = 1.0) -> None:
        """Send *frames* in reports of up to four, waiting for transmit credits.

        A report carries no more frames than the link has credits for. The wait for credits for all the
        reports together lasts up to *timeout* seconds (1.0 by default). Raise LinkError if the link is not
        open or has failed, the credits do not come or the write fails.
        """
        if self._dev is None:
            raise LinkError("link is not open")
        if self.failure:  # unplugged, say: no reply could ever come, so say so instead of timing out
            raise LinkError(f"{self.failure}; the link must be opened again")
        pending = list(frames)
        deadline = time.monotonic() + timeout
        while pending:
            with self._credits_changed:
                while self._credits < 1:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise LinkError("link has no transmit credits")
                    self._credits_changed.wait(remaining)
                batch, pending = pending[:min(MAX_RECORDS, self._credits)], pending[min(MAX_RECORDS, self._credits):]
                self._credits -= len(batch)
            payload = pack_report(batch)
            for f in batch:
                logger.debug(f"-> {f}")
            report = bytes([REPORT_ID]) + payload
            written = self._dev.write(report)
            logger.debug(f"-> report {len(report)} bytes, {len(batch)} records, credits left {self._credits}, "
                         f"write returned {written}: {_report_text(payload, batch)}")
            if written < 0:
                error = ""
                try:
                    error = self._dev.error()
                except (OSError, ValueError, AttributeError):
                    pass
                raise LinkError(f"write to MasterBus USB Link failed{': ' + error if error else ''}")
            self.frames_out += len(batch)

    # -- receiving

    def _read_loop(self) -> None:
        assert self._dev is not None
        logger.debug("reader started")
        last = time.monotonic()
        quiet = False
        while not self._stop.is_set():
            try:
                data = self._dev.read(PAYLOAD_SIZE, timeout_ms=100)
            except (OSError, ValueError) as e:
                if not self._stop.is_set():
                    self.failure = f"read from MasterBus USB Link failed: {e}"
                    logger.error(self.failure)
                logger.debug("reader stopped on error")
                return
            if not data:
                if not quiet and time.monotonic() - last > SILENCE:
                    quiet = True
                    logger.debug(f"no report from the link for {SILENCE:.0f} s")
                continue
            last = time.monotonic()
            if quiet:
                quiet = False
                logger.debug("the link is talking again")
            raw = bytes(data)
            try:
                report = unpack_report(raw)
            except ValueError as e:  # not shown in hex: its slots may hold anything, an old login among them
                logger.warning(f"bad report from link, {len(raw)} bytes, count byte {raw[:1].hex()}: {e}")
                continue
            logger.debug(f"<- report {len(raw)} bytes: {_report_text(raw, report.frames)}")
            self.reports_in += 1
            self.frames_in += len(report.frames)
            with self._credits_changed:
                self._credits = report.credits
                self._credits_changed.notify_all()
            logger.debug(f"   {len(report.frames)} records, credits {report.credits}, flags {report.flags:#04x}")
            for frame in report.frames:
                logger.debug(f"<- {frame}")
                if self._listener is not None:
                    self._listener(frame)
        logger.debug("reader stopped")


def _report_text(payload: bytes, frames: list[Frame]) -> str:
    """Return the part of a report's 64 *payload* bytes in use, in hex, for the log.

    That is the record count, the records it counts (*frames*), and the link's credits, flags and timestamps.
    The slots beyond the count hold stale data, an earlier login's password among them, and are not shown. A
    report holding a login is not shown at all: its frames are logged masked instead.
    """
    if any(f.secret for f in frames):
        return "(holds a login; the frames are shown masked)"
    return f"{payload[:1 + RECORD_SIZE * len(frames)].hex(' ')} .. {payload[58:PAYLOAD_SIZE].hex(' ')}"


class FrameQueue:
    """A listener that queues the frames it is given for a consumer, with timeouts."""

    def __init__(self):
        self._q: queue.Queue[Frame] = queue.Queue()

    def __call__(self, frame: Frame) -> None:
        self._q.put(frame)

    def get(self, timeout: float) -> Frame | None:
        """Return the next frame, waiting up to *timeout* seconds, or None if none arrives."""
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self) -> None:
        """Discard every queued frame."""
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                return

    def until(self, deadline: float) -> Iterator[Frame]:
        """Yield the frames arriving until *deadline*, a time.monotonic() value, and then those queued by then.

        A frame that came in time is not lost to a consumer that looks late.
        """
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                for _ in range(self._q.qsize()):
                    try:
                        yield self._q.get_nowait()
                    except queue.Empty:
                        return
                return
            frame = self.get(remaining)
            if frame is not None:
                yield frame
