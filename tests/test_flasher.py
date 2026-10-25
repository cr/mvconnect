"""The bootloader transfer protocol (mvconnect.flasher).

The FakeBootloader models the device side of the protocol as mvconnect.flasher
describes it: the 0x83/0x84 mode handshake in the right order, the kind-0x3d
sub-commands, per-bank erase, the byte-count check and the CRC-16 at
bank-close. A flasher that drives it to a clean finish speaks that protocol;
whether a device does is for a capture to show. Nothing here touches real
hardware; the CLI keeps the write path disabled.
"""

from pathlib import Path

import pytest
from expect import logged, mentions

from mvconnect import flasher
from mvconnect.firmware import META_END, META_START, Firmware, Meta
from mvconnect.flasher import (
    ACK_KIND,
    FLASH_KIND,
    NAK_KIND,
    SEL_BANK_CLOSE,
    SEL_BANK_SELECT,
    SEL_DATA,
    SEL_DONE,
    SEL_PROCESSOR,
    FlashError,
    crc16_ccitt,
    flash,
    plan,
    rehearse_flash,
)
from mvconnect.frame import KIND_ANNOUNCE, KIND_ENUMERATE, KIND_NA, KIND_REPLY, KIND_REQUEST, Address, Frame
from mvconnect.masterbus import Device, MasterBus


