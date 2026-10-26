<a id="mvconnect.masterbus"></a>

# mvconnect.masterbus

The MasterBus request/reply protocol, as far as it is understood.

Every device has an address (frame.Address). A request is a frame of
KIND_REQUEST addressed to the device, data[0] being the command; the
device answers with a frame of KIND_REPLY carrying its own address and
the same command byte. A broadcast KIND_ENUMERATE with no data makes
every device send a KIND_ANNOUNCE with its address and four info bytes.

A reply of kind 0x0d instead of 0x0c means "not available".

Requests by their first data byte, the Command:

    0x08 kind                -> 0x08 kind lo hi      (Command.NUMBER)
        one of the device's 16-bit numbers; see Number. Number.VERSION
        answers the firmware version as major.minor bytes.
    0x08 kind lo hi [f32]    -> reply                (Command.NUMBER)
        writes one; mvconnect writes only the login (Number.LOGIN).
    0x09 which               -> 0x09 which lo hi     (Command.IDENTITY)
        the id of one of the device's identity strings; see Identity.
    0x30 id_lo id_hi chunk   -> 0x30 id_lo id_hi chunk c c c c   (Command.STRING)
        4 characters of string `id`, from character 4*chunk; the string
        ends at a NUL or at a chunk shorter than 4 characters. The table
        is laid out differently on every device type: fetch the ids of
        the identity strings with 0x09, the rest are units and the names
        of the device's values, groups and alarms.

<a id="mvconnect.masterbus.HOST_ADDRESS"></a>

#### HOST\_ADDRESS

<a id="mvconnect.masterbus.LINK_PRODUCT"></a>

#### LINK\_PRODUCT

the product number of the MasterBus USB Link

<a id="mvconnect.masterbus.host_address"></a>

#### host\_address

```python
def host_address(serial: str, product: str = LINK_PRODUCT) -> Address
```

Return the address this computer takes on the bus through the link with the *serial* number.

The high field is 0x14. The low field is computed from the serial number, nine characters of the form
'LYWWLNNNN', and the link's *product* number the way a device computes its own, so that two links get two
addresses. Return HOST_ADDRESS if *serial* is not of that form.

<a id="mvconnect.masterbus.Command"></a>

## Command

```python
class Command(enum.IntEnum)
```

What a request (KIND_REQUEST) asks for: its first data byte.

<a id="mvconnect.masterbus.Command.NUMBER"></a>

#### NUMBER

one of the device's 16-bit numbers, of a kind of Number; with a value behind it, a write

<a id="mvconnect.masterbus.Command.IDENTITY"></a>

#### IDENTITY

the id of one of the device's identity strings, of a kind of Identity

<a id="mvconnect.masterbus.Command.STRING"></a>

#### STRING

four characters of a string, by its id; with characters behind them, a write

<a id="mvconnect.masterbus.Identity"></a>

## Identity

```python
class Identity(enum.IntEnum)
```

The device's identity strings, which Command.IDENTITY gives the ids of.

<a id="mvconnect.masterbus.Identity.PRODUCT"></a>

#### PRODUCT

the product number, such as 77020100

<a id="mvconnect.masterbus.Identity.SERIAL"></a>

#### SERIAL

<a id="mvconnect.masterbus.Identity.PRODUCT_NAME"></a>

#### PRODUCT\_NAME

"<type> <name>", e.g. "MSH House Bank"

<a id="mvconnect.masterbus.Identity.REVISION"></a>

#### REVISION

not every device has one

<a id="mvconnect.masterbus.Number"></a>

## Number

```python
class Number(enum.IntEnum)
```

The kinds of the device's 16-bit numbers that Command.NUMBER reads, those used here.

A page's own version number, which changes when its layout or attributes change (the writeable flags after
a lock, for one), is a kind too: 0x1c monitor, 0x1d history, 0x1e config and 0x1f alarm; see pages.PAGES.
So are the numbers of a page's values and groups.

<a id="mvconnect.masterbus.Number.VERSION"></a>

#### VERSION

firmware version, major in the high byte, minor in the low byte

<a id="mvconnect.masterbus.Number.LOGIN"></a>

#### LOGIN

the login level, 0 when logged out; written to log in (see MasterBus.login)

<a id="mvconnect.masterbus.Number.STATUS_VERSION"></a>

#### STATUS\_VERSION

changes whenever something about the device changed; MasterAdjust polls it

<a id="mvconnect.masterbus.FLOAT32_MAX"></a>

#### FLOAT32\_MAX

values, and a login's password, travel as float32

