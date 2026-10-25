<a id="mvconnect.pages"></a>

# mvconnect.pages

A device's property pages: monitor, alarm, history, configuration.

Every page is a table of values with an index each. Three request kinds
serve a page: one reads a value (DLC 2: index) and, with DLC 6 (index,
float32), writes it; one reads attributes of a value (DLC 3: attribute,
index; DLC 4 adds a sub-index); one reads attributes of a group. A device
answers with kind - 0x20, or kind - 0x10 when the value or attribute does
not exist or it refuses the value. Group attribute 0x03 with a sub-index
n gives the index of the group's n-th value.

<a id="mvconnect.pages.Page"></a>

## Page

```python
@dataclass(frozen=True)
class Page()
```

One of a device's four pages and the request kinds that serve it.

*name* is the page's name. *read* reads a value and, with a float behind the index, writes it (None where
values cannot be read); *info* reads a value's attributes; *group* reads a group's attributes.
*count_values* and *count_groups* are the kinds of masterbus.Number for the numbers of values and groups,
and *version* the kind for the page's version number, which changes when the page changes.

<a id="mvconnect.pages.Page.name"></a>

#### name: `str`

<a id="mvconnect.pages.Page.read"></a>

#### read: `int | None`

<a id="mvconnect.pages.Page.info"></a>

#### info: `int`

<a id="mvconnect.pages.Page.group"></a>

#### group: `int`

<a id="mvconnect.pages.Page.count_values"></a>

#### count\_values: `int`

<a id="mvconnect.pages.Page.count_groups"></a>

#### count\_groups: `int`

<a id="mvconnect.pages.Page.version"></a>

#### version: `int`

<a id="mvconnect.pages.PAGES"></a>

#### PAGES

<a id="mvconnect.pages.PAGE_BY_NAME"></a>

#### PAGE\_BY\_NAME

<a id="mvconnect.pages.Attribute"></a>

## Attribute

```python
class Attribute(enum.IntEnum)
```

The attributes a page's info and group requests ask for, by number.

Each is named after the field of Value (or Group) it fills. A string's attribute gives the id of the
string, read with Command.STRING.

<a id="mvconnect.pages.Attribute.VISUALIZATION"></a>

#### VISUALIZATION

the VIS_* type: 1 number, 3 list of texts, 4 alarm...

<a id="mvconnect.pages.Attribute.MEMBERS"></a>

#### MEMBERS

a group's: with a sub-index n, the index of its n-th value

<a id="mvconnect.pages.Attribute.MINIMUM"></a>

#### MINIMUM

<a id="mvconnect.pages.Attribute.MAXIMUM"></a>

#### MAXIMUM

<a id="mvconnect.pages.Attribute.STEP"></a>

#### STEP

<a id="mvconnect.pages.Attribute.DEFAULT"></a>

#### DEFAULT

<a id="mvconnect.pages.Attribute.WRITEABLE"></a>

#### WRITEABLE

<a id="mvconnect.pages.Attribute.GRAY"></a>

#### GRAY

<a id="mvconnect.pages.Attribute.EVENTABLE"></a>

#### EVENTABLE

<a id="mvconnect.pages.Attribute.COMMANDS"></a>

#### COMMANDS

with a sub-index n: the string id of entry n of a command value

<a id="mvconnect.pages.Attribute.TARGETS"></a>

#### TARGETS

with a sub-index n: the address of entry n of a device value, in 4 bytes

<a id="mvconnect.pages.Attribute.OPTIONS"></a>

#### OPTIONS

with a sub-index n: the string id of option n

<a id="mvconnect.pages.Attribute.NAME"></a>

#### NAME

the string id of the name

<a id="mvconnect.pages.Attribute.LONG_NAME"></a>

#### LONG\_NAME

the string id of a longer name

<a id="mvconnect.pages.Attribute.DESCRIPTION"></a>

#### DESCRIPTION

the string id of the longest text, a description

<a id="mvconnect.pages.Attribute.UNIT"></a>

#### UNIT

the string id of the unit

<a id="mvconnect.pages.VIS_NUMBER"></a>

