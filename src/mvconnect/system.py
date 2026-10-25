"""The bus as devices, pages and values, addressed by words.

Things are named with plain words:

    DEVICE...                       a device: its address (hh:lllll), the words of its name, or its type (msh, chg...)
    DEVICE... PAGE                  one of its pages: monitor, alarm, history, config
    DEVICE... VALUE...              a value on any page: its index, or the words of its name
    DEVICE... VALUE... PAGE         a value of that page; the page keyword may stand anywhere after the device

The device is the longest run of leading words that selects exactly one
device (System.locate). Among the rest, a page keyword restricts the
search to that page, and the others select the value; the page keyword
itself is never a value's word. Either side is selected by mvconnect.words: a
bare selector or index wins, otherwise every word must equal the index or
start a word of the name, the long name or the group, so an index among
the words pins one value. Names repeat within a page and across pages;
the index with the page is the unambiguous handle. Groups are how the
device presents its values: a group's name selects its values, and a
listing keeps the grouping.
"""

from __future__ import annotations

import logging
import math
import queue
import time
from collections.abc import Callable, Iterable, Iterator
import dataclasses
from dataclasses import dataclass, field

from mvconnect import cache
from mvconnect.frame import Address, Frame
from mvconnect.hidlink import LinkError
from mvconnect.masterbus import Device, MasterBus, Progress, sendable
from mvconnect.pages import (LIVE, PAGES, PAGE_BY_NAME, Page, PageReader, PageTable, VIS_ACTION, VIS_COUNTER,
                             VIS_DATE, VIS_DEVICE, Value)
from mvconnect.words import fold, select

logger = logging.getLogger(__name__)

TRAFFIC_BACKLOG = 10_000  # frames held for a reader of the traffic that lags; more are dropped


class AddressError(Exception):
    """The words name no device, page or value, or more than one where one is needed."""


class Refused(ValueError):
    """A write the checks refuse before anything is sent.

    The checks refuse a value out of range, not an option, too long, or one the device marks read-only. The
    caller may send it anyway.
    """


@dataclass
class Written:
    """What a write did.

    It holds the *value* written; the value before and after, as text (*before*, *after*); what was asked for
    (*wanted*); any *warnings*; and whether the value reads back as asked for (*took*).
    """

    value: Value
    before: str
    after: str
    wanted: str
    warnings: list[str] = field(default_factory=list)
    took: bool = True


@dataclass
class Selection:
    """What a list of words names: the device, and the readings of the words after it.

    *device* is the device. *readings* are the readings of the words after it, the likeliest first, each a page
    name (None for every page) and the words left for the value ('' for the whole page or device). A page
    keyword may be the page or a word of a name, as in Alarm levels or Min/Max history, so each page keyword is
    a reading of its own, and all the words on every page the last.
    """

    device: Device
    readings: list[tuple[str | None, str]]

    @property
    def page(self) -> str | None:
        """Return the page of the likeliest reading."""
        return self.readings[0][0]

    @property
    def words(self) -> str:
        """Return the value's words in the likeliest reading."""
        return self.readings[0][1]


def check_identity(devices: list[Device]) -> list[Device]:
    """Warn of each of *devices* without a serial number or firmware version, and return *devices*.

    Every MasterBus device has both, so one missing is a fault, a reply lost on the bus or a bug, and worth
    saying whenever the devices are listed: such a device's pages are never cached, since the cache is keyed
    by both.
    """
    for d in devices:
        missing = [what for what, known in (("serial number", d.serial), ("firmware version", d.version)) if not known]
        if missing:
            logger.warning(f"{d.selector} {d.product_name}: no {' and no '.join(missing)}, which every MasterBus "
                           "device has: a reply lost on the bus, or a bug. Its pages are read anew each time")
    return devices


