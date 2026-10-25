"""The bootloader transfer protocol: how a firmware image is written to a device.

IMPORTANT: the transfer has NOT been confirmed on the bus. No capture of a
device being flashed exists yet; flash() has never been run against a real
device, and rehearse_flash() is as far as a device has been taken. The tests
run flash() against a fake bootloader that models the protocol described
here, and nothing more.

A broken or interrupted flash is recoverable by design: the
bootloader lives below the image's start address (meta.start), is never inside the written range, and
keeps the device in flash mode until a flash completes, so the device is
flashed again. What nothing recovers from is a write OUTSIDE the meta
record's range — the shipped HEX even carries the bootloader's bytes below
meta.start, so a tool that flashed the file's extent instead of the meta
range would overwrite it. The range here therefore comes from meta.start and
meta.end only, never from the file's extent. Before the write path may be
enabled, a capture of a device being flashed has to settle the erase size and
fill value for format-1 files (the record carries none) and whether the meta
record's end address is the last one written (this module stops one byte
short, never touching a sector it cannot show is meant to be written).
`erase_size` and `fill_value` are therefore passed in rather than read from
the file.

Two frame kinds carry the flash:

  * kind 0x0e, addressed to the device — the mode handshake. 0x83 + META_START
    then 0x84 + META_END enter flash mode; the device enforces that order and
    then re-announces with the bootloader bit set, which the host checks before
    it programs anything. 0x85 leaves flash mode. Replies are the ordinary kind
    0x0c, or NA 0x0d.
  * kind 0x3d, broadcast — the programming, acted on only by the single device
    in flash mode. The 5-bit address-high field carries a sub-command selector
    and the 18-bit low field a bank number or in-bank offset. Replies come back
    as ACK kind 0x1d or NAK kind 0x2d, each echoing the selector.

A 36-bit flash address splits as bank = address >> 18, offset = address & 0x3ffff;
a programmed range never crosses a bank: it is clipped at the bank's end.

Sequence to flash one processor:
    enter mode (0x83, 0x84) -> confirm the bootloader bit in the announcement
    -> processor-select
    -> for each erase block: bank-select (erases) -> stream data (<=8 B/frame)
       -> bank-close (the device returns a CRC-16 the host checks)
    -> done -> leave mode (0x85)

Pacing and retries: this module waits for the ACK of every frame before it
sends the next, so a bank-select is acknowledged only once its erase is done.
It re-sends processor-select, bank-select, bank-close and done when no answer
comes, but never a data frame: a data frame that fails aborts the flash, and
the block is redone whole the next time.

Re-flashing a device that is already in flash mode (after an abort, or a power
cut mid-update) needs no change on the wire, only in the flow: the device still
answers ENUMERATE (with the bootloader bit) and 0x80/0x81/0x82, but ignores
0x83/0x84 without a reply, so the handshake is skipped when the bit is already
announced. Processor-select is accepted again for the same processor. A bank
left open by an abort mid-block makes the next bank-select NAK; a bank-close
with count 0 is always accepted and closes it (a force-close), after which
the select is retried. After a CRC-mismatch abort the bank is
already closed, and after a power cut the bootloader's state is fresh.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from mvconnect.firmware import META_END, META_START, Firmware, Loader
from mvconnect.frame import KIND_ANNOUNCE, KIND_ENUMERATE, Address, Frame
from mvconnect.masterbus import AnnounceInfo, Device, MasterBus, Progress, uninterrupted, winding_down

logger = logging.getLogger(__name__)


# kind 0x3d programming frames: selector in the address-high field, reply kinds
FLASH_KIND = 0x3D
ACK_KIND = 0x1D
NAK_KIND = 0x2D

SEL_DATA = 0
SEL_PROCESSOR = 1
SEL_BANK_SELECT = 2
SEL_BANK_CLOSE = 3
SEL_DONE = 4
SEL_NAMES = {SEL_DATA: "data", SEL_PROCESSOR: "processor-select", SEL_BANK_SELECT: "bank-select",
             SEL_BANK_CLOSE: "bank-close", SEL_DONE: "done"}

BANK_SHIFT = 18
BANK_SIZE = 1 << BANK_SHIFT
BANK_OFFSET_MASK = BANK_SIZE - 1  # 0x3ffff
MAX_DATA = 8  # bytes of firmware image per data frame

# how long to wait for a reply, and how often to re-send, per frame class
CONTROL_TIMEOUT = 1.0
CONTROL_RETRIES = 2  # processor-select, bank-select, bank-close, done
DATA_TIMEOUT = 1.0
DATA_RETRIES = 0  # a data frame is never sent twice: the block is redone whole
CONFIRM_ATTEMPTS = 5  # enumerate-and-look-for-the-bootloader-bit rounds after 0x84



class FlashError(Exception):
    """The flash cannot proceed.

    The cause is a refused or unanswered frame, a CRC mismatch, or a file without a usable range.
    """


class Rejected(FlashError):
    """The device answered NAK (kind 0x2d)."""


class NotEntered(FlashError):
    """The device did not answer the first step of the flash-mode handshake, so it is not in flash mode."""


def crc16_ccitt(data: bytes, crc: int = 0xFFFF) -> int:
    """Return the CRC-16/CCITT of *data*, continued from *crc*.

    The CRC has polynomial 0x1021, init 0xFFFF, MSB first, no reflection. The device computes this over the
    bytes written to a bank and returns it at bank-close; the host computes the same and compares.
    """
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


@dataclass
class FlashPlan:
    """The erase blocks a flash is split into, from the file's meta range.

    It holds the range's *start* and *end* (exclusive), the *erase_size*, and the *blocks* as (flash address,
    length) pairs.
    """

    start: int
    end: int
    erase_size: int
    blocks: list[tuple[int, int]] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        """Return the bytes in all blocks."""
        return sum(length for _, length in self.blocks)

    @property
    def total_frames(self) -> int:
        """Return the data frames the blocks take, at MAX_DATA bytes each."""
        return sum((length + MAX_DATA - 1) // MAX_DATA for _, length in self.blocks)


def plan(firmware: Firmware, erase_size: int) -> FlashPlan:
    """Return the FlashPlan splitting the flash range of *firmware* into erase blocks of *erase_size* bytes.

    Raise FlashError if the range or the size is unusable: a range that is empty or does not start in the
    file's own data, a size that is not positive, or a Microchip image, whose addresses are not understood
    well enough to plan a flash.

    The range is [meta.start, meta.end): the end may be meant as the last address written, and for a format-1
    file the sector at meta.end may be meant to be erased and filled too. Until a capture shows either, the
    plan stops one byte short and touches no sector it cannot show is meant to be written. A block is clipped
    at a bank boundary, bank + 0x3ffff.
    """
    m = firmware.meta
    start, end = m.start, m.end
    if m.packing != "plain":
        # Microchip images: their meta range seems to count program words, where the HEX file counts bytes at
        # two per word, with a phantom byte per cell. Until that is confirmed, nothing is planned for them.
        raise FlashError(f"{firmware.path.name} is a Microchip image ({m.packing}), whose addresses are not "
                         "understood well enough to plan a flash")
    if not (start and end and end > start):
        raise FlashError(f"firmware has no usable image range ({start:#x}..{end:#x})")
    if not firmware.base <= start < firmware.base + firmware.size:
        # a range the file's own data does not start: a meta record read wrongly, and a plan that could reach
        # the bootloader, which nothing recovers from
        raise FlashError(f"the image range from {start:#x} to {end:#x} does not start in the file's data, from "
                         f"{firmware.base:#x} to {firmware.base + firmware.size:#x}: its meta record is not understood")
    if erase_size <= 0:
        raise FlashError(f"erase size must be positive, not {erase_size}")
    blocks = []
    a = start
    while a < end:
        bank_end = ((a >> BANK_SHIFT) + 1) << BANK_SHIFT
        length = min(erase_size, end - a, bank_end - a)
        blocks.append((a, length))
        a += length
    return FlashPlan(start, end, erase_size, blocks)


# -- the mode handshake (kind 0x0e)

def enter_flash_mode(bus: MasterBus, address: Address) -> None:
    """Send Loader.ARM (0x83 + META_START) then Loader.ENTER (0x84 + META_END) to the device at *address*.

    The device arms on the first and enters flash mode only on the second, in that order; both carry the 14
    meta-marker bytes split across them. Raise NotEntered if the first goes unanswered, and FlashError if the
    second does, after which the device may be in flash mode.
    """
    try:
        bus.request(address, bytes([Loader.ARM]) + META_START, echo=1)
    except TimeoutError as e:
        raise NotEntered(f"{address} did not accept the flash-mode handshake: {e}") from e
    try:
        bus.request(address, bytes([Loader.ENTER]) + META_END, echo=1)
    except TimeoutError as e:
        raise FlashError(f"{address} did not answer the end of the flash-mode handshake: {e}") from e
    logger.info(f"{address} accepted the flash-mode handshake")


def _announcement(bus: MasterBus, address: Address) -> AnnounceInfo:
    """Enumerate once and return the announcement of the device at *address*.

    Raise TimeoutError if it does not announce itself, and ValueError if the announcement is malformed.
    """
    frame = bus.exchange(Frame(KIND_ENUMERATE, bus.host), lambda f: f.kind == KIND_ANNOUNCE and f.address == address,
                         timeout=CONTROL_TIMEOUT, retries=0)
    return AnnounceInfo.parse(frame.data)


def in_flash_mode(bus: MasterBus, address: Address) -> bool:
    """Tell whether the device at *address* already announces the bootloader bit, with one enumerate.

    False also when it does not announce itself or its announcement is malformed. A device in flash mode
    (after an abort, or a power cut mid-update) still answers ENUMERATE and 0x80/0x81/0x82, but is expected
    to ignore 0x83/0x84 without any reply, so the handshake has to be skipped for it.
    """
    try:
        return _announcement(bus, address).flash_mode
    except (TimeoutError, ValueError):
        return False


def confirm_flash_mode(bus: MasterBus, address: Address) -> None:
    """Enumerate until the device at *address* announces itself with the bootloader bit set.

    Raise FlashError if it does not within CONFIRM_ATTEMPTS rounds, or if an announcement is malformed.

    Nothing is programmed until this announcement with the bootloader bit (info & 0x20) is seen. The device
    is expected to re-announce after 0x84 on its own; enumerating makes sure the announcement is heard.
    """
    for attempt in range(CONFIRM_ATTEMPTS):
        try:
            flash_mode = _announcement(bus, address).flash_mode
        except TimeoutError:
            logger.debug(f"{address} did not announce itself, attempt {attempt + 1}")
            continue
        except ValueError as e:
            raise FlashError(f"{address}: bad announcement while confirming flash mode: {e}") from e
        if flash_mode:
            logger.info(f"{address} is in flash mode (bootloader bit set)")
            return
        logger.debug(f"{address} announced without the bootloader bit, attempt {attempt + 1}")
    raise FlashError(f"{address} did not enter flash mode: no announcement with the bootloader bit")


def leave_flash_mode(bus: MasterBus, address: Address, processor: int, version: int) -> None:
    """Send Loader.LEAVE (0x85) to the device at *address* and make sure it left flash mode.

    The device honours 0x85 once a 'done' has armed it; its next announcement must come without the
    bootloader bit. The frame carries *processor* and a 32-bit software version at data[2..5]: *version*, the
    file's. It is sent once, never again into an application that has started. Raise TimeoutError if neither
    an answer nor an announcement comes, and FlashError if the device announces itself still in flash mode.
    An answer without an announcement after it is only logged as a warning.
    """
    data = bytes([Loader.LEAVE, processor & 0xFF]) + (version & 0xFFFFFFFF).to_bytes(4, "little")
    unanswered = None
    try:
        bus.request(address, data, echo=1, retries=0)
    except TimeoutError as e:
        unanswered = e
    try:
        still = _announcement(bus, address).flash_mode
    except (TimeoutError, ValueError):
        if unanswered is not None:
            raise unanswered from None
        logger.warning(f"{address} answered 0x85 but has not announced itself since; check that it came up")
        return
    if still:
        raise FlashError(f"{address} still announces itself in flash mode after 0x85")
    logger.info(f"{address} left flash mode")


# -- the programming frames (kind 0x3d)

def _frame(selector: int, low: int, data: bytes = b"") -> Frame:
    """Build a 0x3d programming frame: selector in the address-high field, bank/offset in the low 18 bits."""
    return Frame(FLASH_KIND, Address(selector, low), data)


def _program(bus: MasterBus, selector: int, low: int, data: bytes = b"") -> Frame:
    """Send one 0x3d frame and return the ACK.

    Raise Rejected on a NAK, and FlashError when no reply comes within the frame class's retries.
    """
    frame = _frame(selector, low, data)
    is_data = selector == SEL_DATA
    try:
        reply = bus.exchange(frame, lambda f: f.kind in (ACK_KIND, NAK_KIND) and f.address.hi == selector,
                             timeout=DATA_TIMEOUT if is_data else CONTROL_TIMEOUT,
                             retries=DATA_RETRIES if is_data else CONTROL_RETRIES)
    except TimeoutError as e:
        raise FlashError(f"no acknowledgement for {SEL_NAMES.get(selector, selector)} "
                         f"(bank/offset {low:#x}): {e}") from e
    if reply.kind == NAK_KIND:
        raise Rejected(f"device rejected {SEL_NAMES.get(selector, selector)} "
                       f"(bank/offset {low:#x}, reply {reply.data.hex(' ')})")
    return reply


def _processor_select(bus: MasterBus, processor: int) -> None:
    _program(bus, SEL_PROCESSOR, processor & BANK_OFFSET_MASK)


def _bank_select(bus: MasterBus, bank: int, offset: int) -> None:
    """Select (erase) a bank; a bank left open by an earlier abort is force-closed first.

    The device refuses bank-select while a bank is open. A bank-close with
    count 0 is accepted whatever was written and closes the bank, ACK or NAK
    (a force-close); after it the select is retried once.
    """
    data = offset.to_bytes(3, "little")
    try:
        _program(bus, SEL_BANK_SELECT, bank & BANK_OFFSET_MASK, data)
    except Rejected as e:
        logger.info(f"bank {bank:#x} refused select ({e}); force-closing it and retrying")
        try:
            _program(bus, SEL_BANK_CLOSE, bank & BANK_OFFSET_MASK, (0).to_bytes(3, "little"))
        except Rejected:
            pass  # count 0 closes the bank even when the device answers NAK
        _program(bus, SEL_BANK_SELECT, bank & BANK_OFFSET_MASK, data)


def _data(bus: MasterBus, offset: int, chunk: bytes) -> None:
    _program(bus, SEL_DATA, offset & BANK_OFFSET_MASK, chunk)


def _bank_close(bus: MasterBus, bank: int, count: int) -> int:
    """Close a bank and return the CRC-16 the device reports (reply data[4] | data[5] << 8).

    Raise FlashError as _program() does, and if the reply is too short to carry a CRC.
    """
    reply = _program(bus, SEL_BANK_CLOSE, bank & BANK_OFFSET_MASK, count.to_bytes(3, "little"))
    if len(reply.data) < 6:
        raise FlashError(f"bank-close reply too short for a CRC: {reply.data.hex(' ')}")
    return reply.data[4] | (reply.data[5] << 8)


def _done(bus: MasterBus, bank: int) -> None:
    _program(bus, SEL_DONE, bank & BANK_OFFSET_MASK)


def flash(bus: MasterBus, device: Device, firmware: Firmware, *, erase_size: int, fill_value: int,
          processor: int = 0, progress: Progress | None = None) -> FlashPlan:
    """Write *firmware* to *device* over *bus* and return the FlashPlan used.

    This erases and programs the device: a caller asks for explicit confirmation first.

    *erase_size* and *fill_value* belong to the firmware's meta section (or to defaults, for files whose
    record carries none), which this module cannot decode yet, so they are passed in; addresses the file
    does not define within the flash range are sent as *fill_value*. *processor* is the processor to
    program, 0 by default. *progress*, if given, is called with the bytes done and the total.

    Integrity: the device must announce the bootloader bit before anything is programmed; every step is
    acknowledged (kind 0x1d) before the next; each block's CRC-16 is compared against the device's bank-close
    reply. A bank-select refused because a bank is still open is force-closed and retried once; any other
    NAK, a timeout or a CRC mismatch aborts with FlashError before 'done' is ever sent, so an aborted device
    keeps its bootloader, stays in flash mode and is flashed again. Whatever stops the flash, Ctrl-C
    included, the exit (0x85) is still attempted, and its failure is only logged. Raise FlashError too if
    the plan cannot be made, and if the device was flashed but does not leave flash mode.
    """
    p, image_byte = _prepare(firmware, erase_size, fill_value)
    logger.info(f"flashing {device.selector}: {p.total_bytes} bytes, {len(p.blocks)} blocks, "
                f"{p.total_frames} data frames")
    done_bytes = 0
    try:  # from the handshake on: a reply lost after 0x84, or Ctrl-C there, leaves the device in flash mode
        _enter(bus, device)
        confirm_flash_mode(bus, device.address)
        _processor_select(bus, processor)
        for block_addr, length in p.blocks:
            bank = block_addr >> BANK_SHIFT
            _bank_select(bus, bank, block_addr & BANK_OFFSET_MASK)
            crc = 0xFFFF
            addr = block_addr
            while addr < block_addr + length:
                n = min(MAX_DATA, block_addr + length - addr)
                chunk = bytes(image_byte(addr + i) for i in range(n))
                _data(bus, addr & BANK_OFFSET_MASK, chunk)
                crc = crc16_ccitt(chunk, crc)
                addr += n
                done_bytes += n
                if progress:
                    progress(done_bytes, p.total_bytes)
            device_crc = _bank_close(bus, bank, length)
            if device_crc != crc:
                raise FlashError(f"CRC mismatch in block {block_addr:#010x}: device reports "
                                 f"{device_crc:#06x}, computed {crc:#06x}")
        _done(bus, p.blocks[-1][0] >> BANK_SHIFT)
    except BaseException as e:  # Ctrl-C too: the exit is attempted whatever stopped the flash
        winding_down(e, f"taking {device.selector} out of flash mode before ending", logger)
        _try_leave(bus, device.address, processor, firmware.meta.version)
        raise
    try:
        leave_flash_mode(bus, device.address, processor, firmware.meta.version)
    except (TimeoutError, FlashError) as e:
        raise FlashError(f"{device.selector} was flashed but did not leave flash mode: {e}") from e
    logger.info(f"{device.selector} flashed and left flash mode")
    return p


def _prepare(firmware: Firmware, erase_size: int, fill_value: int) -> tuple[FlashPlan, Callable[[int], int]]:
    """Return the plan of a flash and a function giving the byte to program at a flash address.

    The byte is the image's, or *fill_value* where the file defines none. Raise FlashError if *fill_value* is
    not a byte, or as plan() does.
    """
    if not 0 <= fill_value <= 0xFF:
        raise FlashError(f"fill value must be a byte, not {fill_value}")
    image, base = firmware.image, firmware.base

    def image_byte(addr: int) -> int:
        off = addr - base
        return image[off] if 0 <= off < len(image) else fill_value

    return plan(firmware, erase_size), image_byte


def _enter(bus: MasterBus, device: Device) -> None:
    """Put *device* into flash mode, skipping the handshake when it is there already."""
    if in_flash_mode(bus, device.address):
        logger.info(f"{device.selector} is already in flash mode; skipping the handshake")
    else:
        enter_flash_mode(bus, device.address)


def _try_leave(bus: MasterBus, address: Address, processor: int, version: int) -> None:
    """Try to take the device at *address* out of flash mode after a failure, best-effort.

    A failure of the exit is only logged as a warning; a second Ctrl-C waits for it.
    """
    try:
        with uninterrupted():
            leave_flash_mode(bus, address, processor, version)
    except Exception as e:  # a lost link too: the original failure goes on, with this warning beside it
        logger.warning(f"{address} may still be in flash mode: {e}")


def rehearse_flash(bus: MasterBus, device: Device, firmware: Firmware, *, erase_size: int, fill_value: int,
                   processor: int = 0) -> FlashPlan:
    """Enter and leave flash mode on *device* for real, but only log the programming of *firmware*.

    Return the FlashPlan.

    The flash-mode handshake (0x83/0x84) and the exit are sent for real; the device genuinely enters its
    bootloader and genuinely leaves it. Everything between (processor-select, every bank-select, every data
    frame, every bank-close) is only logged as 'would send', with the reply it would expect and the CRC-16
    the host would check, and is never put on the bus. So no sector is erased and no byte is written.

    The exit is real: 'done' (0x3d selector 4), which as the protocol is understood only arms the device's
    stop and writes nothing, then 0x85, which takes the device out of flash mode. *erase_size* and
    *fill_value* are the same unverified inputs a real flash needs; here they only shape the logged plan.

    Raise FlashError, with nothing done, if the device does not announce itself or is in flash mode already
    (its application may be erased, and a rehearsal would take it out with nothing programmed); raise it too
    if the plan cannot be made, or if the device does not leave flash mode.

    Entering the bootloader is itself a real state change on the device. Whether a device that enters and
    leaves flash mode without being programmed comes back up on its existing application is device firmware
    behaviour MasterAdjust does not show; the strongest indirect evidence that it does is that the bootloader
    reports software version 0 as a normal, expected ('already in flash mode') state. Rehearse on a device
    you can recover.
    """
    p, image_byte = _prepare(firmware, erase_size, fill_value)

    def would(frame: Frame, expect: str, *, data_frame: bool = False) -> None:
        line = f"  would send  {frame}  dlc {len(frame.data)}  -> expect {expect}"
        (logger.debug if data_frame else logger.info)(line)

    for _attempt in range(3):  # a device that does not announce itself may be in flash mode: no guess either way
        try:
            already = _announcement(bus, device.address).flash_mode
            break
        except (TimeoutError, ValueError) as e:
            heard = e
    else:
        raise FlashError(f"{device.selector} does not announce itself ({heard}), so whether it is in flash mode "
                         "already is not known; nothing was done")
    if already:
        # its application may be erased: a rehearsal's exit would start it with nothing programmed
        raise FlashError(f"{device.selector} is in flash mode already, from an earlier flash; a rehearsal would "
                         "take it out with nothing programmed. Flash it instead, or power-cycle it")
    logger.warning(f"REHEARSAL for {device.selector}: entering flash mode FOR REAL; the programming "
                   f"below is only logged, never sent — nothing is erased or written")
    exit_bank = (p.blocks[-1][0] >> BANK_SHIFT) if p.blocks else 0
    stopped: BaseException | None = None  # what ended the rehearsal early, if anything
    try:  # from the handshake on: a reply lost after 0x84, or Ctrl-C there, leaves the device in flash mode
        enter_flash_mode(bus, device.address)  # never skipped: the device was just seen in its application
        confirm_flash_mode(bus, device.address)
        logger.info(f"---- simulated programming: {p.total_bytes} bytes, {len(p.blocks)} blocks, "
                    f"{p.total_frames} data frames (NOTHING below is sent) ----")
        would(_frame(SEL_PROCESSOR, processor & BANK_OFFSET_MASK), "ACK (kind 0x1d) processor-select")
        for block_addr, length in p.blocks:
            bank = block_addr >> BANK_SHIFT
            would(_frame(SEL_BANK_SELECT, bank, (block_addr & BANK_OFFSET_MASK).to_bytes(3, "little")),
                  "ACK (kind 0x1d) bank-select — this erases the block")
            crc = 0xFFFF
            addr = block_addr
            frames = 0
            while addr < block_addr + length:
                n = min(MAX_DATA, block_addr + length - addr)
                chunk = bytes(image_byte(addr + i) for i in range(n))
                would(_frame(SEL_DATA, addr & BANK_OFFSET_MASK, chunk), "ACK (kind 0x1d) data", data_frame=True)
                crc = crc16_ccitt(chunk, crc)
                addr += n
                frames += 1
            would(_frame(SEL_BANK_CLOSE, bank, length.to_bytes(3, "little")),
                  f"ACK (kind 0x1d) bank-close carrying CRC-16 {crc:#06x}; host would compare")
            logger.info(f"  block {block_addr:#010x}: {length} bytes, {frames} data frames, "
                        f"expect device CRC {crc:#06x}")
        logger.info("---- end simulated programming ----")
    except BaseException as e:
        stopped = e
        raise
    finally:
        if not isinstance(stopped, NotEntered):  # not in flash mode: no 'done' broadcast for another device
            winding_down(stopped, f"taking {device.selector} out of flash mode (done, then 0x85) before ending",
                         logger)
            # done arms the stop, 0x85 leaves flash mode; a second Ctrl-C waits until both are out
            left = False
            try:
                with uninterrupted():
                    _done(bus, exit_bank)
                    leave_flash_mode(bus, device.address, processor, firmware.meta.version)
                    left = True
                    logger.warning(f"REHEARSAL complete for {device.selector}: left flash mode; "
                                   f"nothing was erased or written")
            except BaseException as e:  # a lost link too, or an interrupt held during the exit
                if not left:
                    logger.error(f"REHEARSAL: could not leave flash mode cleanly: {e!r}; the device may still be "
                                 f"in flash mode — power-cycle it and check before relying on it")
                if not isinstance(e, Exception):
                    raise
                if stopped is None:  # otherwise what stopped the rehearsal goes on, Ctrl-C as Ctrl-C
                    raise FlashError(f"{device.selector} did not leave flash mode: {e}") from e
    return p
