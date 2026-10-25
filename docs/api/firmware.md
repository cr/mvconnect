<a id="mvconnect.firmware"></a>

# mvconnect.firmware

Firmware files and the bootloader's identification queries.

Mastervolt ships firmware as Intel HEX. Somewhere in the image sits a
meta section, between two markers of 7 bytes each. ARM images hold it as plain bytes;
Microchip PIC24/dsPIC images, whose HEX files store 24-bit words in
32-bit cells, hold it two bytes per cell in the low half:

    4c 70 f0 7c 0f c0 3f   <record>   63 c1 f8 0f f0 07 fe

In the record, as verified against CHM1_V8_34.hex and the CHG it runs on:

    +0   u16  1 (format or processor number; meaning not confirmed)
    +2   u32  product id: the product number as a number (44010500)
    +6   u16  hardware version? (0x0100 on the CHG file)
    +8   u8   0
    +9   u16  firmware version, major in the high byte (0x0822 = 8.34)
    +11  u16  0
    +13  u32  image start address (0x08003000)
    +17  u32  image end address (0x08020000)
    +21  u16  0x0880 (meaning not confirmed)
    +23  ...  the rest is not decoded yet (likely a CRC)

A format-2 record (+0 is 2, 39 bytes) moves the image start two bytes up, to +11, as its files show: a
start inside the file's own range there, where +13 gives a number such as 0x801. Its end stays at +17.

Devices answer three bootloader queries, as plain requests (kind 0x0e):

    80            -> 80 u32 product id
    81 proc       -> 81 proc u16 hardware version
    82 proc       -> 82 proc u32 software version

<a id="mvconnect.firmware.META_START"></a>

#### META\_START

<a id="mvconnect.firmware.META_END"></a>

#### META\_END

<a id="mvconnect.firmware.MAX_SPAN"></a>

#### MAX\_SPAN

the widest span of addresses an image may cover; Mastervolt's widest is 44 MiB

<a id="mvconnect.firmware.Loader"></a>

## Loader

```python
class Loader(enum.IntEnum)
```

The bootloader's requests (kind 0x0e), by their first data byte.

The first three only ask; the others take the device into flash mode and out of it (see
mvconnect.flasher).

<a id="mvconnect.firmware.Loader.PRODUCT_ID"></a>

#### PRODUCT\_ID

-> 80 u32 product id

<a id="mvconnect.firmware.Loader.HARDWARE"></a>

#### HARDWARE

+ processor -> 81 proc u16 hardware version

<a id="mvconnect.firmware.Loader.SOFTWARE"></a>

#### SOFTWARE

+ processor -> 82 proc u32 software version

<a id="mvconnect.firmware.Loader.ARM"></a>

#### ARM

+ META_START: the first half of the way into flash mode

<a id="mvconnect.firmware.Loader.ENTER"></a>

#### ENTER

+ META_END: the second half, once armed

<a id="mvconnect.firmware.Loader.LEAVE"></a>

#### LEAVE

+ processor + software version (LE): out of flash mode, once a 'done' armed it

<a id="mvconnect.firmware.FirmwareError"></a>

## FirmwareError

```python
class FirmwareError(Exception)
```

A firmware file cannot be read, or has no meta section this module understands.

<a id="mvconnect.firmware.parse_intel_hex"></a>

#### parse\_intel\_hex

```python
def parse_intel_hex(text: str) -> dict[int, int]
```

Return a dict from address to byte for every data record of the Intel HEX *text*.

Record types 1, 2 and 4 are honoured, others ignored. Raise FirmwareError as parse_intel_hex_image() does.

<a id="mvconnect.firmware.parse_intel_hex_image"></a>

#### parse\_intel\_hex\_image

```python
def parse_intel_hex_image(text: str) -> tuple[int, int, bytes]
```

Return (lowest address, highest address, image bytes) for the Intel HEX *text*.

The image has 0xFF where the file defines nothing. Raise FirmwareError on a malformed line, a file without
data records, or records that span more than MAX_SPAN bytes of addresses.

<a id="mvconnect.firmware.Meta"></a>

## Meta

```python
@dataclass
class Meta()
```

The meta record of a firmware file.

It holds the record's *product_id*; the firmware *version*; the image's *start* and *end* addresses; the
*hardware* version; the *offset* of the marker in the image; the record's *raw* bytes; its *format* word,
1 for a 27-byte record and 2 for 39 bytes; and its *packing*, 'plain' bytes or 'low16', two bytes per
32-bit cell as Microchip PIC24/dsPIC images hold it.

<a id="mvconnect.firmware.Meta.product_id"></a>

#### product\_id: `int`

<a id="mvconnect.firmware.Meta.version"></a>

#### version: `int`

<a id="mvconnect.firmware.Meta.start"></a>

#### start: `int`

<a id="mvconnect.firmware.Meta.end"></a>

#### end: `int`

