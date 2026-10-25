<a id="mvconnect.system"></a>

# mvconnect.system

The bus as devices, pages and values, addressed by words.

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

<a id="mvconnect.system.TRAFFIC_BACKLOG"></a>

#### TRAFFIC\_BACKLOG

frames held for a reader of the traffic that lags; more are dropped

<a id="mvconnect.system.AddressError"></a>

## AddressError

```python
class AddressError(Exception)
```

The words name no device, page or value, or more than one where one is needed.

<a id="mvconnect.system.Refused"></a>

## Refused

```python
class Refused(ValueError)
```

A write the checks refuse before anything is sent.

The checks refuse a value out of range, not an option, too long, or one the device marks read-only. The
caller may send it anyway.

<a id="mvconnect.system.Written"></a>

## Written

```python
@dataclass
class Written()
```

What a write did.

It holds the *value* written; the value before and after, as text (*before*, *after*); what was asked for
(*wanted*); any *warnings*; and whether the value reads back as asked for (*took*).

<a id="mvconnect.system.Written.value"></a>

#### value: `Value`

<a id="mvconnect.system.Written.before"></a>

#### before: `str`

<a id="mvconnect.system.Written.after"></a>

#### after: `str`

<a id="mvconnect.system.Written.wanted"></a>

#### wanted: `str`

<a id="mvconnect.system.Written.warnings"></a>

#### warnings: `list[str]`

<a id="mvconnect.system.Written.took"></a>

#### took: `bool`

<a id="mvconnect.system.Selection"></a>

## Selection

```python
@dataclass
class Selection()
```

What a list of words names: the device, and the readings of the words after it.

*device* is the device. *readings* are the readings of the words after it, the likeliest first, each a page
name (None for every page) and the words left for the value ('' for the whole page or device). A page
keyword may be the page or a word of a name, as in Alarm levels or Min/Max history, so each page keyword is
a reading of its own, and all the words on every page the last.

<a id="mvconnect.system.Selection.device"></a>

#### device: `Device`

<a id="mvconnect.system.Selection.readings"></a>

#### readings: `list[tuple[str | None, str]]`

<a id="mvconnect.system.Selection.page"></a>

#### page

```python
@property
def page() -> str | None
```

Return the page of the likeliest reading.

<a id="mvconnect.system.Selection.words"></a>

#### words

```python
@property
def words() -> str
```

Return the value's words in the likeliest reading.

<a id="mvconnect.system.check_identity"></a>

#### check\_identity

```python
def check_identity(devices: list[Device]) -> list[Device]
```

Warn of each of *devices* without a serial number or firmware version, and return *devices*.

Every MasterBus device has both, so one missing is a fault, a reply lost on the bus or a bug, and worth
saying whenever the devices are listed: such a device's pages are never cached, since the cache is keyed
by both.

<a id="mvconnect.system.System"></a>

## System

```python
class System()
```

The bus as devices, pages and values, seen through one link.

Create one on *bus*. Page layouts are cached on disk per device firmware. With *refresh* each page is read
from its device again the first time this System needs it, and the cache is updated; with *use_cache*
False the cache is neither read nor written. Every method that talks to the bus raises LinkError once the
link has failed.

<a id="mvconnect.system.System.devices"></a>

#### devices

```python
def devices(timeout: float = 1.5,
            progress: Progress | None = None,
            refresh: bool = False) -> list[Device]
```

Return the devices on the bus, identified.

Listen *timeout* seconds for them the first time and remember them after; with *refresh*, enumerate the
bus again. *progress* is handed to MasterBus.list_devices(). A device without a serial number or
firmware version is warned of (check_identity()).

<a id="mvconnect.system.System.device"></a>

#### device

```python
def device(words: str, timeout: float = 1.5) -> Device
```

Return the device *words* select.

The words are its selector, or its name, type (MSH, CHG, DIS...) or product name, exactly or by word
starts (see mvconnect.words). *timeout* is as for devices(). Raise AddressError unless exactly one device
is selected, with nothing left over.