#### VIS\_NUMBER

<a id="mvconnect.pages.VIS_FLAG"></a>

#### VIS\_FLAG

0..2 status flags on the CHG

<a id="mvconnect.pages.VIS_LIST"></a>

#### VIS\_LIST

option texts via attribute 0x26

<a id="mvconnect.pages.VIS_ALARM"></a>

#### VIS\_ALARM

<a id="mvconnect.pages.VIS_ACTION"></a>

#### VIS\_ACTION

0..1 or 0..2 push buttons: "Reset setup", "Start test", "Clear history"

<a id="mvconnect.pages.VIS_TEXT"></a>

#### VIS\_TEXT

a text such as "Device name"; the range is its length. MasterAdjust shows it, offers no edit

<a id="mvconnect.pages.VIS_COUNTER"></a>

#### VIS\_COUNTER

history counters, read-only

<a id="mvconnect.pages.VIS_DATE"></a>

#### VIS\_DATE

<a id="mvconnect.pages.VIS_DEVICE"></a>

#### VIS\_DEVICE

a device on the bus: "Event n target", "Master", "Slave n"; its entries are addresses (attribute 0x20)

<a id="mvconnect.pages.VIS_COMMAND"></a>

#### VIS\_COMMAND

"Event n command": a command of the target device; its entries are texts (attribute 0x0F)

<a id="mvconnect.pages.LISTED"></a>

#### LISTED

values with entries

<a id="mvconnect.pages.LIVE"></a>

#### LIVE

values whose entries follow the bus: read when needed, never kept in a table

<a id="mvconnect.pages.NO_DEFAULT"></a>

#### NO\_DEFAULT

how devices say a value has no default

<a id="mvconnect.pages.ENTRIES_MOST"></a>

#### ENTRIES\_MOST

how many entries a list is read for where its maximum does not say

<a id="mvconnect.pages.MASTER_ADDRESS_TYPE"></a>

#### MASTER\_ADDRESS\_TYPE

the high field of a master's address (MasterAdjust, an EasyView); data[2] of the frame above

<a id="mvconnect.pages.VIS_NAMES"></a>

#### VIS\_NAMES

<a id="mvconnect.pages.VIS_LETTERS"></a>

#### VIS\_LETTERS

<a id="mvconnect.pages.Value"></a>

## Value

```python
@dataclass
class Value()
```

One value of a page, as its attributes describe it.

It holds the value's *index*, *name* and *unit*; its *visualization* type, one of VIS_*; its *minimum*,
*maximum*, *step* and *default*; whether the device marks it *writeable*; the *options* of a list value,
the entries' texts by value; whether it is shown *gray* (greyed out, not in effect right now) and whether
it is *eventable* (the device broadcasts the value); and its *long_name* and *description*.

A device or command value has entries too (a device value's are the addresses as str() writes them, or
'none' for an empty entry), but they follow the bus, so a table keeps none: PageReader.read_options()
reads them when they are shown or written, and they stand in *options* only in a copy made then.

<a id="mvconnect.pages.Value.index"></a>

#### index: `int`

<a id="mvconnect.pages.Value.name"></a>

#### name: `str`

<a id="mvconnect.pages.Value.unit"></a>

#### unit: `str`

<a id="mvconnect.pages.Value.visualization"></a>

#### visualization: `int`

<a id="mvconnect.pages.Value.minimum"></a>

#### minimum: `float | None`

<a id="mvconnect.pages.Value.maximum"></a>

#### maximum: `float | None`

<a id="mvconnect.pages.Value.step"></a>

#### step: `float | None`

<a id="mvconnect.pages.Value.default"></a>

#### default: `float | None`

<a id="mvconnect.pages.Value.writeable"></a>

#### writeable: `bool`

<a id="mvconnect.pages.Value.options"></a>

#### options: `list[str]`

<a id="mvconnect.pages.Value.gray"></a>

#### gray: `bool`

<a id="mvconnect.pages.Value.eventable"></a>

#### eventable: `int`

<a id="mvconnect.pages.Value.long_name"></a>