<a id="mvconnect.firmware.Meta.hardware"></a>

#### hardware: `int`

<a id="mvconnect.firmware.Meta.offset"></a>

#### offset: `int`

<a id="mvconnect.firmware.Meta.raw"></a>

#### raw: `bytes`

<a id="mvconnect.firmware.Meta.format"></a>

#### format: `int`

<a id="mvconnect.firmware.Meta.packing"></a>

#### packing: `str`

<a id="mvconnect.firmware.Meta.product"></a>

#### product

```python
@property
def product() -> str
```

Return the product number as text, such as '44010500'.

<a id="mvconnect.firmware.Meta.version_text"></a>

#### version\_text

```python
@property
def version_text() -> str
```

Return the version as text, or '?' for a record without one.

<a id="mvconnect.firmware.find_meta"></a>

#### find\_meta

```python
def find_meta(img: bytes, base: int) -> Meta | None
```

Return the Meta record in *img*, an image starting at address *base*, or None when there is none.

The record is looked for in plain bytes (ARM parts) and packed two bytes per 32-bit cell (Microchip parts,
whose HEX files hold 24-bit words in 32-bit cells with the data in the low half); None is returned when
neither packing shows the marker. Raise FirmwareError if the marker starts a record without an end or too
short to read.

<a id="mvconnect.firmware.Firmware"></a>

## Firmware

```python
@dataclass
class Firmware()
```

A firmware file.

It holds the file's *path*, the *image* bytes from *base* upwards with 0xFF where the file defines
nothing, and its *meta* record.

<a id="mvconnect.firmware.Firmware.path"></a>

#### path: `Path`

<a id="mvconnect.firmware.Firmware.image"></a>

#### image: `bytes`

<a id="mvconnect.firmware.Firmware.meta"></a>

#### meta: `Meta`

<a id="mvconnect.firmware.Firmware.base"></a>

#### base: `int`

<a id="mvconnect.firmware.Firmware.load"></a>

#### load

```python
@classmethod
def load(cls, path: str | Path) -> Firmware
```

Return the Firmware read from the Intel HEX file at *path*.

Raise FirmwareError if the file is malformed or has no meta section marker, and OSError if it cannot
be read.

<a id="mvconnect.firmware.Firmware.size"></a>

#### size

```python
@property
def size() -> int
```

Return the image's length in bytes.

<a id="mvconnect.firmware.LibraryEntry"></a>

## LibraryEntry

```python
@dataclass
class LibraryEntry()
```

A file in the library as the index remembers it.

It holds the file's *path*, its *meta* record (None when it has no readable meta section) and the *error*
that says why.

<a id="mvconnect.firmware.LibraryEntry.path"></a>

#### path: `Path`

<a id="mvconnect.firmware.LibraryEntry.meta"></a>

#### meta: `Meta | None`

<a id="mvconnect.firmware.LibraryEntry.error"></a>

#### error: `str`

<a id="mvconnect.firmware.INDEX_FORMAT"></a>

#### INDEX\_FORMAT

of index.json: raised whenever decoding a file changes what it gives

<a id="mvconnect.firmware.library"></a>

#### library

```python
def library(directory: str | Path,
            with_errors: bool = False) -> list[LibraryEntry]
```

Return the firmware files of *directory*, newest version first per product.

Decoding is cached in index.json next to the files, keyed by size and mtime, since decoding 80 HEX files
takes seconds; an index of another INDEX_FORMAT, written before the decoding changed, is not used. With
*with_errors* the files without a readable meta section come too, as LibraryEntry with *meta* None;
otherwise only the decoded files.

<a id="mvconnect.firmware.Processor"></a>

## Processor

```python
@dataclass
class Processor()
```

One processor of a device as the bootloader reports it.

It holds the processor's *number*, and its *hardware* and *software* versions, None where unanswered.

<a id="mvconnect.firmware.Processor.number"></a>

#### number: `int`

<a id="mvconnect.firmware.Processor.hardware"></a>

#### hardware: `int | None`

<a id="mvconnect.firmware.Processor.software"></a>

#### software: `int | None`

<a id="mvconnect.firmware.Identification"></a>

## Identification

```python
@dataclass
class Identification()
```

What the bootloader's queries say about a device.

It holds the device's *product_id*, None when unanswered, and its *processors*.

<a id="mvconnect.firmware.Identification.product_id"></a>

#### product\_id: `int | None`

<a id="mvconnect.firmware.Identification.processors"></a>

#### processors: `list[Processor]`

<a id="mvconnect.firmware.identify"></a>

#### identify

```python
def identify(bus: MasterBus,
             address: Address,
             max_processors: int = 8) -> Identification
```

Return the Identification of the device at *address* on *bus*.

It holds the device's product id and the hardware and software version of each of up to *max_processors*
processors; the processors end at the first that does not answer. Read-only; a device that does not
answer the product id query gives an empty one.

