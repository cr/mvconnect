<a id="mvconnect.preflight"></a>

# mvconnect.preflight

What has to be true before a device may be flashed; read-only.

A Preflight gathers the device's identity from the bus, the candidate
firmware from the library, and lists every finding. Nothing here sends
anything but the bootloader's identification queries (0x80, 0x81, 0x82);
the write path is not part of this module.

<a id="mvconnect.preflight.Finding"></a>

## Finding

```python
@dataclass
class Finding()
```

One check before a flash: whether it passed (*ok*) and what it says (*text*).

<a id="mvconnect.preflight.Finding.ok"></a>

#### ok: `bool`

<a id="mvconnect.preflight.Finding.text"></a>

#### text: `str`

<a id="mvconnect.preflight.Preflight"></a>

## Preflight

```python
@dataclass
class Preflight()
```

The checks made before a device is flashed.

It holds the *device*, the *firmware* chosen for it, the device's bootloader *identification* and the
*findings*.

<a id="mvconnect.preflight.Preflight.device"></a>

#### device: `Device`

<a id="mvconnect.preflight.Preflight.firmware"></a>

#### firmware: `Firmware`

<a id="mvconnect.preflight.Preflight.identification"></a>

#### identification: `Identification`

<a id="mvconnect.preflight.Preflight.findings"></a>

#### findings: `list[Finding]`

<a id="mvconnect.preflight.Preflight.ok"></a>

#### ok

```python
@property
def ok() -> bool
```

Return True if every finding passed.

<a id="mvconnect.preflight.choose"></a>

#### choose

```python
def choose(files: list[LibraryEntry],
           product: str,
           version: str | None = None,
           file: str | None = None) -> Firmware
```

Return the firmware to flash from the library entries *files* or the file *file*.

That is the file *file* if given, else the library entry of *version* for *product*, else the newest the
library has for *product*. Raise LookupError if the library has no such entry, and FirmwareError or
OSError if the file cannot be read.

<a id="mvconnect.preflight.preflight"></a>

#### preflight

```python
def preflight(bus: MasterBus, device: Device, firmware: Firmware) -> Preflight
```

Return the Preflight of flashing *firmware* to *device* on *bus*.

Its findings say whether the file is for this product and this hardware, whether the device's bootloader
answers and is in its application, and whether the file's range is usable. Read-only; nothing is sent but
the bootloader's identification queries.