class FakeBootloader:
    """The device side of the flash protocol, as a fake link."""

    def __init__(self, product_id=44010500, hw=(0x0100,), sw=(0x0822,), corrupt_crc=False, reject=(),
                 address=Address(0x0A, 0x1062B), info=bytes.fromhex("0c030000"), enter_fails=False,
                 in_mode=False, processor=None, bank=None, bank_open=False):
        self.address, self.info = address, info
        self.enter_fails = enter_fails  # answers 0x84 but never actually enters flash mode
        self.product_id = product_id
        self.hw, self.sw = list(hw), list(sw)
        self.corrupt_crc = corrupt_crc
        self.reject = set(reject)  # selectors to answer with NAK
        self.listener = None
        # the bootloader's state; preset to model a device left in flash mode by an earlier run
        self.in_mode, self.armed, self.stop_armed = in_mode, False, False
        self.processor = processor
        self.bank = bank
        self.bank_open = bank_open
        self.start_offset = 0
        self.written = 0
        self.staged = {}  # cleared on every bank-open, as the device does; used for the CRC
        self.flashed = {}  # everything ever written, kept for assertions
        self.received = []  # every request frame, for wire-format assertions

    # link interface
    def open(self, listener):
        self.listener = listener
        return self

    def close(self):
        pass

    def send(self, *frames, timeout=1.0):
        for f in frames:
            self.received.append(f)
            if f.kind == KIND_REQUEST:
                self._handshake(f)
            elif f.kind == FLASH_KIND:
                self._program(f)
            elif f.kind == KIND_ENUMERATE:
                self._announce()

    def _announce(self):
        """The announcement; in flash mode the bootloader bit (0x20) is set in the third info byte."""
        info = bytearray(self.info)
        if self.in_mode:
            info[2] |= 0x20
        self._reply(KIND_ANNOUNCE, self.address,
                    bytes([self.address.hi]) + self.address.lo.to_bytes(3, "little") + bytes(info))

    # replies
    def _reply(self, kind, address, data):
        self.listener(Frame(kind, address, bytes(data)))

    def _ack(self, sel, low, payload):
        self._reply(ACK_KIND, Address(sel, low), payload)

    def _nak(self, sel, low, payload=b"\0\0\0\0"):
        self._reply(NAK_KIND, Address(sel, low), payload)

    def _handshake(self, f):
        a, d = f.address, f.data
        if d[0] == 0x80:
            self._reply(KIND_REPLY, a, bytes([0x80]) + self.product_id.to_bytes(4, "little"))
        elif d[0] == 0x81:
            proc = d[1]
            if proc < len(self.hw):
                self._reply(KIND_REPLY, a, bytes([0x81, proc]) + self.hw[proc].to_bytes(2, "little"))
            else:
                self._reply(KIND_NA, a, d[:2])
        elif d[0] == 0x82:
            proc = d[1]
            if proc < len(self.sw):
                self._reply(KIND_REPLY, a, bytes([0x82, proc]) + self.sw[proc].to_bytes(4, "little"))
            else:
                self._reply(KIND_NA, a, d[:2])
        elif d[0] == 0x83:  # arm, only with the exact key and only when not already in mode
            if len(d) == 8 and d[1:8] == META_START and not self.in_mode:
                self.armed = True
                self._reply(KIND_REPLY, a, bytes([0x83]))
        elif d[0] == 0x84:  # enter flash mode, only if 0x83 armed it; silently ignored when already in mode
            if self.in_mode:
                return
            if len(d) == 8 and d[1:8] == META_END:
                if self.armed and not self.enter_fails:
                    self.in_mode, self.armed, self.stop_armed = True, False, False
                    self.bank_open = False
                self._reply(KIND_REPLY, a, bytes([0x84]))
        elif d[0] == 0x85:  # leave flash mode once a done armed it
            proc = d[1]
            if proc < max(len(self.sw), 1):
                if self.stop_armed:
                    self.in_mode = False
                self._reply(KIND_REPLY, a, bytes([0x85]))

    def _program(self, f):
        if not self.in_mode:
            return  # a device not in flash mode ignores 0x3d entirely
        sel, low, d = f.address.hi, f.address.lo, f.data
        if sel in self.reject:
            self._nak(sel, low)
            return
        if sel == SEL_PROCESSOR:
            if self.processor in (None, low & 0xFF):
                self.processor = low & 0xFF
                self._ack(sel, low, b"\0\0\0\0")
            else:
                self._nak(sel, low, b"\0" * 8)
        elif sel == SEL_BANK_SELECT:
            if self.processor is not None and not self.bank_open and len(d) == 3:
                self.bank = low
                self.start_offset = int.from_bytes(d[:3], "little")
                self.bank_open, self.written = True, 0
                self.staged = {}  # the device erases its staged image on every bank-open
                self._ack(sel, low, bytes([low & 0xFF, (low >> 8) & 0xFF, (low >> 16) & 0xFF]) + d[:3] + b"\0")
            else:
                self._nak(sel, low, b"\0" * 8)
        elif sel == SEL_DATA:
            if self.bank_open:
                base = self.bank << 18
                for i, b in enumerate(d):
                    self.staged[base + low + i] = b
                    self.flashed[base + low + i] = b
                self.written += len(d)
                self._ack(sel, low, b"\0\0\0\0")
            else:
                self._nak(sel, low)
        elif sel == SEL_BANK_CLOSE:
            expected = int.from_bytes(d[:3], "little") if len(d) >= 3 else -1
            # count 0 is always accepted (the force-close), and the bank closes on ACK and NAK alike
            ok = expected == 0 or (self.bank_open and self.bank == low and self.written == expected)
            crc = self._bank_crc() if self.bank is not None else 0xFFFF
            if self.corrupt_crc:
                crc ^= 0xFFFF
            payload = bytes([low & 0xFF, (low >> 8) & 0xFF, (low >> 16) & 0xFF, 0, crc & 0xFF, (crc >> 8) & 0xFF])
            self.bank_open = False
            (self._ack if ok else self._nak)(sel, low, payload)
        elif sel == SEL_DONE:
            self.stop_armed = True
            self._ack(sel, low, b"\0\0\0\0")
        else:
            self._nak(sel, low)

    def _bank_crc(self):
        base = (self.bank << 18) + self.start_offset
        data = bytes(self.staged.get(base + k, 0xFF) for k in range(self.written))
        return crc16_ccitt(data)


