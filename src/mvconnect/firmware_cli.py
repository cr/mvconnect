"""The firmware subcommand: what is installed, what is in the library, and (later) flashing."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from mvconnect.download import DownloadError, bundle_dir, bundle_url, cache_dir, extract, fetch
from mvconnect.firmware import Firmware, FirmwareError, identify, version_text
from mvconnect.firmware import library as scan_library
from mvconnect.hidlink import LinkError
from mvconnect.masterbus import MasterBus
from mvconnect.options import typed
from mvconnect.system import AddressError, System

logger = logging.getLogger("mvconnect")


def default_library() -> Path:
    """Return the default library directory, the cache.

    The cache holds every bundle ever fetched, unpacked beside its zip. Older versions stay selectable.
    """
    return cache_dir()


class Firmware_:
    """Firmware of the devices on the bus and files in the local library.

    `fetch` downloads Mastervolt's firmware bundle into
    $XDG_CACHE_HOME/mvconnect/firmware (~/.cache/mvconnect/firmware by
    default) and unpacks it there; that is the default library, or give
    --library DIR with your own Intel HEX files.
    """

    def __init__(self, ui):
        self._ui = ui

    def list(self, *words: str, library: str | None = None, timeout: float = 1.5, debug: bool = False):
        """List the installed firmware per device, and the newest matching file in the library.

        Words select one device as for data; none lists every device.

        Args:
            library: Directory of firmware .hex files.
            timeout: How many seconds to wait for devices to announce themselves; longer while they keep appearing.
            debug: Log every frame on the link.
        """
        ui = self._ui
        ui.start(self.list, debug)
        timeout = ui.seconds(timeout)
        files = library_or_warn(Path(library) if library else default_library(), ui, given=library is not None)
        newest: dict[int, Any] = {}  # product id -> its newest file
        for f in files:
            newest.setdefault(f.meta.product_id, f)
        try:
            said = " ".join(str(w) for w in words)
            if ui.server_here():  # with hidraw it could open the link beside the server, which re-enumerates
                ui.fail("an mvconnect server on this computer holds the link; stop it for the firmware commands")
            with MasterBus() as bus:
                system = System(bus)
                with ui.show_progress("Identifying", "devices") as progress:
                    devices = ([system.device(said, timeout)] if said.split()
                               else system.devices(timeout=timeout, progress=progress))
                if not devices:
                    ui.fail("no devices answered")
                table = ui.table(("selector", "type", "name", "product", "installed", "processors", "library"))
                for d in devices:
                    ident = identify(bus, d.address)
                    procs = ", ".join(f"#{p.number} hw {version_text(p.hardware) if p.hardware is not None else '?'}"
                                      f" sw {version_text(p.software) if p.software is not None else '?'}"
                                      for p in ident.processors) or "-"
                    pid: int | None
                    try:
                        pid = int(d.product)
                    except ValueError:
                        pid = ident.product_id
                    avail = newest.get(pid) if pid is not None else None
                    lib = f"{avail.meta.version_text} ({avail.path.name})" if avail else "-"
                    if ident.product_id is not None and pid is not None and ident.product_id != pid:
                        logger.warning(f"{d.selector}: bootloader product id {ident.product_id} differs from "
                                       f"product {d.product}")
                    table.add_row(f"[bold cyan]{d.selector}[/]", ui.esc(d.kind), ui.esc(d.name), ui.esc(d.product),
                                  d.version, ui.esc(procs), ui.esc(lib))
                ui.print(table)
        except (LinkError, AddressError, TimeoutError, ValueError) as e:  # a short answer among them
            ui.fail(ui.explained(e))

    def info(self, file: str, debug: bool = False):
        """Show what a firmware file says about itself: product, version, image range.

        Args:
            file: An Intel HEX file from Mastervolt.
            debug: Log how the file is read.
        """
        ui = self._ui
        ui.start(self.info, debug)
        try:
            fw = Firmware.load(file)
        except (FirmwareError, OSError) as e:
            ui.fail(str(e))
        m = fw.meta
        table = ui.table(("", ""), header=False)
        for label, text in (("file", str(fw.path)), ("product", m.product), ("version", m.version_text),
                            ("hardware", version_text(m.hardware)), ("image", f"{m.start:#010x} .. {m.end:#010x}"),
                            ("bytes", str(fw.size)), ("meta at", f"{m.offset:#010x}"),
                            ("meta raw", m.raw.hex(" "))):
            table.add_row(f"  [dim]{label}[/]", ui.esc(text))
        ui.print(table)

    def library(self, product: str | None = None, library: str | None = None, debug: bool = False):
        """List the firmware files in the library, every version of every bundle, by product and version.

        Args:
            product: Only files for this product number.
            library: Directory of firmware .hex files (default: the cache with every fetched bundle).
            debug: Log how the files are read.
        """
        ui = self._ui
        ui.start(self.library, debug)
        library_dir = Path(library) if library else default_library()
        files = library_or_warn(library_dir, ui, given=library is not None)
        if product is not None:
            files = [f for f in files if f.meta.product == str(product)]
        if not files:
            logger.info("no firmware files" + (f" for product {product}" if product else ""))
            return
        table = ui.table(("product", "version", "hardware", "file"))
        for f in files:
            table.add_row(f.meta.product, f.meta.version_text, version_text(f.meta.hardware),
                          ui.esc(str(f.path.relative_to(library_dir))))
        others = [] if product else [e for e in scan_library(library_dir, with_errors=True) if e.meta is None]
        for e in others:
            table.add_row("[dim]?[/]", "", "", ui.esc(str(e.path.relative_to(library_dir))))
        ui.print(table)
        if others:
            logger.info(f"{len(others)} files carry no meta section this tool can read; their folder names the family")

    def fetch(self, password: str | None = None, force: bool = False, debug: bool = False):
        """Download Mastervolt's firmware bundle if it changed, and unpack it into the cache.

        The bundle is the "MasterBus Firmware" zip on Mastervolt's downloads
        page, protected by the MasterAdjust installer password. Without the
        password it is only downloaded.

        Args:
            password: The MasterAdjust installer password, to unpack the zip.
            force: Download even if the server says the cached copy is current.
            debug: Log more.
        """
        ui = self._ui
        ui.start(self.fetch, debug)
        password = typed("password", password, "-p", command="fetch")  # a password such as 1,5 or 007, as typed
        directory = cache_dir()
        try:
            url = bundle_url()
            logger.info(f"bundle: {url}")
            if force:  # without its stamp, the bundle is fetched whatever the server says of it
                (directory / (url.rsplit("/", 1)[-1] + ".json")).unlink(missing_ok=True)
            with ui.show_progress("Downloading", "bytes") as progress:
                got = fetch(url, directory, progress=progress)
            if password is None:
                logger.warning("no --password given: the bundle is downloaded but not unpacked")
                return str(got.path)
            target = bundle_dir(got.path)
            if got.updated or not target.is_dir():  # extract() leaves a whole bundle or none
                files = extract(got.path, target, str(password))
                logger.info(f"{len(files)} firmware files unpacked into {target}")
            else:
                logger.info("bundle unchanged, library kept")
            self.library()
        except DownloadError as e:
            ui.fail(str(e))

    def flash(self, *words: str, version: str | None = None, file: str | None = None, library: str | None = None,
              really: bool = False, erase_size: str | None = None, fill: str | None = None,
              timeout: float = 1.5, debug: bool = False):
        """Check a device against a firmware file; --really runs a semi-live rehearsal.

        Words select the device as for data. The firmware is the newest file
        for the device's product in the library, or --version V from the
        library, or --file PATH. Without --really every check runs, the device
        is queried, and nothing touches it. With --really the device is put
        into flash mode FOR REAL and taken out again FOR REAL, but the actual
        programming is only logged frame by frame, never sent: no sector is
        erased and no byte is written. A real write path is still not enabled.
        --really needs --erase-size, since the meta section's erase size is not
        decoded yet; entering flash mode is a real state change, so rehearse on
        a device you can recover.

        Args:
            version: Flash this version from the library instead of the newest.
            file: Flash this .hex file instead of one from the library.
            library: Directory of firmware .hex files (default: the cache).
            really: Run the semi-live rehearsal: real flash-mode handshake and exit, logged programming.
            erase_size: Erase-block size for the rehearsal's frame layout (decimal or 0x..); required with --really.
            fill: Byte sent for image gaps in the rehearsal (decimal or 0x.., default 0xFF).
            timeout: How many seconds to wait for devices to announce themselves; longer while they keep appearing.
            debug: Log every frame on the link (and every would-send data frame in a rehearsal).
        """
        from mvconnect.preflight import choose, preflight

        ui = self._ui
        ui.start(self.flash, debug)
        timeout = ui.seconds(timeout)
        given = {"version": version, "file": file}
        version = typed("version", version, "-v", command="flash")  # 2.10 is not 2.1
        file = typed("file", file, command="flash")
        for name, text in (("version", version), ("file", file)):
            if given[name] is not None and not text:  # bare, or empty: not a choice of the newest
                ui.fail(f"--{name} needs a value; without it the newest file in the library is taken")
        erase_size, fill = typed("erase_size", erase_size, "-e", command="flash"), typed("fill", fill, command="flash")
        try:
            said = " ".join(str(w) for w in words)
            if not said.split():
                raise AddressError("name the device to flash")
            files = library_or_warn(Path(library) if library else default_library(), ui, given=library is not None)
            if ui.server_here():  # with hidraw it could open the link beside the server, which re-enumerates
                ui.fail("an mvconnect server on this computer holds the link; stop it for the firmware commands")
            with MasterBus() as bus:
                system = System(bus)
                with ui.show_progress("Identifying", "devices"):
                    device = system.device(said, timeout)
                try:
                    fw = choose(files, device.product, version=version, file=file)
                except (LookupError, FirmwareError, OSError) as e:
                    ui.fail(str(e))
                pf = preflight(bus, device, fw)
                table = ui.table(("check", "result"), header=False)
                table.add_row("[bold]device[/]", f"{device.selector} {ui.esc(device.kind)} {ui.esc(device.name)}")
                table.add_row("[bold]file[/]", ui.esc(str(fw.path)))
                for f in pf.findings:
                    table.add_row("  [green]ok[/]" if f.ok else "  [bold red]FAIL[/]", ui.esc(f.text))
                ui.print(table)
                if not pf.ok:
                    ui.fail("preflight failed; nothing was written")
                _print_warning(ui)
                if not really:
                    logger.info("dry run complete; the device was only queried. Re-run with --really "
                                "--erase-size N for a semi-live rehearsal (real flash-mode handshake and "
                                "exit, programming only logged). The real write path is not enabled.")
                    return
                if erase_size is None:
                    ui.fail("--really runs a semi-live rehearsal (real flash-mode handshake, logged "
                            "programming, real exit) and needs --erase-size N to lay out the programming "
                            "frames; the meta section's erase size is not decoded yet. Nothing was done.")
                try:
                    es = _integer(erase_size)
                    fv = 0xFF if fill is None else _integer(fill)
                except ValueError:
                    ui.fail("--erase-size and --fill must be integers (decimal or 0x..)")
                from mvconnect import flasher
                try:
                    flasher.rehearse_flash(bus, device, fw, erase_size=es, fill_value=fv)
                except flasher.FlashError as e:
                    ui.fail(str(e))
        except (LinkError, AddressError, TimeoutError, ValueError) as e:  # a short answer among them
            ui.fail(ui.explained(e))


def _print_warning(ui) -> None:
    ui.print("[bold red]WARNING[/bold red]: flashing erases and rewrites the device's application over "
             "the bus. An interrupted flash (dropped bus, reset, power dip) leaves the device in its "
             "bootloader, waiting to be flashed again; the bootloader sits below the written range and "
             "is never touched. What nothing recovers from is a write outside the firmware's meta range, "
             "so the range is taken from the file's meta record only. Still, before --really:\n"
             "  - keep the computer on mains or a full battery, and do not let it sleep;\n"
             "  - keep the MasterBus powered for the whole run; a device that powers the bus stops doing so "
             "while it is being flashed, so power the bus from elsewhere or connect this device alone;\n"
             "  - keep every AC and DC input of the device connected to a stable source;\n"
             "  - terminate the bus at both ends and do not touch cables until it reports success.")


def _integer(value) -> int:
    """Return *value*, a number or its text in decimal or 0x hex, as an int.

    Raise ValueError if it is neither.
    """
    return int(str(value), 0)


def library_or_warn(directory: Path, ui, given: bool):
    """Return the files of the library at *directory*, through *ui*.

    Where the directory does not exist, the result is an empty list with a note if it is the default library,
    and the command fails (ui.fail) if it was *given* with --library.
    """
    if not directory.is_dir():
        if given:
            ui.fail(f"--library {directory}: no such directory")
        logger.info(f"no firmware library at {directory}")
        return []
    return scan_library(directory)
