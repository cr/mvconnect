"""The mvconnect command line.

The work is done by the library, the package's other modules, which logs
through `logging` and reports progress through a callback. On a terminal, rich draws the log lines and
a progress bar; elsewhere the same run prints plain lines. All of that
goes to stderr; results go to stdout.

Things are named with words, see mvconnect.system and mvconnect.words:

    mvconnect data                             the devices
    mvconnect data house bank                  every value of a device
    mvconnect data 1b:0cad9 config             one page
    mvconnect data house bank config timers    the Timers group
    mvconnect data house bank config 21        one value by index
    mvconnect data house bank config date      every value of that name
    mvconnect data house bank config 32 --set 7   write one value
"""

from __future__ import annotations

import atexit
import dataclasses
import logging
import math
import os
import re
import signal
import sys
from contextlib import contextmanager
from typing import NoReturn

import fireaid as fire

from mvconnect import decode
from mvconnect.frame import Address
from mvconnect.hidlink import LinkError, describe_link, find_link
from mvconnect.masterbus import (
    ENDING,
    HOST_ADDRESS,
    LOGIN_LEVELS,
    Ended,
    Progress,
    login_level_name,
    sendable,
    winding_down,
)
from mvconnect.options import occurrences, typed, unknown_flags
from mvconnect.pages import PageTable
from mvconnect.state import open_system
from mvconnect.system import AddressError, Refused, Selection, System

logger = logging.getLogger("mvconnect")

_console = None  # the rich stderr console of the running command, once logging is set up


def setup_logging(debug: bool = False) -> None:
    """Send log records to stderr, in the style that suits it."""
    global _console
    from rich.console import Console

    _console = Console(stderr=True)
    handler: logging.Handler
    if _console.is_terminal:
        from rich.highlighter import NullHighlighter
        from rich.logging import RichHandler
        handler = RichHandler(console=_console, show_path=debug, omit_repeated_times=False, markup=False,
                              highlighter=NullHighlighter(), rich_tracebacks=True, log_time_format="%H:%M:%S")
        handler.setFormatter(logging.Formatter("%(message)s"))
    else:
        handler = logging.StreamHandler()
        names = " %(name)s" if debug else ""
        handler.setFormatter(logging.Formatter(f"%(asctime)s %(levelname)s{names} %(message)s", "%Y-%m-%d %H:%M:%S"))
    handler.addFilter(_CleanRecords())
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO, handlers=[handler], force=True)


EXPERIMENTAL = "mvconnect is experimental software that can damage your devices. Use at your own risk."


def start(routine, debug: bool = False) -> None:
    """Begin the command *routine*: set up logging, warn, then check its flags before it does anything.

    Logging is at debug level with *debug*. The warning, EXPERIMENTAL, is the first line of every command.
    Every flag on the command line must be one *routine* takes; otherwise the command fails, logging the
    unknown flags and exiting with status 1.
    """
    setup_logging(debug)
    logger.warning(EXPERIMENTAL)
    unknown = unknown_flags(routine)
    if unknown:
        _fail(f"{' '.join(unknown)}: no such option of {routine.__name__}; nothing was done (a value that starts "
              "with - goes as --option=VALUE; --help lists the options)")


def explained(error: Exception) -> str:
    """Return *error* as text.

    Where it is a LinkError and a server on this computer holds the link, the text names the way out: run
    the command through the server, or stop it.
    """
    from mvconnect import state
    if not isinstance(error, LinkError) or os.environ.get(state.STATE_VARIABLE) or not server_here():
        return str(error)
    return (f"{error}; an mvconnect server on this computer holds the link: run commands through it "
            "(--remote localhost), or stop it")


def server_here() -> bool:
    """Tell whether an mvconnect server on this computer answers at its state's socket."""
    import socket

    from mvconnect import state
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            sock.connect(str(state.socket_path()))
        return True
    except (OSError, AttributeError):  # none there, or no Unix sockets here
        return False


def seconds(timeout) -> float:
    """Return --timeout, *timeout*, as a number of seconds.

    The command fails, exiting with status 1, for one that is not a finite number above 0 and up to an hour
    (3600): nan would wait for ever, inf and 1e300 overflow the clock, and 0 or less hears nothing.
    """
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        value = math.nan
    if not (math.isfinite(value) and 0 < value <= 3600):
        _fail(f"--timeout takes seconds, more than 0 and up to an hour, not {timeout!r}")
    return value