def make_firmware(image, start=0x08003000, end=None, version=0x0822, product_id=44010500):
    end = start + len(image) if end is None else end
    meta = Meta(product_id=product_id, version=version, start=start, end=end, hardware=0x0100, offset=0)
    return Firmware(path=Path("CHM1_V8_34.hex"), image=bytes(image), meta=meta, base=start)


def run(boot):
    return MasterBus(boot, reply_timeout=0.1, retries=0).open()


def test_crc16_ccitt_check_value():
    # the standard CRC-16/CCITT-FALSE check value for "123456789"
    assert crc16_ccitt(b"123456789") == 0x29B1
    assert crc16_ccitt(b"") == 0xFFFF


def test_plan_blocks():
    fw = make_firmware(bytes(0x40), end=0x08003040)
    p = plan(fw, erase_size=0x20)
    assert p.blocks == [(0x08003000, 0x20), (0x08003020, 0x20)]
    assert p.total_bytes == 0x40 and p.total_frames == 8  # 4 frames of 8 bytes per block
    # a trailing short block when the range is not a multiple of the erase size
    assert plan(make_firmware(bytes(0x30), end=0x08003030), 0x20).blocks == [(0x08003000, 0x20), (0x08003020, 0x10)]
    with pytest.raises(FlashError, match=mentions("range")):
        plan(make_firmware(bytes(0x10), start=0, end=0), 0x20)


def test_flash_end_to_end():
    image = bytes((i * 7) & 0xFF for i in range(0x40))
    fw = make_firmware(image, end=0x08003040)
    boot = FakeBootloader()
    steps = []
    p = flash(run(boot), Device(Address(0x0A, 0x1062B), product="44010500", version="8.34"),
              fw, erase_size=0x20, fill_value=0xFF, progress=lambda d, t: steps.append((d, t)))
    # the whole image reached the device, byte for byte
    assert {a - 0x08003000: b for a, b in boot.flashed.items()} == dict(enumerate(image))
    assert not boot.in_mode and boot.stop_armed  # done armed the stop, 0x85 left the mode
    assert steps[-1] == (0x40, 0x40) and p.total_frames == 8
    # the handshake went out in the right order with the split marker key
    reqs = [f for f in boot.received if f.kind == KIND_REQUEST]
    assert reqs[0].data == bytes([0x83]) + META_START
    assert reqs[1].data == bytes([0x84]) + META_END
    assert reqs[-1].data[0] == 0x85
    # one enumerate to see whether the device is already in flash mode, the handshake, then the
    # confirmation (enumerate -> announce with the bootloader bit) before any 0x3d frame
    kinds = [f.kind for f in boot.received]
    assert kinds[:4] == [KIND_ENUMERATE, KIND_REQUEST, KIND_REQUEST, KIND_ENUMERATE] and kinds[4] == FLASH_KIND
    # the programming selectors, in order: proc, then (bank, 4x data, close) per block, then done
    sels = [f.address.hi for f in boot.received if f.kind == FLASH_KIND]
    assert sels == [SEL_PROCESSOR] + 2 * ([SEL_BANK_SELECT] + 4 * [SEL_DATA] + [SEL_BANK_CLOSE]) + [SEL_DONE]


def test_flash_fills_undefined_addresses():
    image = bytes(range(0x30))  # file defines 0x30 bytes; range runs to 0x40
    fw = make_firmware(image, end=0x08003040)
    boot = FakeBootloader()
    flash(run(boot), Device(Address(0x0A, 0x1062B)), fw, erase_size=0x40, fill_value=0xAB)
    assert boot.flashed[0x08003030] == 0xAB and boot.flashed[0x0800303F] == 0xAB
    assert boot.flashed[0x0800302F] == 0x2F  # last defined byte unchanged


