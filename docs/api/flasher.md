<a id="mvconnect.flasher"></a>

# mvconnect.flasher

The bootloader transfer protocol: how a firmware image is written to a device.

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

<a id="mvconnect.flasher.FLASH_KIND"></a>

#### FLASH\_KIND

<a id="mvconnect.flasher.ACK_KIND"></a>

#### ACK\_KIND

<a id="mvconnect.flasher.NAK_KIND"></a>

#### NAK\_KIND

<a id="mvconnect.flasher.SEL_DATA"></a>

#### SEL\_DATA

<a id="mvconnect.flasher.SEL_PROCESSOR"></a>

#### SEL\_PROCESSOR

<a id="mvconnect.flasher.SEL_BANK_SELECT"></a>

#### SEL\_BANK\_SELECT

<a id="mvconnect.flasher.SEL_BANK_CLOSE"></a>

#### SEL\_BANK\_CLOSE

<a id="mvconnect.flasher.SEL_DONE"></a>

#### SEL\_DONE

<a id="mvconnect.flasher.SEL_NAMES"></a>

#### SEL\_NAMES

<a id="mvconnect.flasher.BANK_SHIFT"></a>

#### BANK\_SHIFT

<a id="mvconnect.flasher.BANK_SIZE"></a>

#### BANK\_SIZE

<a id="mvconnect.flasher.BANK_OFFSET_MASK"></a>

#### BANK\_OFFSET\_MASK

0x3ffff

<a id="mvconnect.flasher.MAX_DATA"></a>

#### MAX\_DATA

bytes of firmware image per data frame

<a id="mvconnect.flasher.CONTROL_TIMEOUT"></a>

#### CONTROL\_TIMEOUT

<a id="mvconnect.flasher.CONTROL_RETRIES"></a>

#### CONTROL\_RETRIES

processor-select, bank-select, bank-close, done

<a id="mvconnect.flasher.DATA_TIMEOUT"></a>

#### DATA\_TIMEOUT

<a id="mvconnect.flasher.DATA_RETRIES"></a>

#### DATA\_RETRIES

a data frame is never sent twice: the block is redone whole

<a id="mvconnect.flasher.CONFIRM_ATTEMPTS"></a>

#### CONFIRM\_ATTEMPTS

enumerate-and-look-for-the-bootloader-bit rounds after 0x84

<a id="mvconnect.flasher.FlashError"></a>

## FlashError

```python
class FlashError(Exception)
```

The flash cannot proceed.

The cause is a refused or unanswered frame, a CRC mismatch, or a file without a usable range.

<a id="mvconnect.flasher.Rejected"></a>

## Rejected

```python
class Rejected(FlashError)
```

The device answered NAK (kind 0x2d).

<a id="mvconnect.flasher.NotEntered"></a>

## NotEntered

```python
class NotEntered(FlashError)
```

The device did not answer the first step of the flash-mode handshake, so it is not in flash mode.

<a id="mvconnect.flasher.crc16_ccitt"></a>

#### crc16\_ccitt

```python
def crc16_ccitt(data: bytes, crc: int = 0xFFFF) -> int
```

Return the CRC-16/CCITT of *data*, continued from *crc*.

The CRC has polynomial 0x1021, init 0xFFFF, MSB first, no reflection. The device computes this over the
bytes written to a bank and returns it at bank-close; the host computes the same and compares.

<a id="mvconnect.flasher.FlashPlan"></a>

## FlashPlan

```python
@dataclass
class FlashPlan()
```

The erase blocks a flash is split into, from the file's meta range.

It holds the range's *start* and *end* (exclusive), the *erase_size*, and the *blocks* as (flash address,
length) pairs.

<a id="mvconnect.flasher.FlashPlan.start"></a>

#### start: `int`

<a id="mvconnect.flasher.FlashPlan.end"></a>

#### end: `int`

<a id="mvconnect.flasher.FlashPlan.erase_size"></a>

#### erase\_size: `int`

<a id="mvconnect.flasher.FlashPlan.blocks"></a>

#### blocks: `list[tuple[int, int]]`

<a id="mvconnect.flasher.FlashPlan.total_bytes"></a>