#### long\_name: `str`

<a id="mvconnect.pages.Value.description"></a>

#### description: `str`

<a id="mvconnect.pages.Value.parse"></a>

#### parse

```python
def parse(text: str) -> float
```

Return the number *text* means for this value.

A value with options takes only their names, compared without regard to case, and gives the option's
index: a number, an index among them, is no name. A switch, an alarm value or an action or flag value
of range 0..1, takes on and off (or true and false, yes and no) as 1 and 0. Any other value takes a
number. A number past a bound by less than a millionth of it gives the bound, since the bounds arrive
as float32.

Raise ValueError, which names the options where there are any, if *text* is none of those, is a number
the bus cannot carry (see sendable()) or lies outside the range.

<a id="mvconnect.pages.Value.writer"></a>

#### writer

```python
@property
def writer() -> str
```

Return how a write reaches this value: 'float' or 'text'.

<a id="mvconnect.pages.Value.text_length"></a>

#### text\_length

```python
@property
def text_length() -> int
```

Return the most characters a text value takes.

That is its range's maximum, but no more than the 16 that MasterBus.write_string() writes, and 16
without a maximum or with one of 0 or less.

<a id="mvconnect.pages.Value.option_for"></a>

#### option\_for

```python
def option_for(*names: str) -> int | None
```

Return the index of the option equal to one of *names*, compared without regard to case, or None.

<a id="mvconnect.pages.Value.default_value"></a>

#### default\_value

```python
@property
def default_value() -> float | None
```

Return the default the device gives, or None where it gives none.

Devices mark 'no default' with 65535 or another value outside the range, 65535 also where there is no
range, and a list's default must name one of its entries.

<a id="mvconnect.pages.Value.type_name"></a>

#### type\_name

```python
@property
def type_name() -> str
```

Return the visualization type's name, such as 'number' or 'list'.

<a id="mvconnect.pages.Value.flags"></a>

#### flags

```python
@property
def flags() -> str
```

Return the attributes in four characters, as ls -l shows a mode.

The characters are the type's letter, then w for writeable, e for eventable and g for greyed out, or -
for each that is not.

<a id="mvconnect.pages.Value.describe"></a>

#### describe

```python
def describe(value: float | None,
             text: str | None = None) -> list[tuple[str, str]]
```

Return (label, text) pairs describing the value.

The pairs say what it holds (*value*, and *text* for a text value), its unit, type, long name and
description, its options or range, its default, whether and how it may be written, and whether it is
eventable or greyed out.

<a id="mvconnect.pages.Value.format"></a>

#### format

```python
def format(value: float | None) -> str
```

Return *value* as text for this value.

That is the entry's name for a list, device or command value whose entries are known, '-' for None,
'n/a' for NaN, else the number with the step's precision.

<a id="mvconnect.pages.Group"></a>

## Group

```python
@dataclass
class Group()
```

A group of a page: its *index*, *name* and the indices of its *values*.

<a id="mvconnect.pages.Group.index"></a>

#### index: `int`

<a id="mvconnect.pages.Group.name"></a>

#### name: `str`

<a id="mvconnect.pages.Group.values"></a>

#### values: `list[int]`

<a id="mvconnect.pages.PageTable"></a>

## PageTable

```python
@dataclass
class PageTable()
```

The layout of one page as read from a device.

It holds its *page*, its *values* by index, its *groups*, and the page's *version* number when it was
read, None where unknown.

<a id="mvconnect.pages.PageTable.page"></a>

#### page: `Page`

<a id="mvconnect.pages.PageTable.values"></a>

#### values: `dict[int, Value]`

<a id="mvconnect.pages.PageTable.groups"></a>

#### groups: `list[Group]`

<a id="mvconnect.pages.PageTable.version"></a>

#### version: `int | None`

<a id="mvconnect.pages.PageTable.ungrouped"></a>

#### ungrouped

```python
def ungrouped() -> list[int]
```

Return the indices of the values no group holds, in index order.

<a id="mvconnect.pages.entry_address"></a>

#### entry\_address

```python
def entry_address(data: bytes) -> Address | None
```