def test_flash_aborts_on_crc_mismatch():
    fw = make_firmware(bytes(0x20), end=0x08003020)
    boot = FakeBootloader(corrupt_crc=True)
    with pytest.raises(FlashError, match=mentions("CRC")):
        flash(run(boot), Device(Address(0x0A, 0x1062B)), fw, erase_size=0x20, fill_value=0xFF)
    # the abort tried to leave, but no "done" had armed the stop, so the device stays in flash
    # mode: exactly the recovery gap that keeps the write path disabled pending a capture
    assert boot.in_mode


def test_flash_aborts_on_device_nak():
    fw = make_firmware(bytes(0x20), end=0x08003020)
    boot = FakeBootloader(reject={SEL_BANK_SELECT})
    with pytest.raises(FlashError, match=mentions("bank-select")):
        flash(run(boot), Device(Address(0x0A, 0x1062B)), fw, erase_size=0x20, fill_value=0xFF)


class Lossy(FakeBootloader):
    """Takes everything, but its answer to the first data frame is lost; and it closes a bank with a short
    reply when *short_close*, and stays silent to 0x85 when *mute_exit*."""

    def __init__(self, short_close=False, mute_exit=False, **options):
        super().__init__(**options)
        self.short_close, self.mute_exit, self.lost = short_close, mute_exit, False

    def _ack(self, sel, low, payload):
        if sel == SEL_DATA and not self.lost:
            self.lost = True
            return
        if sel == SEL_BANK_CLOSE and self.short_close:
            payload = payload[:4]
        super()._ack(sel, low, payload)

    def _handshake(self, f):
        if not (self.mute_exit and f.data[:1] == b"\x85"):
            super()._handshake(f)


def test_a_data_frame_without_its_answer_is_not_sent_again():
    fw = make_firmware(bytes(0x20), end=0x08003020)
    boot = Lossy()
    with pytest.raises(FlashError):
        flash(run(boot), Device(Address(0x0A, 0x1062B)), fw, erase_size=0x20, fill_value=0xFF)
    # the block is redone whole
    assert [f.address.hi for f in boot.received if f.kind == FLASH_KIND].count(SEL_DATA) == 1


def test_a_bank_close_without_a_crc_aborts():
    fw = make_firmware(bytes(0x20), end=0x08003020)
    boot = Lossy(short_close=True)
    boot.lost = True  # every data frame answered
    with pytest.raises(FlashError, match=mentions("CRC")):
        flash(run(boot), Device(Address(0x0A, 0x1062B)), fw, erase_size=0x20, fill_value=0xFF)


def test_an_exit_that_fails_after_an_abort_leaves_the_abort_as_the_error():
    fw = make_firmware(bytes(0x20), end=0x08003020)
    boot = Lossy(mute_exit=True, reject={SEL_BANK_SELECT})
    with pytest.raises(FlashError, match=mentions("bank-select")):  # not the exit's timeout
        flash(run(boot), Device(Address(0x0A, 0x1062B)), fw, erase_size=0x20, fill_value=0xFF)


def test_flash_rejects_bad_fill_value():
    fw = make_firmware(bytes(0x10), end=0x08003010)
    with pytest.raises(FlashError, match=mentions("fill")):
        flash(run(FakeBootloader()), Device(Address(0x0A, 0x1062B)), fw, erase_size=0x10, fill_value=256)


def test_flash_aborts_if_device_never_enters_flash_mode():
    fw = make_firmware(bytes(0x20), end=0x08003020)
    boot = FakeBootloader(enter_fails=True)
    with pytest.raises(FlashError, match=mentions("flash mode")):
        flash(run(boot), Device(Address(0x0A, 0x1062B)), fw, erase_size=0x20, fill_value=0xFF)
    assert not any(f.kind == FLASH_KIND for f in boot.received)  # nothing was programmed