<a id="mvconnect.masterbus.sendable"></a>

#### sendable

```python
def sendable(number: float) -> bool
```

Return whether *number* can travel on the bus as a float32: finite, and within its range.

<a id="mvconnect.masterbus.BOOTLOADER_BIT"></a>

#### BOOTLOADER\_BIT

in the third info byte of an announcement: the device is in flash mode

<a id="mvconnect.masterbus.version_text"></a>

#### version\_text

```python
def version_text(v: int) -> str
```

Return the version word *v* as MasterAdjust shows it: major.minor, without a zero prefix.

<a id="mvconnect.masterbus.LOGIN_LEVELS"></a>

#### LOGIN\_LEVELS

<a id="mvconnect.masterbus.login_level_name"></a>

#### login\_level\_name

```python
def login_level_name(level: int | None) -> str
```

Return the login level *level* as Mastervolt names it.

The name is one of LOGIN_LEVELS for a level that has one, 'logged out' for 0, 'level N' for a level it has
no name for, and '?' for None.

<a id="mvconnect.masterbus.ENDING"></a>

#### ENDING

<a id="mvconnect.masterbus.uninterrupted"></a>

#### uninterrupted

```python
@contextmanager
def uninterrupted() -> Iterator[None]
```

Hold off the signals that end a process until the block is done, then deliver them.

The signals are those of ENDING: SIGINT for Ctrl-C, SIGTERM and SIGHUP. Holding them off means a write of
several frames is never cut off between two of them. Each signal that arrived is delivered once, when the
block ends, after the previous handlers are back in place. Only the main thread receives signals; elsewhere
the block runs as it is.

<a id="mvconnect.masterbus.Ended"></a>

## Ended

```python
class Ended(SystemExit)
```

The process ends on the signal *signum*, SIGTERM or SIGHUP, as an exit, so that its cleanups run.

Its status is 128 plus *signum*. A SystemExit with that status that is not an Ended, a remote command's
relayed status among them, says nothing about a signal here.

<a id="mvconnect.masterbus.ending_cause"></a>

#### ending\_cause

```python
def ending_cause(exc: BaseException | None) -> str | None
```

Return what ends the process, as a log line names it, or None where *exc* is no interrupt.

That is 'interrupted by Ctrl-C' for a KeyboardInterrupt, and 'ended by SIGTERM' and so on for an Ended. An
error, an ordinary exit or no exception at all is None.

<a id="mvconnect.masterbus.winding_down"></a>

#### winding\_down

```python
def winding_down(exc: BaseException | None,
                 step: str,
                 log: logging.Logger | None = None) -> None
```

Log that *step* is wound down because *exc* ends the process, where it is an interrupt.

The line names the cause first (see ending_cause()), then the step: 'interrupted by Ctrl-C: closing the
link to the bus'. The first such line of the process is a warning, since it is the one that says a signal
came; the steps after it are at info level. It goes to *log*, the logger of the module that winds down,
this module's by default. Nothing is logged for an error or an ordinary end, which have their own lines.

<a id="mvconnect.masterbus.NotAvailable"></a>

## NotAvailable

```python
class NotAvailable(TimeoutError)
```

The device answers 'not available' (kind 0x0d): it has no such thing to give.

It is a TimeoutError still, as for a caller one is as good as the other, unless it has to tell an answer
from silence.

<a id="mvconnect.masterbus.Device"></a>

## Device

```python
@dataclass
class Device()
```

A device on the bus.

It holds its *address*; the four *info* bytes of its announcement, whose meaning is not yet known; its
*product* number such as 77020100; its *serial* number; its *product_name* as the device reports it,
'<type> <name>'; its hardware *revision*; and its firmware *version*, 'major.minor'. Everything but the
address and the info bytes is filled in by MasterBus.identify().

<a id="mvconnect.masterbus.Device.address"></a>

#### address: `Address`

<a id="mvconnect.masterbus.Device.info"></a>

#### info: `bytes`

<a id="mvconnect.masterbus.Device.product"></a>

#### product: `str`

<a id="mvconnect.masterbus.Device.serial"></a>

#### serial: `str`

<a id="mvconnect.masterbus.Device.product_name"></a>

#### product\_name: `str`

<a id="mvconnect.masterbus.Device.revision"></a>

#### revision: `str`

<a id="mvconnect.masterbus.Device.version"></a>

#### version: `str`

<a id="mvconnect.masterbus.Device.selector"></a>

#### selector

```python
@property
def selector() -> str
```

Return the device's selector, the name no other device shares: its address as hh:lllll.