<a id="mvconnect.system.System.locate"></a>

#### locate

```python
def locate(words: Iterable[object], timeout: float = 1.5) -> Selection
```

Return the Selection *words* make.

The longest run of leading words that selects exactly one device is the device; among the rest, a page
keyword names the page and the others are the value's words; a page keyword may also be a word of a
name, see Selection. *timeout* is as for devices(). Raise AddressError if there are no words or no run of
leading words is one device; where the longest run that selects devices selects several, the error says
so.

<a id="mvconnect.system.System.traffic"></a>

#### traffic

```python
def traffic() -> Iterator[tuple[float, Frame, bool]]
```

Yield every frame on the bus as it passes, sent or received, as (time.time(), frame, sent).

Go on until the caller stops asking. A reader that falls TRAFFIC_BACKLOG frames behind loses frames,
with a warning. Raise LinkError when the link fails.

<a id="mvconnect.system.System.listen"></a>

#### listen

```python
def listen() -> tuple[queue.Queue, Callable[[], None], list[int]]
```

Return a queue that every frame on the bus is put into, a function that stops it, and a drop count.

Each frame is put as (time.time(), frame, sent). The count is a one-element list of the frames dropped
because the queue held TRAFFIC_BACKLOG already: a reader that stalls costs frames, not memory.

<a id="mvconnect.system.System.login_level"></a>

#### login\_level

```python
def login_level(device: Device) -> int
```

Return the level *device* is logged in at, 0 when logged out.

Raise TimeoutError if it does not say, ValueError if its answer is too short to say.

<a id="mvconnect.system.System.login"></a>

#### login

```python
def login(device: Device,
          level: int,
          password: float = 0.0) -> tuple[bool, int | None]
```

Log *device* in at *level* with *password*, 0 to log out, and read the level back.

Return whether the device acknowledged the login, and the level it reports after it, None if it does
not say. Raise ValueError, sending nothing, for a level that is none of LOGIN_LEVELS or a password the
bus cannot carry.

<a id="mvconnect.system.System.tables"></a>

#### tables

```python
def tables(device: Device,
           pages: list[Page] | None = None,
           progress: Progress | None = None,
           afresh: Iterable[str] | None = None) -> dict[str, PageTable]
```

Return the page tables of *device* for *pages*, all four by default, by page name.

A table comes from the cache where it has it and its version number still matches the device's; else
it is read from the device, with *progress* called as values and groups come in, and saved to the cache
beside the pages it holds. The pages named in *afresh* are read from the device whatever the cache
holds; by default, with *refresh*, those this System has not read afresh yet.

<a id="mvconnect.system.System.to_refresh"></a>

#### to\_refresh

```python
def to_refresh(device: Device, pages: list[Page]) -> list[str]
```

Return the names of the *pages* to read from *device* afresh.

With *refresh*, these are the pages this System has not read afresh yet, which it then counts as read;
without, there are none.

<a id="mvconnect.system.System.read_texts"></a>

#### read\_texts

```python
def read_texts(device: Device, table: PageTable,
               indices: list[int]) -> dict[int, str]
```

Return the texts behind the text values among *indices* of *table*'s page, by index.

A value the device does not answer for is left out, with a warning.

<a id="mvconnect.system.System.read_values"></a>

#### read\_values

```python
def read_values(device: Device, table: PageTable,
                indices: list[int]) -> dict[int, float | None]
```

Return the values at *indices* of *table*'s page, by index.

A value the device does not answer for is None, with a warning.

<a id="mvconnect.system.System.read_options"></a>

#### read\_options

```python
def read_options(device: Device, table: PageTable, value: Value) -> list[str]
```

Return the entries of *value*, a list, device or command value of *table*'s page, as they are now.

The entries are read from the device, not taken from the table; see PageReader.read_options().

<a id="mvconnect.system.System.read_entries"></a>