def test_a_second_ctrl_c_during_the_rehearsal_s_exit_waits_until_the_device_is_out(monkeypatch, caplog):
    import signal

    class Impatient(FakeBootloader):  # Ctrl-C again as the exit's 'done' goes out
        def _program(self, f):
            if f.address.hi == flasher.SEL_DONE:
                signal.raise_signal(signal.SIGINT)
            super()._program(f)

    def first_ctrl_c(bus, address):
        raise KeyboardInterrupt
    monkeypatch.setattr(flasher, "confirm_flash_mode", first_ctrl_c)
    fw = make_firmware(bytes(0x20), end=0x08003020)
    boot = Impatient()
    with pytest.raises(KeyboardInterrupt):
        flasher.rehearse_flash(run(boot), Device(Address(0x0A, 0x1062B)), fw, erase_size=0x20, fill_value=0xFF)
    assert [f.data[0] for f in boot.received if f.kind == KIND_REQUEST][-1] == 0x85 and not boot.in_mode


def test_flash_device_already_in_flash_mode_skips_handshake():
    image = bytes(range(0x20))
    fw = make_firmware(image, end=0x08003020)
    boot = FakeBootloader(in_mode=True)  # e.g. after a power cut mid-update: it ignores 0x83/0x84
    flash(run(boot), Device(Address(0x0A, 0x1062B)), fw, erase_size=0x20, fill_value=0xFF)
    cmds = [f.data[0] for f in boot.received if f.kind == KIND_REQUEST]
    assert 0x83 not in cmds and 0x84 not in cmds and cmds == [0x85]
    assert {a - 0x08003000: b for a, b in boot.flashed.items()} == dict(enumerate(image)) and not boot.in_mode


def test_flash_resumes_after_abort_left_a_bank_open():
    image = bytes(range(0x20))
    fw = make_firmware(image, end=0x08003020)
    # an earlier run selected the processor, opened bank 0x200 and died mid-block
    boot = FakeBootloader(in_mode=True, processor=0, bank=0x200, bank_open=True)
    boot.written, boot.start_offset = 5, 0x3000
    flash(run(boot), Device(Address(0x0A, 0x1062B)), fw, erase_size=0x20, fill_value=0xFF)
    sels = [f.address.hi for f in boot.received if f.kind == FLASH_KIND]
    # proc-select, bank-select (refused), force-close, bank-select (accepted), data..., close, done
    assert sels[:5] == [SEL_PROCESSOR, SEL_BANK_SELECT, SEL_BANK_CLOSE, SEL_BANK_SELECT, SEL_DATA]
    first_close = next(f for f in boot.received if f.kind == FLASH_KIND and f.address.hi == SEL_BANK_CLOSE)
    assert first_close.data == b"\0\0\0"  # the force-close carries count 0
    assert {a - 0x08003000: b for a, b in boot.flashed.items()} == dict(enumerate(image)) and not boot.in_mode


def test_rehearse_enters_and_leaves_but_programs_nothing():
    image = bytes(range(0x40))
    fw = make_firmware(image, end=0x08003040)
    boot = FakeBootloader()
    rehearse_flash(run(boot), Device(Address(0x0A, 0x1062B)), fw, erase_size=0x20, fill_value=0xFF)
    # the handshake and the exit went out for real, in order
    cmds = [f.data[0] for f in boot.received if f.kind == KIND_REQUEST]
    assert cmds == [0x83, 0x84, 0x85]
    # the only 0x3d frame sent was the harmless 'done'; no programming frames touched the bus
    prog = [f.address.hi for f in boot.received if f.kind == FLASH_KIND]
    assert prog == [SEL_DONE]
    assert not any(s in prog for s in (SEL_PROCESSOR, SEL_BANK_SELECT, SEL_DATA, SEL_BANK_CLOSE))
    # nothing was erased or written, and the device left flash mode
    assert boot.flashed == {} and boot.staged == {} and not boot.in_mode