def log_progress(unit: str, steps: int = 8) -> Progress:
    """Return a progress callback that logs a line each time another 1/*steps* of the total is done.

    *unit* names what is counted, as in "12 / 40 values".
    """
    last_step = -1

    def report(done: int, total: int) -> None:
        nonlocal last_step
        step = steps if done >= total else done * steps // max(total, 1)
        if step != last_step:
            last_step = step
            logger.info(f"{done} / {total} {unit}")

    return report


@contextmanager
def show_progress(description: str, unit: str):
    """Yield the progress callback to hand to the work: a bar on a terminal, log lines otherwise.

    The bar is labelled *description* and counts in *unit*; it disappears when the context ends.
    """
    if _console is None or not _console.is_terminal:
        yield log_progress(unit)
        return

    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        TaskProgressColumn,
        TextColumn,
        TimeRemainingColumn,
    )

    columns = (TextColumn("{task.description}"), BarColumn(), TaskProgressColumn(), MofNCompleteColumn(),
               TextColumn(unit), TimeRemainingColumn())
    with Progress(*columns, console=_console, transient=True) as bar:
        task = bar.add_task(description, total=None)
        yield lambda done, total: bar.update(task, completed=done, total=total)


def _fail(message: str) -> NoReturn:
    logger.error(message)
    sys.exit(1)


def _table(columns, header: bool = True):
    from rich.table import Table
    table = Table(box=None, pad_edge=False, header_style="bold", show_header=header)
    for c in columns:
        table.add_column(c)
    return table


def _stdout_console():
    """Return the console for results, on stdout: coloured on a terminal, plain and unwrapped in a pipe or a file."""
    from rich.console import Console
    con = Console(highlight=False)
    return con if con.is_terminal else Console(highlight=False, width=4000)


def _print(renderable) -> None:
    _stdout_console().print(renderable)


CONTROLS = re.compile("[\x00-\x08\x0a-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")


def _clean(text: object) -> str:
    """Return *text* with control characters shown as \\xNN.

    A text from a device then cannot move the cursor, recolour the terminal or forge a log line.
    """
    return CONTROLS.sub(lambda m: f"\\x{ord(m.group()):02x}", str(text))


class _CleanRecords(logging.Filter):
    """A log filter that shows control characters in log messages as \\xNN (see _clean).

    Line breaks are included, so that every record is one line.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Clean the message of *record* in place and return True: every record passes.

        The message is formatted with its arguments first; a format and arguments that do not fit are shown
        as they are.
        """
        try:
            message = record.getMessage()
        except (TypeError, ValueError):  # a format and its arguments that do not fit: shown as they are
            message = f"{record.msg} {record.args}"
        record.msg, record.args = _clean(message), None
        return True


def _esc(text: object) -> str:
    """Return *text* with rich's markup characters escaped and control characters shown as \\xNN.

    A text from a device is then never read as a style or a terminal command.
    """
    from rich.markup import escape
    return "\n".join(escape(_clean(line)) for line in str(text).split("\n"))


def _path(*parts: str) -> str:
    """Return a coloured 'selector / page / group' heading of *parts*, device-provided strings.

    Empty parts are left out.
    """
    styles = ("bold cyan", "bold green", "bold yellow")
    return " / ".join(f"[{styles[min(i, 2)]}]{_esc(p)}[/]" for i, p in enumerate(parts) if p)


def _value(index: int, name: str, width: int = 0) -> str:
    return f"[dim]\\[{index:>{width}}][/] {_esc(name or '(unnamed)')}"


# The attribute column, coloured as eza colours a file's mode: the kind in the first place,
# then one colour per permission-like flag, and dim dashes where a flag is off.
_TYPE_STYLES = {"n": "", "f": "yellow", "l": "blue", "a": "red", "b": "magenta", "t": "cyan", "c": "dim",
                "d": "cyan", "D": "bold green", "C": "green", "?": "dim"}
_FLAG_STYLES = {"w": "bold red", "e": "green", "g": "yellow"}