class System:
    """The bus as devices, pages and values, seen through one link.

    Create one on *bus*. Page layouts are cached on disk per device firmware. With *refresh* each page is read
    from its device again the first time this System needs it, and the cache is updated; with *use_cache*
    False the cache is neither read nor written. Every method that talks to the bus raises LinkError once the
    link has failed.
    """

    def __init__(self, bus: MasterBus, use_cache: bool = True, refresh: bool = False):
        self.bus = bus
        self.use_cache = use_cache
        self.refresh = refresh
        self._fresh: set[tuple[str, str, str]] = set()  # (serial, version, page) read afresh by this System
        self._devices: list[Device] | None = None

    # -- devices

    def devices(self, timeout: float = 1.5, progress: Progress | None = None, refresh: bool = False) -> list[Device]:
        """Return the devices on the bus, identified.

        Listen *timeout* seconds for them the first time and remember them after; with *refresh*, enumerate the
        bus again. *progress* is handed to MasterBus.list_devices(). A device without a serial number or
        firmware version is warned of (check_identity()).
        """
        if self._devices is None or refresh:
            self._devices = check_identity(self.bus.list_devices(timeout=timeout, progress=progress))
        return self._devices

    def device(self, words: str, timeout: float = 1.5) -> Device:
        """Return the device *words* select.

        The words are its selector, or its name, type (MSH, CHG, DIS...) or product name, exactly or by word
        starts (see mvconnect.words). *timeout* is as for devices(). Raise AddressError unless exactly one device
        is selected, with nothing left over.
        """
        sel = self.locate(words.split(), timeout)
        if sel.readings[-1][1]:  # the last reading holds every word after the device
            raise AddressError(f"no device {words!r} on the bus")
        return sel.device

    def locate(self, words: Iterable[object], timeout: float = 1.5) -> Selection:
        """Return the Selection *words* make.

        The longest run of leading words that selects exactly one device is the device; among the rest, a page
        keyword names the page and the others are the value's words; a page keyword may also be a word of a
        name, see Selection. *timeout* is as for devices(). Raise AddressError if there are no words or no run of
        leading words is one device; where the longest run that selects devices selects several, the error says
        so.
        """
        parts = [str(w) for w in words if str(w).strip()]
        if not parts:
            raise AddressError("no device given")
        several: tuple[str, list[Device]] | None = None
        for k in range(len(parts), 0, -1):
            head = " ".join(parts[:k])
            found = self._select_devices(head, timeout)
            if len(found) == 1:
                rest = parts[k:]
                pages = list(dict.fromkeys(w.lower() for w in rest if w.lower() in PAGE_BY_NAME))
                readings = [(page, " ".join(w for w in rest if w.lower() != page)) for page in pages]
                return Selection(self._one(head, found), readings + [(None, " ".join(rest))])
            if len(found) > 1 and several is None:
                several = head, found
        if several is not None:
            self._one(*several)  # raises: the longest run that named devices named too many
        raise AddressError(f"no device {' '.join(parts)!r} on the bus")

    def _select_devices(self, words: str, timeout: float) -> list[Device]:
        """Return the devices *words* select: by address when they are one, else by name, type or product name."""
        try:
            address = Address.parse(words)
        except ValueError:
            found = select(self.devices(timeout), words.split(), lambda d: d.selector, lambda d: d.name,
                           lambda d: (d.kind, d.product_name))
            logger.debug(f"{words!r} as a name, type or product name selects {len(found)} of "
                         f"{len(self.devices(timeout))} devices")
            return found
        found = [d for d in self.devices(timeout) if d.address == address]
        logger.debug(f"{words!r} is address {address}: {len(found)} such device")
        return found

    @staticmethod
    def _one(words: str, found: list[Device]) -> Device:
        """Return the one device in *found*, logging what the words resolved to; raise AddressError for several."""
        if len(found) > 1:
            for d in found:
                logger.info(f"{d.selector}  {d.kind} {d.name}  {d.product} {d.serial}")
            raise AddressError(f"{words!r} names {len(found)} devices; use the selector")
        d = found[0]
        if words != d.selector:
            logger.info(f"{words!r} is {d.selector}  {d.kind} {d.name}  {d.product} {d.serial}")
        return d

    # -- traffic

    def traffic(self) -> Iterator[tuple[float, Frame, bool]]:
        """Yield every frame on the bus as it passes, sent or received, as (time.time(), frame, sent).

        Go on until the caller stops asking. A reader that falls TRAFFIC_BACKLOG frames behind loses frames,
        with a warning. Raise LinkError when the link fails.
        """
        frames, remove, dropped = self.listen()
        try:
            while True:
                try:
                    item = frames.get(timeout=0.5)
                except queue.Empty:
                    self._check_link()
                    continue
                if dropped[0]:
                    logger.warning(f"{dropped[0]} frames dropped: the monitor fell behind the bus")
                    dropped[0] = 0
                yield item
        finally:
            remove()

    def listen(self) -> tuple[queue.Queue, Callable[[], None], list[int]]:
        """Return a queue that every frame on the bus is put into, a function that stops it, and a drop count.

        Each frame is put as (time.time(), frame, sent). The count is a one-element list of the frames dropped
        because the queue held TRAFFIC_BACKLOG already: a reader that stalls costs frames, not memory.
        """
        frames: queue.Queue = queue.Queue(maxsize=TRAFFIC_BACKLOG)
        dropped = [0]

        def put(frame: Frame, sent: bool) -> None:
            try:
                frames.put_nowait((time.time(), frame, sent))
            except queue.Full:
                dropped[0] += 1
        return frames, self.bus.tap(put), dropped

    def _check_link(self) -> None:
        """Raise LinkError if the link has failed under its reader, as an unplugged link does."""
        failure = getattr(self.bus.link, "failure", None)
        if failure:
            raise LinkError(failure)

    # -- login

    def login_level(self, device: Device) -> int:
        """Return the level *device* is logged in at, 0 when logged out.

        Raise TimeoutError if it does not say, ValueError if its answer is too short to say.
        """
        return self.bus.login_level(device.address)

    def login(self, device: Device, level: int, password: float = 0.0) -> tuple[bool, int | None]:
        """Log *device* in at *level* with *password*, 0 to log out, and read the level back.

        Return whether the device acknowledged the login, and the level it reports after it, None if it does
        not say. Raise ValueError, sending nothing, for a level that is none of LOGIN_LEVELS or a password the
        bus cannot carry.
        """
        try:
            self.bus.login(device.address, level, password)
            acknowledged = True
        except TimeoutError:
            acknowledged = False
        try:
            return acknowledged, self.bus.login_level(device.address)
        except (TimeoutError, ValueError):
            return acknowledged, None

    # -- pages

    def tables(self, device: Device, pages: list[Page] | None = None, progress: Progress | None = None,
               afresh: Iterable[str] | None = None) -> dict[str, PageTable]:
        """Return the page tables of *device* for *pages*, all four by default, by page name.

        A table comes from the cache where it has it and its version number still matches the device's; else
        it is read from the device, with *progress* called as values and groups come in, and saved to the cache
        beside the pages it holds. The pages named in *afresh* are read from the device whatever the cache
        holds; by default, with *refresh*, those this System has not read afresh yet.
        """
        pages = pages or list(PAGES)
        afresh = set(self.to_refresh(device, pages) if afresh is None else afresh)
        # The cache is keyed by serial number and firmware version: a device that did not say both shares no
        # file with another that did not either.
        use_cache = self.use_cache and bool(device.serial and device.version)
        if self.use_cache and not use_cache:
            logger.debug(f"{device.address}: no serial number or version, so no cache")
        cached = cache.load_tables(device.serial, device.version) if use_cache else None
        reader = PageReader(self.bus, device.address)
        for name in afresh & set(cached or ()):
            del cached[name]  # type: ignore[union-attr]
        if cached:
            # A page whose version number moved on has changed, its writeable flags for one: read it again.
            for page in pages:
                table = cached.get(page.name)
                if table is None:
                    continue
                try:
                    now = reader.page_version(page)
                except TimeoutError:  # whether the page changed is not known: it is read again
                    logger.info(f"{device.address} {page.name}: no answer for its version, reading it again")
                    del cached[page.name]
                    continue
                if now is not None and now != table.version:
                    logger.info(f"{device.address} {page.name}: version {table.version} -> {now}, reading it again")
                    del cached[page.name]
        if cached and all(p.name in cached for p in pages):
            logger.debug(f"{device.address}: pages {[p.name for p in pages]} from the cache")
            return {p.name: cached[p.name] for p in pages}
        logger.debug(f"{device.address}: reading pages {[p.name for p in pages if not cached or p.name not in cached]} "
                     f"from the device" + ("" if self.use_cache else " (cache off)") + (" (refreshing)" if afresh else ""))
        tables = dict(cached or {})
        totals: dict[str, tuple[int, int]] = {}

        def sub_progress(name):
            def report(n, total):
                totals[name] = (n, total)
                if progress:
                    progress(sum(v[0] for v in totals.values()), sum(v[1] for v in totals.values()))
            return report

        for page in pages:
            if page.name not in tables:
                tables[page.name] = reader.read_page(page, sub_progress(page.name))
        if use_cache:  # what was read, merged beside the cached pages that were not asked for
            cache.save_tables(device.serial, device.version, tables)
        return {p.name: tables[p.name] for p in pages}

    def to_refresh(self, device: Device, pages: list[Page]) -> list[str]:
        """Return the names of the *pages* to read from *device* afresh.

        With *refresh*, these are the pages this System has not read afresh yet, which it then counts as read;
        without, there are none.
        """
        if not self.refresh:
            return []
        keys = [(device.serial, device.version, p.name) for p in pages]
        names = [key[2] for key in keys if key not in self._fresh]
        self._fresh.update(keys)
        return names

    def read_texts(self, device: Device, table: PageTable, indices: list[int]) -> dict[int, str]:
        """Return the texts behind the text values among *indices* of *table*'s page, by index.

        A value the device does not answer for is left out, with a warning.
        """
        texts = {}
        for i in indices:
            if table.values[i].writer == "text":
                try:
                    texts[i] = self.read_text(device, table, i)[1]
                except TimeoutError:
                    logger.warning(f"{device.address} {table.page.name} #{i}: no answer for its text")
        return texts

    def read_values(self, device: Device, table: PageTable, indices: list[int]) -> dict[int, float | None]:
        """Return the values at *indices* of *table*'s page, by index.

        A value the device does not answer for is None, with a warning.
        """
        reader = PageReader(self.bus, device.address)
        values = {}
        for i in indices:
            try:
                values[i] = reader.read_value(table.page, i)
            except TimeoutError:
                logger.warning(f"{device.address} {table.page.name} #{i}: no answer")
                values[i] = None
        return values

    # -- single values, through the same seam, so that the server can serve them

    def read_options(self, device: Device, table: PageTable, value: Value) -> list[str]:
        """Return the entries of *value*, a list, device or command value of *table*'s page, as the device lists them now.

        The entries are read from the device, not taken from the table; see PageReader.read_options().
        """
        return PageReader(self.bus, device.address).read_options(table.page, value)

    def read_entries(self, device: Device, table: PageTable, indices: list[int]) -> dict[int, list[str]]:
        """Return the entries of the device and command values among *indices* of *table*'s page, by index.

        Their entries follow the bus, so the table keeps none (see Value); a listing reads them here, as it
        reads the texts of text values. A value the device does not answer for is left out, with a warning.
        """
        entries = {}
        for i in indices:
            if table.values[i].visualization in LIVE:
                try:
                    entries[i] = self.read_options(device, table, table.values[i])
                except TimeoutError:
                    logger.warning(f"{device.address} {table.page.name} #{i}: no answer for its entries")
        return entries

    def read_text(self, device: Device, table: PageTable, index: int) -> tuple[int | None, str]:
        """Return (string id, text) for the text value at *index* of *table*'s page; see PageReader.read_text()."""
        return PageReader(self.bus, device.address).read_text(table.page, index)

    def write_value(self, device: Device, table: PageTable, index: int, value: float) -> float:
        """Write *value* at *index* of *table*'s page and return what the device echoes.

        See PageReader.write_value().
        """
        return PageReader(self.bus, device.address).write_value(table.page, index, value)

    def write_text(self, device: Device, table: PageTable, index: int, text: str) -> str:
        """Write *text* as the string the text value at *index* of *table*'s page names.

        Return the text read back; see PageReader.write_text().
        """
        return PageReader(self.bus, device.address).write_text(table.page, index, text)

    # -- writing a value given as text

    def write(self, device: Device, table: PageTable, value: Value, text: str, force: bool = False,
              timeout: float = 1.5) -> Written:
        """Write *text* to *value* of *table*'s page on *device*, read it back and return a Written.

        The text is a number, an option name or on/off; for a device value ('Event 1 target') a device on the
        bus by its name, type or address, which must be among the value's entries, or none; for a command value
        ('Event 1 command') an entry's name; for a text value ('Device name') the text itself. The entries of a
        device or command value are read from the device first, since they follow the bus. The text is checked
        against the value's range, entries or length and the device's writeable flag, and a check that fails
        raises Refused, which *force* skips: a number is then sent as typed; a text with a character outside
        Latin-1, which a device's text cannot hold, raises ValueError. A device value takes nothing but a device
        among its entries, or none, *force* or not: a text that names no device on the bus, or more than one,
        or a device that is not among the entries, raises AddressError; *timeout* is for finding the device.
        none chooses the empty entry or, where there is none, the value just past the entries, which is how a
        CHG clears an event target (on a device value that holds a plain index, the read-back then says the
        write did not take). The device checks again and may refuse (ValueError); a device that does not
        acknowledge is read back anyway, with a warning. A text value whose text cannot be read raises
        TimeoutError: before the write, before anything is sent, or after a write it does not acknowledge.
        """
        label = self._label(device, table, value)
        if not value.writeable and not force:
            raise Refused(f"{label}: the device marks this value read-only")
        if value.visualization in LIVE:
            value = dataclasses.replace(value, options=self.read_options(device, table, value))
        if value.writer == "text":
            if len(text) > value.text_length and not force:
                raise Refused(f"{label}: {text!r} is longer than {value.text_length} characters")
            try:
                text.encode("latin-1")  # what a device's text holds, as MasterBus.write_string() sends it
            except UnicodeEncodeError as e:
                raise ValueError(f"{label}: {text[e.start:e.end]!r} is no character a device's text holds") from None
            wanted: float | str = text
        elif value.visualization == VIS_DEVICE:
            try:
                wanted = self._device_entry(value, text, timeout)
            except AddressError as e:
                raise AddressError(f"{label}: {e}") from None
        else:
            try:
                wanted = value.parse(text)
            except ValueError as e:
                if not force:
                    raise Refused(f"{label}: {e}") from None
                try:  # forced past the range and the list, but not past what the bus can carry
                    wanted = float(text)
                except ValueError:
                    wanted = math.nan
                if not sendable(wanted):
                    raise ValueError(f"{label}: {text!r} is no number the bus can carry") from None
        return self._send(device, table, value, wanted)

    def write_default(self, device: Device, table: PageTable, value: Value, force: bool = False) -> Written:
        """Reset *value* of *table*'s page on *device* to the default the device gives.

        Write the default, read it back and return a Written. Raise ValueError where there is no default to
        return to (none given, or a text, device or command value) and for an action value, whose default would
        carry the action out; raise Refused where the device marks the value read-only, or for a counter or a
        date, which *force* skips.
        """
        label = self._label(device, table, value)
        default = value.default_value
        if value.writer != "float" or value.visualization in LIVE or default is None:
            raise ValueError(f"{label}: no default to reset to")
        if value.visualization == VIS_ACTION:
            raise ValueError(f"{label}: an action has no state to reset; writing its default would carry it out")
        if value.visualization in (VIS_COUNTER, VIS_DATE) and not force:
            raise Refused(f"{label}: a {value.type_name} is not reset to a default")
        if not value.writeable and not force:
            raise Refused(f"{label}: the device marks this value read-only")
        return self._send(device, table, value, default)

    @staticmethod
    def _label(device: Device, table: PageTable, value: Value) -> str:
        return f"{device.selector} / {table.page.name} / [{value.index}] {value.name}"

    def _device_entry(self, value: Value, text: str, timeout: float) -> float:
        """Return the index of the entry of *value*, a device value, that *text* chooses.

        *text* is an entry as the list shows it (an address, or none for an empty entry), or a device on the
        bus by its name, type or address, whose address must be among the entries; *timeout* is for finding
        it. *value*'s options are the entries as read just now. Where no entry is empty, none is the value just
        past the entries: the CHG lists an empty entry only while no target is set, and clears the target on
        that value. Raise AddressError where the text names no device on the bus, or more than one, or a device
        that is not among the entries.
        """
        choice = value.option_for(text)
        if choice is not None:
            return float(choice)
        if fold(text) == "none":
            return float(len(value.options))
        try:
            target = self.device(text, timeout)
        except AddressError as e:
            if not value.options:
                raise AddressError(f"{e}; and the value has no entries to choose from") from None
            raise AddressError(f"{e}; the entries are: {', '.join(value.options)}") from None
        choice = value.option_for(str(target.address))
        if choice is None:
            raise AddressError(f"{target.product_name} at {target.address} is not among the entries: "
                               f"{', '.join(value.options) or 'none at all'}")
        return float(choice)

    def _send(self, device: Device, table: PageTable, value: Value, wanted: float | str) -> Written:
        """Make the write itself, after the checks, and the read-back; return a Written.

        *wanted* is the number or text to write. A write the device does not acknowledge is read back anyway,
        with a warning. A text value whose text cannot be read raises TimeoutError.
        """
        label = self._label(device, table, value)
        warnings: list[str] = []
        if value.writer == "text":
            assert isinstance(wanted, str)
            _, text_before = self.read_text(device, table, value.index)
            try:
                text_after = self.write_text(device, table, value.index, wanted)
            except TimeoutError:
                warnings.append(f"{label}: the device did not acknowledge the write; reading it back")
                _, text_after = self.read_text(device, table, value.index)
            took = text_after == wanted[:MasterBus.STRING_MAX]
            if not took:
                warnings.append(f"{label}: wanted {wanted!r}, reads back {text_after!r}")
            return Written(value, text_before, text_after, wanted, warnings, took)
        assert isinstance(wanted, float)
        if value.visualization == VIS_DEVICE:
            return self._send_device(device, table, value, wanted)
        before = self.read_values(device, table, [value.index])[value.index]
        try:
            echoed = self.write_value(device, table, value.index, wanted)
        except TimeoutError:
            warnings.append(f"{label}: the device did not acknowledge the write; reading it back")
            echoed = None
        after = self.read_values(device, table, [value.index])[value.index]
        took = after is not None and abs(after - wanted) <= (value.step or 1e-6) / 2
        if not took:
            warnings.append(f"{label}: wanted {value.format(wanted)}, device echoed {value.format(echoed)}, "
                            f"reads back {value.format(after)}")
        return Written(value, value.format(before), value.format(after), value.format(wanted), warnings, took)

    def _send_device(self, device: Device, table: PageTable, value: Value, wanted: float) -> Written:
        """Write entry *wanted* of the device value *value* and read back which device it holds now.

        A device may hold a device value otherwise than as the index written: the CHG keeps the current
        target as entry 0 of its event targets with the value at 0, and rearranges the entries on a write.
        So the value and the entries are read back together, and the write took when the entry the value
        points at is the device chosen. *value*'s options are the entries as read just before.
        """
        label = self._label(device, table, value)
        warnings: list[str] = []
        chosen = value.format(wanted) if wanted < len(value.options) else "none"  # past the entries clears
        before = value.format(self.read_values(device, table, [value.index])[value.index])
        try:
            self.write_value(device, table, value.index, wanted)
        except TimeoutError:
            warnings.append(f"{label}: the device did not acknowledge the write; reading it back")
        held = self.read_values(device, table, [value.index])[value.index]
        live = dataclasses.replace(value, options=self.read_options(device, table, value))
        after = live.format(held)
        took = after == chosen
        if not took:
            warnings.append(f"{label}: wanted {chosen}, the device holds {after}; its entries are now "
                            f"{', '.join(live.options) or 'none at all'}")
        return Written(value, before, after, chosen, warnings, took)

    # -- lookups

    @staticmethod
    def find_values(table: PageTable, words: str) -> list[Value]:
        """Return the values of *table* that *words* select, in table order (see mvconnect.words).

        A lone index selects the value it names; else each value is selected for which each word equals its
        index or starts a word of its name, long name or group. No words select every value.
        """
        groups = {i: [g.name for g in table.groups if i in g.values] for i in table.values}
        return select(table.values.values(), words.split(), lambda i: str(i.index), lambda i: i.name,
                      lambda i: (i.long_name, *groups[i.index]))

    @staticmethod
    def sections(table: PageTable, values: list[Value]) -> list[tuple[str, list[int]]]:
        """Return (group name, indices) pairs laying *values* out as the device groups them.

        The groups come in table order with the values each has among them, then the rest under 'other'; a page
        without groups is one unnamed section.
        """
        wanted = {i.index for i in values}
        out = [(g.name, [i for i in g.values if i in wanted]) for g in table.groups]
        out = [(name, indices) for name, indices in out if indices]
        rest = [i for i in table.ungrouped() if i in wanted]
        if rest:
            out.append(("other" if out else "", rest))
        return out

    def find(self, device: Device, words: str, page: str | None = None,
             progress: Progress | None = None) -> list[tuple[PageTable, Value]]:
        """Return (table, value) for every value of *device* that *words* select, in page and table order.

        Search *page*, or every page without one. The page tables are read as for tables(), with *progress*.
        Raise AddressError for a *page* that is none of PAGES, whose names are taken without regard to case.
        """
        if page and page.lower() not in PAGE_BY_NAME:
            raise AddressError(f"no page {page!r}; the pages are {', '.join(p.name for p in PAGES)}")
        pages = [PAGE_BY_NAME[page.lower()]] if page else list(PAGES)
        tables = self.tables(device, pages, progress)
        return [(tables[p.name], v) for p in pages for v in self.find_values(tables[p.name], words)]

    def find_selection(self, sel: Selection, progress: Progress | None = None
                       ) -> tuple[str | None, str, list[tuple[PageTable, Value]]]:
        """Return the reading of *sel* that selects something, as its page, its words and what find() gives.

        That is the likeliest reading that selects any value, or one that names a page alone, which stands even
        when the page is empty; the likeliest reading with nothing found when none does. *progress* is as for
        tables().
        """
        for page, words in sel.readings:
            found = self.find(sel.device, words, page, progress)
            if found or not words:
                return page, words, found
        return sel.page, sel.words, []

    def resolve_value(self, device: Device, words: str, page: str | None = None) -> tuple[PageTable, Value]:
        """Return (table, value) for the one value of *device* that *words* select, for writing.

        Search *page*, or any page without one. Raise AddressError if the words are empty or select no value
        or several.
        """
        if not words.split():
            raise AddressError("no value named")
        found = self.find(device, words, page)
        if not found:
            raise AddressError(f"no value {words!r} on {page or 'any page'} of {device.selector}")
        if len(found) > 1:
            which = ", ".join(f"{t.page.name} [{v.index}] {v.name}" for t, v in found)
            raise AddressError(f"{words!r} selects {len(found)} values; use the index"
                               + ("" if page else " with the page") + f": {which}")
        return found[0]
