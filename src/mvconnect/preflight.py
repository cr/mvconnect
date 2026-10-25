"""What has to be true before a device may be flashed; read-only.

A Preflight gathers the device's identity from the bus, the candidate
firmware from the library, and lists every finding. Nothing here sends
anything but the bootloader's identification queries (0x80, 0x81, 0x82);
the write path is not part of this module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from mvconnect.firmware import Firmware, Identification, LibraryEntry, identify, version_text
from mvconnect.masterbus import Device, MasterBus

logger = logging.getLogger(__name__)

@dataclass
class Finding:
    """One check before a flash: whether it passed (*ok*) and what it says (*text*)."""
    ok: bool
    text: str


@dataclass
class Preflight:
    """The checks made before a device is flashed.

    It holds the *device*, the *firmware* chosen for it, the device's bootloader *identification* and the
    *findings*.
    """
    device: Device
    firmware: Firmware
    identification: Identification
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Return True if every finding passed."""
        return all(f.ok for f in self.findings)


def choose(files: list[LibraryEntry], product: str, version: str | None = None,
           file: str | None = None) -> Firmware:
    """Return the firmware to flash from the library entries *files* or the file *file*.

    That is the file *file* if given, else the library entry of *version* for *product*, else the newest the
    library has for *product*. Raise LookupError if the library has no such entry, and FirmwareError or
    OSError if the file cannot be read.
    """
    if file is not None:
        return Firmware.load(file)
    candidates = [(f.path, f.meta) for f in files if f.meta is not None and f.meta.product == product]
    if version is not None:
        for path, meta in candidates:
            if meta.version_text == str(version):
                return Firmware.load(path)
        have = ", ".join(sorted({meta.version_text for _, meta in candidates})) or "none"
        raise LookupError(f"no version {version} for product {product} in the library; available: {have}")
    if not candidates:
        raise LookupError(f"no firmware for product {product} in the library")
    return Firmware.load(candidates[0][0])  # the library is sorted newest first per product


def preflight(bus: MasterBus, device: Device, firmware: Firmware) -> Preflight:
    """Return the Preflight of flashing *firmware* to *device* on *bus*.

    Its findings say whether the file is for this product and this hardware, whether the device's bootloader
    answers and is in its application, and whether the file's range is usable. Read-only; nothing is sent but
    the bootloader's identification queries.
    """
    ident = identify(bus, device.address)
    pf = Preflight(device, firmware, ident)
    m = firmware.meta
    add = pf.findings.append

    add(Finding(m.product == device.product,
                f"file is for product {m.product}, device reports {device.product}"))
    if ident.product_id is None:
        add(Finding(False, "device does not answer the bootloader's product id query"))
    else:
        add(Finding(ident.product_id == m.product_id,
                    f"bootloader product id {ident.product_id}, file {m.product_id}"))
    if not ident.processors:
        add(Finding(False, "device does not answer the processor version queries"))
    else:
        hw = ident.processors[0].hardware
        add(Finding(hw is not None and (hw >> 8) == (m.hardware >> 8),
                    f"hardware version {version_text(hw) if hw is not None else '?'}, "
                    f"file built for {version_text(m.hardware)} (major must match)"))
        for p in ident.processors:
            add(Finding(p.software not in (None, 0),
                        f"processor {p.number}: software {version_text(p.software) if p.software else '?'}"
                        + ("" if p.software else " (0 means it is already in flash mode)")))
    add(Finding(bool(m.version), f"file version {m.version_text}, installed {device.version}"
                + (" (same version: a re-flash)" if m.version_text == device.version else "")))
    add(Finding(m.start < m.end and firmware.size > 0,
                f"image {m.start:#010x} .. {m.end:#010x}, {firmware.size} bytes, meta record format {m.format} "
                f"({m.packing})"))
    add(Finding(not device.flash_mode, "device is in normal mode (bootloader bit clear)"))
    return pf