def _flags(value) -> str:
    """Return Value.flags of *value* with eza-style colours: type letter, w, e, g."""
    kind, rest = value.flags[0], value.flags[1:]
    style = _TYPE_STYLES.get(kind, "")
    out = f"[{style}]{kind}[/]" if style else kind
    for c in rest:
        out += "[dim]-[/]" if c == "-" else f"[{_FLAG_STYLES[c]}]{c}[/]"
    return out


class _UI:
    """The presentation helpers, handed to subcommands."""
    setup_logging = staticmethod(setup_logging)
    start = staticmethod(start)
    seconds = staticmethod(seconds)
    explained = staticmethod(explained)
    server_here = staticmethod(server_here)
    show_progress = staticmethod(show_progress)
    table = staticmethod(_table)
    print = staticmethod(_print)
    esc = staticmethod(_esc)
    fail = staticmethod(_fail)


class Tool:
    """Talk to a Mastervolt MasterBus system through the MasterBus USB Link.

    Devices hold four pages: monitor, alarm, history, config. Each page
    holds values, which the device presents in groups; `data` reads and
    writes them. Log lines and progress go to stderr, results to stdout.

    With --remote [PASSWORD@]HOST[:PORT] anywhere on the command line, a
    command runs on the computer the Link is plugged into, where
    `mvconnect server` is running; see `mvconnect help server`. The server
    keeps the bus's state, so commands run through it answer at once; run
    it here and use --remote localhost for that on this computer. Whoever
    reaches the server may write to every device on the bus, so it warns
    when it starts open to other computers without a password (--password
    or MVCONNECT_SERVER_PASSWORD); --bind 127.0.0.1 keeps it to this one,
    and the connection is never encrypted. The firmware commands are never served: an
    empty --remote= runs a command here where MVCONNECT_REMOTE is set.
    Through the server a command does not hold the link, so its --debug
    shows no frames; `mvconnect server --debug` logs them.
    """

    def __init__(self):
        from mvconnect.firmware_cli import Firmware_
        self.firmware = Firmware_(_UI)

    def data(self, *words: str, set: str | None = None, reset: bool = False, force: bool = False, timeout: float = 1.5,
             cache: bool = True, debug: bool = False):
        """Read the data on the bus, or write one value.

        Words name what to show: nothing for the devices; a device (its
        selector hh:lllll, its name, or its type such as msh when there is
        one of that type); then, in any order, a page (monitor, alarm,
        history, config) to look at that page only, and a value: its index,
        its name, the name of its group, or words that each start a word of
        those (`batt cap` for Battery capacity), an index among them
        pinning the value. A page keyword that selects nothing as a page
        counts as a word of a name, so `alarm levels` finds the Alarm
        levels group on the config page. One value is shown with its range or options,
        its default and whether the device marks it writeable; several are
        listed as the device groups them.

            mvconnect data                             the devices and their selectors
            mvconnect data house bank                  every value of a device, as the device groups them
            mvconnect data msh config                  one page of the MasterShunt, by type
            mvconnect data msh config timers           the Timers group
            mvconnect data msh battery                 every value called Battery..., on every page
            mvconnect data msh battery low config      those called Battery low on the config page
            mvconnect data msh battery low 22          the one of them at index 22
            mvconnect data msh config 21               one value in detail
            mvconnect data msh config 21 --set 7       write it and read it back
            mvconnect data msh config 21 --reset       write its default

        --set writes the one value the words select and reads it back: a
        number, an option name for a list or command value, or on/off; a
        device value ("Event 1 target") takes a device among its entries by
        its name, type or address, or none; a text value ("Device name")
        takes the text, up to the length shown. --reset writes the default
        the device gives; never to a button, which it would press, nor to
        a counter or date unless forced. Either is checked against the value's range and
        the device's writeable flag before anything is sent (--force skips
        both); the device checks again and may refuse. Forced, a write to a
        switch (an alarm, or a button or flag of range 0..1) is followed by
        the frame that names this computer as the writer, which a device may
        need before it acts; untried on a real device, so expect nothing. When the words
        select several values, they are listed and nothing is written; an
        index alone is one value per page, so a write by index names the
        page or a word of the value's name.

        A listing has an attribute column like ls -l's mode: the value's type
        as a letter (n number, f flag, l list, a alarm, b button, t text,
        c counter, d date, D device, C command), then w when the device
        marks it writeable, e when it broadcasts the value (eventable), g
        when it is greyed out, and - for each that is not.

        Args:
            set: The value to write; quoted when it has spaces: --set "Single device".
            reset: Write the value's default instead.
            force: Send it even if the device marks the value read-only or the value is out of range; after a
                switch, send the frame naming this computer as the writer.
            timeout: How many seconds to wait for devices to announce themselves.
            cache: Keep a device's names and units in ~/.cache/mvconnect; --nocache reads them again and updates it.
            debug: Log every frame on the link, and say where each line comes from.
        """
        start(self.data, debug)
        timeout = seconds(timeout)
        text = typed("set", set, "-s", command="data")  # as typed: "Bow, port", 007, not fire's tuple, number or True
        if len(occurrences("set", "-s", command="data")) > 1:
            _fail("--set is given more than once, and only the last would count; nothing was written")
        if set is not None and (text is None or (text == "" and not force)):  # no value, or an empty one
            _fail('--set needs a value: --set 7, --set on, --set "Single device" (an empty text with --force)')
        if isinstance(set, (tuple, list, dict)) and text == str(set):  # several things, from Python
            _fail(f"--set takes one value, not {set!r}")
        if text is not None and reset:
            _fail("--set and --reset exclude each other")
        writing = text is not None or reset
        try:
            said = [str(w) for w in words]
            with open_system(refresh=not cache) as system:
                if not said:
                    if writing:
                        _fail("a write names the device and the value: data DEVICE [PAGE] VALUE --set X")
                    return self._devices(system, timeout)
                with show_progress("Identifying", "devices"):
                    sel = system.locate(said, timeout)
                device = sel.device
                if not writing:
                    return self._show(system, sel)
                if not sel.words:
                    _fail("a write names the value: data DEVICE [PAGE] VALUE --set X")
                with show_progress("Reading layout", "values") as progress:
                    page, value_words, found = system.find_selection(sel, progress)
                if len(found) != 1:
                    self._show(system, sel)  # the listing, so the next line can name one
                    hint = "" if page else "; a page keyword (monitor, alarm, history, config) narrows it"
                    _fail(f"{value_words!r} selects {len(found)} values; nothing written: name one{hint}")
                table, value = found[0]
                try:
                    if reset:
                        written = system.write_default(device, table, value, force)
                    else:
                        assert text is not None  # writing, and not a reset
                        written = system.write(device, table, value, text, force, timeout)
                except Refused as e:
                    _fail(f"{e} (--force sends it anyway)")
                except (ValueError, TimeoutError) as e:
                    _fail(str(e))
                for warning in written.warnings:
                    logger.warning(warning)
                out = _table(("value", "before", "after", "unit"))
                out.add_row(_path(device.selector, table.page.name) + " / " + _value(value.index, value.name),
                            _esc(written.before), f"[bold]{_esc(written.after)}[/]", f"[dim]{_esc(value.unit)}[/]")
                _print(out)
                label = f"{device.selector} / {table.page.name} / [{value.index}] {value.name}"
                if not written.took:
                    _fail(f"{label}: wanted {written.wanted}, reads back {written.after}")
                logger.info(f"{label}: {written.before} -> {written.after}")
        except (LinkError, AddressError, TimeoutError, ValueError) as e:  # a short answer among them
            _fail(explained(e))

    def _devices(self, system: System, timeout: float) -> None:
        with show_progress("Identifying", "devices") as progress:
            devices = system.devices(timeout=timeout, progress=progress)
        if not devices:
            _fail("no devices answered")
        table = _table(("selector", "type", "name", "product", "serial", "version"))
        for d in devices:
            table.add_row(f"[bold cyan]{d.selector}[/]", _esc(d.kind), _esc(d.name), _esc(d.product), _esc(d.serial),
                          d.version)
        _print(table)

    def _show(self, system: System, sel: Selection) -> None:
        """Read and show the values the selection *sel* names: one in detail, several as the device groups them.

        Raise AddressError if the words of *sel* name no value.
        """
        device = sel.device
        with show_progress("Reading layout", "values") as progress:
            page, words, found = system.find_selection(sel, progress)  # no words: every value
        if not found and not words:  # a page without values, as the alarm page of most devices
            logger.info(f"{device.selector} {page or ''}: no values".replace(" :", ":"))
            return
        if not found:
            raise AddressError(f"no value {words!r} on {device.selector} {page or ''}".rstrip())
        by_page: dict[str, tuple[PageTable, list]] = {}  # the tables in page order, with their matches
        for table, value in found:
            by_page.setdefault(table.page.name, (table, []))[1].append(value)
        sections = [(title, table, indices) for table, matches in by_page.values()
                    for title, indices in system.sections(table, matches)]
        with show_progress("Reading values", "values") as progress:
            total = sum(len(indices) for _, _, indices in sections)
            done = 0
            rows = []
            for title, table, indices in sections:
                readings = system.read_values(device, table, indices)
                texts = system.read_texts(device, table, indices)
                entries = system.read_entries(device, table, indices)  # of device and command values, live
                values = {i: dataclasses.replace(table.values[i], options=entries[i]) if i in entries
                          else table.values[i] for i in indices}
                done += len(indices)
                progress(done, total)
                rows.append((title, table, indices, values, readings, texts))
        if words and total == 1:  # one value: the details
            _, table, indices, values, readings, texts = rows[0]
            return self._print_details(device, table, [values[i] for i in indices], readings, texts)
        out = _table(("value", "attrs", "value", "unit"))
        for title, table, indices, values, readings, texts in rows:
            out.add_row(_path(device.selector, table.page.name, title), "", "", "")
            width = max(len(str(i)) for i in indices)
            for i in indices:
                value = values[i]
                shown = texts[i] if i in texts else value.format(readings[i])
                out.add_row("  " + _value(i, value.name, width), _flags(value), f"[bold]{_esc(shown)}[/]",
                            f"[dim]{_esc(value.unit)}[/]")
        _print(out)

    @staticmethod
    def _print_details(device, table: PageTable, found, values, texts) -> None:
        """Print one block per value of *found*: what it is and what --set accepts."""
        con = _stdout_console()
        for n, value in enumerate(found):
            if n:
                con.print()
            con.print(_path(device.selector, table.page.name) + " / " + _value(value.index, value.name)
                      + "  " + _flags(value))
            out = _table(("", ""), header=False)
            for label, text in value.describe(values[value.index], texts.get(value.index)):
                if label == "writeable" and text == "no":
                    text = "no (--force to try)"
                style = "bold" if label == "value" else ""
                out.add_row(f"  [dim]{label}[/]", f"[{style}]{_esc(text)}[/]" if style else _esc(text))
            con.print(out)

    def login(self, level: str = "installer", password: str | None = None, status: bool = False, timeout: float = 1.5,
              debug: bool = False):
        """Log in on every device on the bus, so the values of that level become writeable.

        It sends what MasterAdjust's login sends: the level, with the
        level's password, to each device. The levels are installer,
        distributor and service, Mastervolt's names. The password is the
        number MasterAdjust's login asks for; without --password it is
        asked for here. The devices decide what a level unlocks; `data`
        shows it in the attribute column. A device that keeps no login
        level is left alone. `mvconnect logout` logs out again;
        `mvconnect login --status` shows each device's level.

        Args:
            level: installer, distributor or service.
            password: The level's password, a number (asked for when not given).
            status: Only show the level each device is logged in at.
            timeout: How many seconds to wait for devices to announce themselves.
            debug: Log every frame on the link, and say where each line comes from.
        """
        start(self.login, debug)
        timeout = seconds(timeout)
        if status:
            ignored = [f for f, given in (("--level", level != "installer"), ("--password", password is not None))
                       if given]
            if ignored:
                logger.warning(f"--status only shows the levels; {' and '.join(ignored)} ignored")
            return self._login(None, timeout)
        password = typed("password", password, "-p", command="login")
        name = str(level).strip().lower()
        if name not in LOGIN_LEVELS:
            logger.error(f"login: no level {level!r}")
            logger.info("the levels are " + ", ".join(LOGIN_LEVELS))
            sys.exit(1)
        if password is None:
            import getpass
            try:
                password = getpass.getpass(f"{name} password: ")
            except EOFError:
                _fail("no password: give --password, or run login on a terminal to be asked")
        try:
            value = float(password)
            if not sendable(value):
                raise ValueError
        except ValueError:
            _fail("the password is a number")
        if not value:
            _fail("a password of 0 would log out; use logout for that")
        self._login(LOGIN_LEVELS[name], timeout, value)

    def logout(self, timeout: float = 1.5, debug: bool = False):
        """Log out on every device on the bus: login level 0, as MasterAdjust's Logout button sends.

        Args:
            timeout: How many seconds to wait for devices to announce themselves.
            debug: Log every frame on the link, and say where each line comes from.
        """
        start(self.logout, debug)
        timeout = seconds(timeout)
        self._login(0, timeout)

    @staticmethod
    def _login(level: int | None, timeout: float, password: float = 0.0) -> None:
        """Log every device in at *level* with *password*, 0 to log out, and show its level before and after.

        With *level* None, only show the level each device is at. Exit with status 1 when no device answers,
        when the link fails, and when a device that keeps a login level does not end at the level sent.
        """
        try:
            with open_system() as system:
                with show_progress("Identifying", "devices") as progress:
                    devices = system.devices(timeout=timeout, progress=progress)
                if not devices:
                    _fail("no devices answered")
                out = _table(("selector", "type", "name", "before", "after") if level is not None
                             else ("selector", "type", "name", "level"))
                missed = []  # the devices that do not end at the level sent
                for d in devices:
                    try:
                        before = system.login_level(d)
                    except (TimeoutError, ValueError) as e:
                        logger.debug(f"{d.selector}: no login level, left alone: {e}")
                        out.add_row(f"[bold cyan]{d.selector}[/]", _esc(d.kind), _esc(d.name), "[dim]no login[/]",
                                    *([""] if level is not None else []))
                        continue
                    if level is None:
                        out.add_row(f"[bold cyan]{d.selector}[/]", _esc(d.kind), _esc(d.name),
                                    f"[bold]{login_level_name(before)}[/]")
                        continue
                    acknowledged, after = system.login(d, level, password)
                    if not acknowledged:
                        logger.debug(f"{d.selector}: no acknowledgement; it reports {login_level_name(after)}")
                    if after != level:
                        missed.append(d.selector)
                        logger.warning(f"{d.selector} {d.name}: sent {login_level_name(level)}, the device reports "
                                       f"{login_level_name(after)}")
                    out.add_row(f"[bold cyan]{d.selector}[/]", _esc(d.kind), _esc(d.name), login_level_name(before),
                                f"[bold]{login_level_name(after)}[/]")
                _print(out)
                if missed:
                    _fail(f"{len(missed)} of {len(devices)} devices are not {login_level_name(level)}: "
                          + ", ".join(missed))
        except (LinkError, AddressError, TimeoutError, ValueError) as e:  # a short answer among them
            _fail(explained(e))

    def monitor(self, filter=None, timeout: float = 1.5, debug: bool = False):
        """Log the traffic on the bus as it passes, until Ctrl-C.

        One line per frame, at info level, laid out as a listing:
        seconds since the start, tx for a frame this computer sent or rx
        for one it received, the frame's kind, its address (the device a
        request goes to, the one a reply comes from), the data bytes, the
        device's name, and what the frame means where that is known. A
        login's password shows as ****.

        Through a server, this is the traffic its link sees; the server
        serves no other command while it runs.

        Args:
            filter: Only frames to or from these devices, comma separated, each an address, name or type.
            timeout: How many seconds to wait for devices to announce themselves.
            debug: Log every frame on the link, and say where each line comes from.
        """
        start(self.monitor, debug)
        timeout = seconds(timeout)
        given = occurrences("filter", "-f", command="monitor")
        if len(given) > 1:
            _fail(f"--filter is given {len(given)} times, and only the last would count: "
                  "give them all in one, comma separated (--filter msh,0a:1062b)")
        if filter is not None and given and given[-1] is None:  # a bare --filter
            _fail("--filter needs the devices: --filter msh,0a:1062b")
        filter = given[-1] if given else filter  # as typed, not fire's tuple
        try:
            with open_system() as system:
                with show_progress("Identifying", "devices") as progress:
                    devices = system.devices(timeout=timeout, progress=progress)
                names = {d.address: f"{d.kind} {d.name}" for d in devices}
                names.setdefault(HOST_ADDRESS, "USB Link")
                wanted = self._filter(system, filter, timeout)
                if wanted:
                    logger.info("only " + ", ".join(f"{a} {names.get(a, '')}".rstrip() for a in sorted(wanted)))
                logger.info(f"{'seconds':>8}  -- kind address  {'data':<23}  {'device':<22} meaning")
                started, count = None, 0
                try:
                    for t, frame, sent in system.traffic():
                        if wanted and frame.address not in wanted:
                            continue
                        started = t if started is None else started
                        count += 1
                        logger.info(decode.line(frame, sent, t - started, names))
                except KeyboardInterrupt as e:
                    winding_down(e, f"the monitor stops after {count} frames", logger)
                else:
                    logger.info(f"{count} frames")
        except (LinkError, AddressError, TimeoutError, ValueError) as e:  # a short answer among them
            _fail(explained(e))

    @staticmethod
    def _filter(system: System, given, timeout: float) -> set:
        """Return the addresses --filter names, *given* comma separated: each an address, or a device's name or type.

        A name or type is looked up on the bus, waiting up to *timeout* seconds for devices to announce
        themselves. Nothing given is an empty set.
        """
        if given is None or given == "":
            return set()
        words = list(given) if isinstance(given, (list, tuple)) else str(given).split(",")
        wanted = set()
        for word in (str(w).strip() for w in words):
            if not word:
                continue
            try:
                wanted.add(Address.parse(word))
            except ValueError:
                wanted.add(system.device(word, timeout).address)
        return wanted

    def version(self):
        """Show this mvconnect's version, as `mvconnect --version` does."""
        return _version()

    def link(self, debug: bool = False):
        """Show the MasterBus USB Link plugged in: the one the commands use.

        With several plugged in, the first one found is used and the others
        are named in a warning.

        Args:
            debug: Log every HID device of Mastervolt's, and whether this user may open the link.
        """
        start(self.link, debug)
        try:
            link = find_link()
        except LinkError as e:
            _fail(f"{explained(e)}; try --debug")
        return describe_link(link)