#### total\_bytes

```python
@property
def total_bytes() -> int
```

Return the bytes in all blocks.

<a id="mvconnect.flasher.FlashPlan.total_frames"></a>

#### total\_frames

```python
@property
def total_frames() -> int
```

Return the data frames the blocks take, at MAX_DATA bytes each.

<a id="mvconnect.flasher.plan"></a>

#### plan

```python
def plan(firmware: Firmware, erase_size: int) -> FlashPlan
```

Return the FlashPlan splitting the flash range of *firmware* into erase blocks of *erase_size* bytes.

Raise FlashError if the range or the size is unusable: a range that is empty or does not start in the
file's own data, a size that is not positive, or a Microchip image, whose addresses are not understood
well enough to plan a flash.

The range is [meta.start, meta.end): the end may be meant as the last address written, and for a format-1
file the sector at meta.end may be meant to be erased and filled too. Until a capture shows either, the
plan stops one byte short and touches no sector it cannot show is meant to be written. A block is clipped
at a bank boundary, bank + 0x3ffff.

<a id="mvconnect.flasher.enter_flash_mode"></a>

#### enter\_flash\_mode

```python
def enter_flash_mode(bus: MasterBus, address: Address) -> None
```

Send Loader.ARM (0x83 + META_START) then Loader.ENTER (0x84 + META_END) to the device at *address*.

The device arms on the first and enters flash mode only on the second, in that order; both carry the 14
meta-marker bytes split across them. Raise NotEntered if the first goes unanswered, and FlashError if the
second does, after which the device may be in flash mode.

<a id="mvconnect.flasher.in_flash_mode"></a>

#### in\_flash\_mode

```python
def in_flash_mode(bus: MasterBus, address: Address) -> bool
```

Tell whether the device at *address* already announces the bootloader bit, with one enumerate.

False also when it does not announce itself or its announcement is malformed. A device in flash mode
(after an abort, or a power cut mid-update) still answers ENUMERATE and 0x80/0x81/0x82, but is expected
to ignore 0x83/0x84 without any reply, so the handshake has to be skipped for it.

<a id="mvconnect.flasher.confirm_flash_mode"></a>

#### confirm\_flash\_mode

```python
def confirm_flash_mode(bus: MasterBus, address: Address) -> None
```

Enumerate until the device at *address* announces itself with the bootloader bit set.

Raise FlashError if it does not within CONFIRM_ATTEMPTS rounds, or if an announcement is malformed.

Nothing is programmed until this announcement with the bootloader bit (info & 0x20) is seen. The device
is expected to re-announce after 0x84 on its own; enumerating makes sure the announcement is heard.

<a id="mvconnect.flasher.leave_flash_mode"></a>

#### leave\_flash\_mode

```python
def leave_flash_mode(bus: MasterBus, address: Address, processor: int,
                     version: int) -> None
```

Send Loader.LEAVE (0x85) to the device at *address* and make sure it left flash mode.

The device honours 0x85 once a 'done' has armed it; its next announcement must come without the
bootloader bit. The frame carries *processor* and a 32-bit software version at data[2..5]: *version*, the
file's. It is sent once, never again into an application that has started. Raise TimeoutError if neither
an answer nor an announcement comes, and FlashError if the device announces itself still in flash mode.
An answer without an announcement after it is only logged as a warning.

<a id="mvconnect.flasher.flash"></a>

#### flash

```python
def flash(bus: MasterBus,
          device: Device,
          firmware: Firmware,
          *,
          erase_size: int,
          fill_value: int,
          processor: int = 0,
          progress: Progress | None = None) -> FlashPlan
```

Write *firmware* to *device* over *bus* and return the FlashPlan used.

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

<a id="mvconnect.flasher.rehearse_flash"></a>

#### rehearse\_flash

```python
def rehearse_flash(bus: MasterBus,
                   device: Device,
                   firmware: Firmware,
                   *,
                   erase_size: int,
                   fill_value: int,
                   processor: int = 0) -> FlashPlan
```

Enter and leave flash mode on *device* for real, but only log the programming of *firmware*.

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