def test_flash_stopped_by_ctrl_c_still_tries_to_leave_flash_mode(monkeypatch, caplog):
    import logging

    import mvconnect.flasher as fl
    caplog.set_level(logging.INFO, logger="mvconnect.flasher")

    def interrupted(*args):
        raise KeyboardInterrupt
    monkeypatch.setattr(fl, "_data", interrupted)  # Ctrl-C during the first data frame
    boot = FakeBootloader()
    with pytest.raises(KeyboardInterrupt):
        flash(run(boot), Device(Address(0x0A, 0x1062B)), make_firmware(bytes(0x20), end=0x08003020),
              erase_size=0x20, fill_value=0xFF)
    assert 0x85 in [f.data[0] for f in boot.received if f.kind == KIND_REQUEST]  # the exit was attempted
    assert logged(caplog, "WARNING", "Ctrl-C", "out of flash mode")


class LosesTheEntryReply(FakeBootloader):
    """Enters flash mode on 0x84, but its reply never arrives; a 0x84 sent again is ignored, as in flash mode."""

    def _reply(self, kind, address, data):
        if kind == KIND_REPLY and data[:1] == bytes([0x84]):
            return
        super()._reply(kind, address, data)


def test_a_handshake_whose_last_reply_is_lost_still_ends_in_the_exit(monkeypatch):
    import mvconnect.flasher as fl
    monkeypatch.setattr(fl, "CONTROL_TIMEOUT", 0.02)
    fw = make_firmware(bytes(0x20), end=0x08003020)
    boot = LosesTheEntryReply()
    with pytest.raises(FlashError):
        rehearse_flash(MasterBus(boot, reply_timeout=0.02, retries=0).open(), Device(Address(0x0A, 0x1062B)), fw,
                       erase_size=0x20, fill_value=0xFF)
    assert not boot.in_mode  # the rehearsal's done and 0x85 took it out again
    boot = LosesTheEntryReply()
    with pytest.raises(FlashError):
        flash(MasterBus(boot, reply_timeout=0.02, retries=0).open(), Device(Address(0x0A, 0x1062B)), fw,
              erase_size=0x20, fill_value=0xFF)
    assert 0x85 in [f.data[0] for f in boot.received if f.kind == KIND_REQUEST]  # the exit was attempted


def test_an_exit_that_fails_is_an_error_and_keeps_what_stopped_the_rehearsal(monkeypatch, caplog):
    import logging

    import mvconnect.flasher as fl
    from mvconnect.hidlink import LinkError
    fw = make_firmware(bytes(0x20), end=0x08003020)

    class Stuck(FakeBootloader):  # never answers 0x85
        def _handshake(self, f):
            if f.data[:1] != bytes([0x85]):
                super()._handshake(f)
    with pytest.raises(FlashError, match=mentions("did not leave")):
        rehearse_flash(MasterBus(Stuck(), reply_timeout=0.02, retries=0).open(), Device(Address(0x0A, 0x1062B)), fw,
                       erase_size=0x20, fill_value=0xFF)
    with pytest.raises(FlashError, match=mentions("did not leave")):
        flash(MasterBus(Stuck(), reply_timeout=0.02, retries=0).open(), Device(Address(0x0A, 0x1062B)), fw,
              erase_size=0x20, fill_value=0xFF)

    def unplugged(*args):
        raise LinkError("link is gone")
    monkeypatch.setattr(fl, "_done", unplugged)
    monkeypatch.setattr(fl, "confirm_flash_mode", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt))
    with caplog.at_level(logging.ERROR), pytest.raises(KeyboardInterrupt):  # Ctrl-C stays Ctrl-C
        rehearse_flash(run(FakeBootloader()), Device(Address(0x0A, 0x1062B)), fw, erase_size=0x20, fill_value=0xFF)
    assert logged(caplog, "ERROR", "flash mode")  # and the warning is not lost


def test_a_device_in_flash_mode_already_is_not_rehearsed():
    boot = FakeBootloader(in_mode=True)  # left so by an earlier, aborted flash
    with pytest.raises(FlashError, match=mentions("flash mode")):
        rehearse_flash(run(boot), Device(Address(0x0A, 0x1062B)), make_firmware(bytes(0x20), end=0x08003020),
                       erase_size=0x20, fill_value=0xFF)
    assert boot.in_mode and not [f for f in boot.received if f.kind == KIND_REQUEST]