def serve_setup(debug: bool) -> None:
    """Set up the server before it serves: logging, at debug level with *debug*, and the bus state in a thread.

    The server owns the link from then on; the commands it runs are pointed at the state and answer at once.
    A link that cannot be opened ends the server here, with status 1 and the reason: a server without the
    link could only pass the same failure on to every command. The state is shut down when the server exits.

    A server reachable from other computers without a password lets anyone who reaches it write to every
    device on the bus. fireaid's server says so as it starts, as a warning: on a network of one's own that may
    be what is wanted.
    """
    from mvconnect import state
    setup_logging(debug)
    logger.warning(EXPERIMENTAL)
    try:
        server = state.start_in_thread()
    except LinkError as e:
        _fail(f"{e}; the server does not start")
    atexit.register(server.shutdown)  # Ctrl-C, SIGTERM and SIGHUP end fireaid's server with an exit


def _ended(signum, _frame) -> None:
    """End the command on SIGTERM or SIGHUP as an exit, so that its cleanups run, a flash-mode exit among them.

    Raise Ended, a SystemExit with status 128 plus *signum*, which the cleanups log as the cause.
    """
    raise Ended(signum)


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("mvconnect")
    except PackageNotFoundError:  # run from a source tree that was never installed
        return "unknown"


def main() -> None:
    """Run the command line.

    Ctrl-C ends a command with status 130, and SIGTERM and SIGHUP, which fireaid's server sends a command whose
    client went away, with 128 plus the signal's number. Either way the command winds down first: the first
    line about the signal is a warning, each step after it logs at info level what it closes, and a last line
    says the wind-down is complete; never a traceback.
    monitor, which runs until Ctrl-C, ends on it normally. `mvconnect --version` alone prints the version.
    """
    if sys.argv[1:] == ["--version"]:
        print(_version())
        return
    for s in ENDING:
        if s != signal.SIGINT:
            signal.signal(s, _ended)
    try:
        fire.Fire(Tool(), remote=fire.Remote(deny=("firmware",), setup=serve_setup))
    except KeyboardInterrupt as e:
        winding_down(e, "wound down, exiting with status 130", logger)
        sys.exit(130)
    except Ended as e:
        winding_down(e, f"wound down, exiting with status {e.code}", logger)
        raise


if __name__ == "__main__":
    main()