#### read\_entries

```python
def read_entries(device: Device, table: PageTable,
                 indices: list[int]) -> dict[int, list[str]]
```

Return the entries of the device and command values among *indices* of *table*'s page, by index.

Their entries follow the bus, so the table keeps none (see Value); a listing reads them here, as it
reads the texts of text values. A value the device does not answer for is left out, with a warning.

<a id="mvconnect.system.System.read_text"></a>

#### read\_text

```python
def read_text(device: Device, table: PageTable,
              index: int) -> tuple[int | None, str]
```

Return (string id, text) for the text value at *index* of *table*'s page; see PageReader.read_text().

<a id="mvconnect.system.System.write_value"></a>

#### write\_value

```python
def write_value(device: Device, table: PageTable, index: int,
                value: float) -> float
```

Write *value* at *index* of *table*'s page and return what the device echoes.

See PageReader.write_value().

<a id="mvconnect.system.System.write_text"></a>

#### write\_text

```python
def write_text(device: Device, table: PageTable, index: int, text: str) -> str
```

Write *text* as the string the text value at *index* of *table*'s page names.

Return the text read back; see PageReader.write_text().

<a id="mvconnect.system.System.write"></a>

#### write

```python
def write(device: Device,
          table: PageTable,
          value: Value,
          text: str,
          force: bool = False,
          timeout: float = 1.5) -> Written
```

Write *text* to *value* of *table*'s page on *device*, read it back and return a Written.

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

<a id="mvconnect.system.System.write_default"></a>

#### write\_default

```python
def write_default(device: Device,
                  table: PageTable,
                  value: Value,
                  force: bool = False) -> Written
```

Reset *value* of *table*'s page on *device* to the default the device gives.

Write the default, read it back and return a Written. Raise ValueError where there is no default to
return to (none given, or a text, device or command value) and for an action value, whose default would
carry the action out; raise Refused where the device marks the value read-only, or for a counter or a
date, which *force* skips.

<a id="mvconnect.system.System.find_values"></a>

#### find\_values

```python
@staticmethod
def find_values(table: PageTable, words: str) -> list[Value]
```

Return the values of *table* that *words* select, in table order (see mvconnect.words).

A lone index selects the value it names; else each value is selected for which each word equals its
index or starts a word of its name, long name or group. No words select every value.

<a id="mvconnect.system.System.sections"></a>

#### sections

```python
@staticmethod
def sections(table: PageTable,
             values: list[Value]) -> list[tuple[str, list[int]]]
```

Return (group name, indices) pairs laying *values* out as the device groups them.

The groups come in table order with the values each has among them, then the rest under 'other'; a page
without groups is one unnamed section.

<a id="mvconnect.system.System.find"></a>

#### find

```python
def find(device: Device,
         words: str,
         page: str | None = None,
         progress: Progress | None = None) -> list[tuple[PageTable, Value]]
```

Return (table, value) for every value of *device* that *words* select, in page and table order.

Search *page*, or every page without one. The page tables are read as for tables(), with *progress*.
Raise AddressError for a *page* that is none of PAGES, whose names are taken without regard to case.

<a id="mvconnect.system.System.find_selection"></a>

#### find\_selection

```python
def find_selection(
    sel: Selection,
    progress: Progress | None = None
) -> tuple[str | None, str, list[tuple[PageTable, Value]]]
```

Return the reading of *sel* that selects something, as its page, its words and what find() gives.

That is the likeliest reading that selects any value, or one that names a page alone, which stands even
when the page is empty; the likeliest reading with nothing found when none does. *progress* is as for
tables().

<a id="mvconnect.system.System.resolve_value"></a>

#### resolve\_value

```python
def resolve_value(device: Device,
                  words: str,
                  page: str | None = None) -> tuple[PageTable, Value]
```

Return (table, value) for the one value of *device* that *words* select, for writing.

Search *page*, or any page without one. Raise AddressError if the words are empty or select no value
or several.