Return the address in *data*, the 8-byte reply for an entry of a device value, or None for an empty entry.

The high field is data[4], the low field the three bytes after it, little-endian, in 18 bits; a high
field above 0x1F marks an empty entry.

<a id="mvconnect.pages.PageReader"></a>

## PageReader

```python
class PageReader()
```

Read and write a device's pages through a MasterBus.

Create one for the device at *address* on *bus*, waiting *reply_timeout* seconds (0.25 by default) for
each reply and re-sending a request *retries* times (2 by default) before giving up.

<a id="mvconnect.pages.PageReader.request"></a>

#### request

```python
def request(kind: int,
            data: bytes,
            retries: int | None = None) -> bytes | None
```

Send a page request of *kind* with *data* and return the reply's data.

A reply repeats the request's data. Return None when the device answers 'not available'. *retries*
defaults to the reader's own. Raise TimeoutError if no reply comes.

<a id="mvconnect.pages.PageReader.read_value"></a>

#### read\_value

```python
def read_value(page: Page, index: int) -> float | None
```

Return the value at *index* of *page*.

Return None where the page has no read kind, the device answers 'not available' or the reply is short.
Raise TimeoutError if the device does not answer.

<a id="mvconnect.pages.PageReader.write_value"></a>

#### write\_value

```python
def write_value(page: Page, index: int, value: float) -> float
```

Write *value* at *index* of *page* and return what the device echoes.

Raise ValueError if *page* has no read kind, for a number the bus cannot carry (see sendable()) or if
the device refuses (out of range, read-only, or not logged in), TimeoutError if it does not answer.

A write is sent once and never repeated: when its reply is lost, the device may have acted on it, and a
button such as 'Complete' would be pressed again. The caller reads the value back instead.

The reply to a write has the kind and index of the reply to a read, so another master polling the
value answers alike. A refusal or an echo of exactly the value sent decides; a reply with another value,
the device's own when it stores the value rounded, counts only when neither comes in time.

<a id="mvconnect.pages.PageReader.read_text"></a>

#### read\_text

```python
def read_text(page: Page, index: int) -> tuple[int | None, str]
```

Return (string id, text) for the text value at *index* of *page*.

That is the id its value holds and the string behind it, or (None, '') where the value is not an id.
Raise TimeoutError if the device does not answer for either.

<a id="mvconnect.pages.PageReader.write_text"></a>

#### write\_text

```python
def write_text(page: Page, index: int, text: str) -> str
```

Write *text* as the string the text value at *index* of *page* names, and return the text read back.

The text goes through MasterBus.write_string(). Raise ValueError if the value names no string,
TimeoutError if the device does not answer.

<a id="mvconnect.pages.PageReader.attr_u16"></a>

#### attr\_u16

```python
def attr_u16(kind: int,
             attr: int,
             index: int,
             sub: int | None = None) -> int | None
```

Return the 16-bit attribute *attr* of value *index*, or of its option *sub*, read with request *kind*.

Return None if the device has none. Each is read once in this reader's life: the junk check of
read_page() and read_attributes() ask for the same name and unit. Raise TimeoutError if the device
does not answer.

<a id="mvconnect.pages.PageReader.attr_float"></a>

#### attr\_float

```python
def attr_float(kind: int, attr: int, index: int) -> float | None
```

Return the float attribute *attr* of value *index*, read with request kind *kind*.

Return None if the device has none. Raise TimeoutError if it does not answer.

<a id="mvconnect.pages.PageReader.attr_string"></a>

#### attr\_string

```python
def attr_string(kind: int,
                attr: int,
                index: int,
                sub: int | None = None) -> str
```

Return the string attribute *attr* of value *index*, or of its option *sub*, read with request *kind*.

Return '' if the device has none or does not answer for it.

<a id="mvconnect.pages.PageReader.read_attributes"></a>

#### read\_attributes

```python
def read_attributes(page: Page, index: int) -> Value
```

Return the Value at *index* of *page* with every attribute read, options included.