<a id="mvconnect.masterbus.Device.kind"></a>

#### kind

```python
@property
def kind() -> str
```

Return the device type in front of the product name, such as MSH, CHG or DIS.

<a id="mvconnect.masterbus.Device.name"></a>

#### name

```python
@property
def name() -> str
```

Return the name given to the device: the product name without its type.

<a id="mvconnect.masterbus.Device.flash_mode"></a>

#### flash\_mode

```python
@property
def flash_mode() -> bool
```

Return whether the device announced itself in flash mode, its bootloader running.

<a id="mvconnect.masterbus.AnnounceInfo"></a>

## AnnounceInfo

```python
@dataclass
class AnnounceInfo()
```

The data of a KIND_ANNOUNCE frame: the device's *address* and its four *info* bytes.

<a id="mvconnect.masterbus.AnnounceInfo.address"></a>

#### address: `Address`

<a id="mvconnect.masterbus.AnnounceInfo.info"></a>

#### info: `bytes`

<a id="mvconnect.masterbus.AnnounceInfo.parse"></a>

#### parse

```python
@classmethod
def parse(cls, data: bytes) -> AnnounceInfo
```

Return the AnnounceInfo in *data*, the data of an announcement.

Raise ValueError if *data* is not 8 bytes.

<a id="mvconnect.masterbus.AnnounceInfo.flash_mode"></a>

#### flash\_mode

```python
@property
def flash_mode() -> bool
```

Return whether the device announces itself in flash mode, its bootloader running.

<a id="mvconnect.masterbus.MasterBus"></a>

## MasterBus

```python
class MasterBus()
```

A MasterBus reached through one link.

Create one on *link*, a HidLink (the first link found, by default), with *host* as this computer's
address (by default the one host_address() computes from the link's serial number when it is opened, or
HOST_ADDRESS), waiting *reply_timeout* seconds for each reply (0.3 by default) and re-sending a request
*retries* times (2 by default) before giving up. Every method that sends raises LinkError once the link
has failed.

<a id="mvconnect.masterbus.MasterBus.host"></a>

#### host

```python
@property
def host() -> Address
```

Return this computer's address on the bus: the one given, or else the one open() found.

<a id="mvconnect.masterbus.MasterBus.open"></a>

#### open

```python
def open() -> MasterBus
```

Open the link and return the bus.

Without a host address given, the bus takes the one host_address() computes from the link's serial
number, where the link says one.

<a id="mvconnect.masterbus.MasterBus.send"></a>

#### send

```python
def send(*frames: Frame) -> None
```

Send *frames* on the link.

Every frame for the bus goes through here, so the taps see it. Raise LinkError if the link cannot send
them; see HidLink.send().

<a id="mvconnect.masterbus.MasterBus.tap"></a>

#### tap

```python
def tap(listener: Callable[[Frame, bool], None]) -> Callable[[], None]
```

Have *listener*(frame, sent) called for every frame this bus sends or receives.

A received frame is passed from the link's reader thread. An exception the listener raises is logged
and goes no further. Return a function that removes the tap.

<a id="mvconnect.masterbus.MasterBus.close"></a>

#### close

```python
def close() -> None
```

Close the link.

<a id="mvconnect.masterbus.MasterBus.enumerate"></a>

#### enumerate

```python
def enumerate(timeout: float = 1.5) -> list[Device]
```

Ask every device to announce itself and return those that did.

Return the devices that announced themselves within *timeout* seconds (1.5 by default), in address
order, each a Device with its address and info bytes only. An announcement that does not parse is
logged and left out.

<a id="mvconnect.masterbus.MasterBus.request"></a>

#### request

```python
def request(address: Address,
            data: bytes,
            echo: int = 2,
            retries: int | None = None) -> bytes
```

Send a request to the device at *address* and return the data of its reply.

*data* is the request's data, the command byte first. A reply is a KIND_REPLY frame from the device
whose first *echo* data bytes (2 by default) repeat the request's (the command byte and its selector).
Raise TimeoutError if no reply comes within reply_timeout after *retries* re-sends (the bus's own count
by default), and NotAvailable, a TimeoutError, if the device answers 'not available'.

<a id="mvconnect.masterbus.MasterBus.exchange"></a>

#### exchange

```python
def exchange(frame: Frame,
             match: Callable[[Frame], bool],
             *,
             timeout: float | None = None,
             retries: int | None = None) -> Frame
```

Send *frame* and return the first reply that *match* accepts.

The reply is looked for among the frames that arrive within *timeout* seconds after the frame is sent,
and the frame is sent again up to *retries* times; a late reply to an earlier attempt counts.
*timeout* and *retries* default to the bus's.