def test_a_device_whose_announcement_is_missed_is_not_rehearsed(monkeypatch):
    monkeypatch.setattr(flasher, "CONTROL_TIMEOUT", 0.02)

    class Unheard(FakeBootloader):  # in flash mode, and its announcements are not heard
        def _announce(self):
            pass
    boot = Unheard(in_mode=True)
    with pytest.raises(FlashError):
        rehearse_flash(run(boot), Device(Address(0x0A, 0x1062B)), make_firmware(bytes(0x20), end=0x08003020),
                       erase_size=0x20, fill_value=0xFF)
    assert boot.in_mode and not [f for f in boot.received if f.kind in (KIND_REQUEST, FLASH_KIND)]


def test_a_device_that_does_not_arm_gets_no_exit(monkeypatch):
    class Deaf(FakeBootloader):  # does not answer the handshake at all
        def _handshake(self, f):
            if f.data[:1] not in (b"\x83", b"\x84", b"\x85"):
                super()._handshake(f)
    boot = Deaf()
    with pytest.raises(FlashError):
        rehearse_flash(MasterBus(boot, reply_timeout=0.02, retries=0).open(), Device(Address(0x0A, 0x1062B)),
                       make_firmware(bytes(0x20), end=0x08003020), erase_size=0x20, fill_value=0xFF)
    assert not [f for f in boot.received if f.kind == FLASH_KIND or f.data[:1] == b"\x85"]  # no done to take


def test_0x85_is_sent_once_and_the_exit_is_seen_in_the_announcement():
    class Restarting(FakeBootloader):  # leaves flash mode on 0x85, but restarts before it answers
        def _reply(self, kind, address, data):
            if not (kind == KIND_REPLY and data[:1] == b"\x85"):
                super()._reply(kind, address, data)
    boot = Restarting()
    rehearse_flash(MasterBus(boot, reply_timeout=0.02, retries=2).open(), Device(Address(0x0A, 0x1062B)),
                   make_firmware(bytes(0x20), end=0x08003020), erase_size=0x20, fill_value=0xFF)
    assert [f.data[0] for f in boot.received if f.kind == KIND_REQUEST].count(0x85) == 1 and not boot.in_mode


def test_rehearse_leaves_flash_mode_even_if_confirm_fails(monkeypatch):
    import mvconnect.flasher as fl
    monkeypatch.setattr(fl, "CONFIRM_ATTEMPTS", 2)
    monkeypatch.setattr(fl, "CONTROL_TIMEOUT", 0.02)
    # a device that accepts 0x84 but never announces the bootloader bit, so confirm_flash_mode fails

    class Quiet(FakeBootloader):
        def _announce(self):
            if not self.in_mode:
                super()._announce()
    fw = make_firmware(bytes(0x20), end=0x08003020)
    boot = Quiet()
    with pytest.raises(FlashError, match=mentions("flash mode")):
        rehearse_flash(run(boot), Device(Address(0x0A, 0x1062B)), fw, erase_size=0x20, fill_value=0xFF)
    # the finally still ran done + 0x85, so the device is out of flash mode and nothing was programmed
    assert 0x85 in [f.data[0] for f in boot.received if f.kind == KIND_REQUEST]
    assert not boot.in_mode and boot.flashed == {}


def test_plan_clips_blocks_at_bank_boundary():
    # a range straddling the 256 KiB bank boundary at 0x40000: clipped at bank + 0x3ffff
    fw = make_firmware(bytes(0x60), start=0x3FFE0, end=0x40040)
    assert plan(fw, erase_size=0x30).blocks == [(0x3FFE0, 0x20), (0x40000, 0x30), (0x40030, 0x10)]
    fw = make_firmware(bytes(0x60), start=0x3FFD0, end=0x40030)
    assert plan(fw, erase_size=0x30).blocks == [(0x3FFD0, 0x30), (0x40000, 0x30)]