Some firmware answers for options it has no text for with junk (the CHG with ids it never answers a
read of, the APR with its serial number's id); the options stop at the first such answer (see
read_options()). Raise TimeoutError if the device does not answer for a number attribute.

<a id="mvconnect.pages.PageReader.serial_id"></a>

#### serial\_id

```python
def serial_id() -> int | None
```

Return the string id of the device's serial number, asked for once, or None where the device does not say.

Some firmware answers for texts it has none for with this id (see read_options()).

<a id="mvconnect.pages.PageReader.read_options"></a>

#### read\_options

```python
def read_options(page: Page, value: Value) -> list[str]
```

Return the entries of *value*, a list, device or command value of *page*, their texts by index.

A device holds as many entries as the value's maximum says, from 0, and is asked for no more, as
MasterAdjust asks. A list value's maximum is *value*'s, and without one between 1 and 255 the device is
asked until it has none, for at most ENTRIES_MOST. A device or command value's maximum is read afresh,
since it follows the bus as the entries do (the CHG's event targets list the current target first, then
the other devices, and an empty entry only while no target is set, one more then), and without one
there are no entries, as for MasterAdjust: asked past its maximum, the CHG answers with whatever lies
beyond its table.

The entries of a list value are the texts behind attribute 0x26, those of a command value the texts
behind attribute 0x0F. Some firmware answers for texts it has none for with junk: the CHG (44010500)
with ids it then never answers a read of, the APR with its serial number's id. The texts stop at the
first such entry, whether the list is read with its page or on its own, as for a write. An empty text
is an entry (the shunt's config lock lists '' first), but empty ones at the end are dropped. A text the
device does not answer for ends the list too.

The entries of a device value are the addresses behind attribute 0x20, as str() writes them, 'none'
where the entry is empty: a high field above 0x1F, or an entry the device calls not available, which
MasterAdjust counts as one. An answer that is no address ends them.

The list is empty where the device lists none. Raise TimeoutError if the device does not answer.

<a id="mvconnect.pages.PageReader.group_length"></a>

#### group\_length

```python
def group_length(page: Page, index: int) -> int | None
```

Return how many values the group at *index* of *page* has.

The length is the group's own value, read as a value is but on the group kind. MasterAdjust reads it
before the members. Return None if the device gives none, or a length that is NaN, negative or above
256. Raise TimeoutError if the device does not answer.

<a id="mvconnect.pages.PageReader.read_group"></a>

#### read\_group

```python
def read_group(page: Page, index: int, n_values: int | None = None) -> Group
```

Return the Group at *index* of *page*: its name and value indices.

The group holds as many members as its length says or, where a device has no length to give, the
members until 'not available', at most 256 since the member number is one byte. Some firmware answers
beyond the end with garbage, so an index out of range (at or above *n_values*) or seen before ends the
group too. Raise TimeoutError if the device does not answer.

<a id="mvconnect.pages.PageReader.page_version"></a>

#### page\_version

```python
def page_version(page: Page) -> int | None
```

Return the version number of *page*, or None where the device has none to give.

Raise TimeoutError when it does not answer at all: that it has no version is not known then.

<a id="mvconnect.pages.PageReader.read_page"></a>

#### read\_page

```python
def read_page(page: Page, progress: Progress | None = None) -> PageTable
```

Return the PageTable of *page*: every value and group, names and attributes included.

This is slow: it takes many round trips. *progress*, if given, is called with the values and groups
read so far and the total. Firmware that counts more values than it has and answers junk for the
phantoms is cut at the first two junk values in a row. A version number the device does not answer
for is kept as None.

Raise TimeoutError if the device does not answer, NotAvailable (a TimeoutError) or ValueError if it
gives no number of values or groups.

<a id="mvconnect.pages.table_to_dict"></a>

#### table\_to\_dict

```python
def table_to_dict(table: PageTable) -> dict
```

Return *table* as a dict of plain values, for JSON.

<a id="mvconnect.pages.table_from_dict"></a>

#### table\_from\_dict

```python
def table_from_dict(d: dict) -> PageTable
```

Return the PageTable the dict *d*, from table_to_dict(), describes.