Every conversation on the bus goes through here: request() and the page requests match a reply by
its address, echo and kind, and the flasher matches the bootloader's ACK (kind 0x1d) and NAK (kind
0x2d) to its 0x3d transfer frames. Raise TimeoutError if nothing is accepted.

<a id="mvconnect.masterbus.MasterBus.read_string"></a>

#### read\_string

```python
def read_string(address: Address,
                string_id: int,
                encoding: str = "latin-1",
                retries: int | None = None) -> str
```

Return string *string_id* of the device at *address*.

The string is read four characters per round trip, at most 64 of them, up to its first NUL or a chunk
shorter than four characters, and decoded with *encoding* (latin-1 by default), a character it cannot
decode replaced. *retries* is passed to request(). Raise TimeoutError, or NotAvailable, as request()
does.

<a id="mvconnect.masterbus.MasterBus.STRING_MAX"></a>

#### STRING\_MAX

MasterAdjust cuts a string to 16 characters before writing it

<a id="mvconnect.masterbus.MasterBus.write_string"></a>

#### write\_string

```python
def write_string(address: Address,
                 string_id: int,
                 text: str,
                 encoding: str = "latin-1",
                 retries: int = 0) -> None
```

Write *text* as string *string_id* of the device at *address*, as MasterAdjust does.

The request is the same as a read, with the characters behind the chunk number, four per round trip and
a NUL at the end. The text is encoded with *encoding* (latin-1 by default), a character it cannot
encode replaced, and cut to its first STRING_MAX bytes, as many characters in latin-1; the NUL is added
unless the cut text ends in one. An interrupt waits until the last chunk is written.

Like every write, each chunk is sent once (*retries* 0): a chunk whose answer is lost raises TimeoutError,
and reading the string back tells what the device holds. A chunk the device answers 'not available' to
raises NotAvailable.

<a id="mvconnect.masterbus.MasterBus.read_number"></a>

#### read\_number

```python
def read_number(address: Address, kind: int) -> int
```

Return the 16-bit number of *kind* of the device at *address*.

*kind* is a Number, a page's version, or the number of a page's values or groups. Raise ValueError on
a short reply, and TimeoutError, or NotAvailable, as request() does.

<a id="mvconnect.masterbus.MasterBus.login"></a>

#### login

```python
def login(address: Address, level: int, password: float = 0.0) -> None
```

Log the device at *address* in at *level* with *password*, or out with level 0.

The login is Number.LOGIN written with the level and the password as a float behind it; a password of
0, the default, is left out (DLC 4), as MasterAdjust does. It is sent once, and its answer must repeat
the level. Raise ValueError for a password that cannot travel as a float32 (see sendable()) or a level
that is neither 0 nor one of LOGIN_LEVELS, TimeoutError if the device does not answer or refuses.

<a id="mvconnect.masterbus.MasterBus.login_level"></a>

#### login\_level

```python
def login_level(address: Address) -> int
```

Return the level the device at *address* is logged in at, 0 when logged out.

<a id="mvconnect.masterbus.MasterBus.identity_string_id"></a>

#### identity\_string\_id

```python
def identity_string_id(address: Address, which: int) -> int
```

Return the string id behind one of the device's identity strings, *which* being an Identity.

Raise ValueError on a short reply, and TimeoutError, or NotAvailable, as request() does.

<a id="mvconnect.masterbus.MasterBus.read_identity"></a>

#### read\_identity

```python
def read_identity(address: Address, which: int) -> str
```

Return one of the device's identity strings, *which* being an Identity, looked up through its id.

Raise ValueError and TimeoutError as identity_string_id() and read_string() do.

<a id="mvconnect.masterbus.MasterBus.IDENTITY"></a>

#### IDENTITY

<a id="mvconnect.masterbus.MasterBus.identify"></a>

#### identify

```python
def identify(device: Device) -> Device
```

Read the identity of *device* into it and return it.

The identity is its product number, serial number, product name, revision and firmware version. A value
the device does not answer for, or answers for too briefly, is left empty, with a warning, except the
revision, which not every device has.

<a id="mvconnect.masterbus.MasterBus.list_devices"></a>

#### list\_devices

```python
def list_devices(timeout: float = 1.5,
                 progress: Progress | None = None) -> list[Device]
```

Return every device on the bus, identified.

The devices are those that announce themselves within *timeout* seconds (see enumerate()), each read
with identify(). *progress*, if given, is called with the number of devices identified so far and the
total.

