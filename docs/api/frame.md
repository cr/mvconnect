<a id="mvconnect.frame"></a>

# mvconnect.frame

MasterBus CAN records as the MasterBus USB Link carries them.

One HID report of 64 payload bytes holds up to four 14-byte records:

    payload[0]        number of records, 1..4
    payload[1+14*i]   record i
    payload[58]       in reports from the link: free transmit slots in the link
    payload[59]       in reports from the link: status flags
    payload[60+i]     in reports from the link: low timestamp byte of record i,
                      whose own timestamp byte is then the high one

A record is a big-endian 32-bit id word, a data length, 8 data bytes and a
timestamp byte. The id word packs a 6-bit kind, a 5-bit high address field and
an 18-bit low address field:

    bits 31..26  kind
    bits 25..21  address high (5 bits)
    bits 20..18  unused
    bits 17..0   address low (18 bits)

<a id="mvconnect.frame.RECORD_SIZE"></a>

#### RECORD\_SIZE

<a id="mvconnect.frame.MAX_RECORDS"></a>

#### MAX\_RECORDS

<a id="mvconnect.frame.PAYLOAD_SIZE"></a>

#### PAYLOAD\_SIZE

<a id="mvconnect.frame.KIND_ANNOUNCE"></a>

#### KIND\_ANNOUNCE

reply to ENUMERATE: data = hi, lo (3 bytes LE), 4 info bytes

<a id="mvconnect.frame.KIND_ENUMERATE"></a>

#### KIND\_ENUMERATE

DLC 0 broadcast: every device answers with ANNOUNCE

<a id="mvconnect.frame.KIND_REPLY"></a>

#### KIND\_REPLY

reply to a REQUEST, same command byte in data[0]

<a id="mvconnect.frame.KIND_NA"></a>

#### KIND\_NA

"not available" reply to a REQUEST

<a id="mvconnect.frame.KIND_REQUEST"></a>

#### KIND\_REQUEST

data[0] = command, see masterbus.py

<a id="mvconnect.frame.LOGIN"></a>

#### LOGIN

<a id="mvconnect.frame.Address"></a>

## Address

```python
@dataclass(frozen=True, order=True)
class Address()
```

A MasterBus device address: a 5-bit high field and an 18-bit low field.

Raise ValueError if either field is out of range.

<a id="mvconnect.frame.Address.hi"></a>

#### hi: `int`

<a id="mvconnect.frame.Address.lo"></a>

#### lo: `int`

<a id="mvconnect.frame.Address.parse"></a>

#### parse

```python
@classmethod
def parse(cls, text: str) -> Address
```

Return the Address *text* names, written as str() writes it: 'hh:lllll' in hex.

Raise ValueError if *text* is not of that form or a field is out of range.

<a id="mvconnect.frame.Frame"></a>

## Frame

```python
@dataclass
class Frame()
```

One CAN record.

It holds its 6-bit *kind*, the *address* of the device it is for or from, up to 8 *data* bytes and a
*timestamp*, one byte in a record and two in a report from the link (unpack_report() adds the low byte).
Raise ValueError if the kind or the data length is out of range.

<a id="mvconnect.frame.Frame.kind"></a>

#### kind: `int`

<a id="mvconnect.frame.Frame.address"></a>

#### address: `Address`

<a id="mvconnect.frame.Frame.data"></a>

#### data: `bytes`

<a id="mvconnect.frame.Frame.timestamp"></a>

#### timestamp: `int`

<a id="mvconnect.frame.Frame.secret"></a>

#### secret

```python
@property
def secret() -> bool
```

Return whether the frame carries a login's password: a login request, or a reply echoing one.

<a id="mvconnect.frame.Frame.data_text"></a>

#### data\_text

```python
def data_text() -> str
```

Return the data bytes in hex, a login's password as ** for each of its bytes.

<a id="mvconnect.frame.Frame.word"></a>

#### word

```python
@property
def word() -> int
```

Return the 32-bit id word: the kind and the address packed as the link carries them.

<a id="mvconnect.frame.Frame.pack"></a>

#### pack

```python
def pack() -> bytes
```

Return the 14 bytes of the record as the link carries it.

<a id="mvconnect.frame.Frame.unpack"></a>

#### unpack

```python
@classmethod
def unpack(cls, record: bytes) -> Frame
```

Return the Frame a 14-byte *record* holds.

Raise ValueError if the record is short or its data length exceeds 8.

<a id="mvconnect.frame.Report"></a>

## Report

```python
@dataclass
class Report()
```

The decoded content of one report from the link.

It holds the report's *frames*, the link's count of free transmit slots (*credits*) and its status *flags*
byte.

<a id="mvconnect.frame.Report.frames"></a>

#### frames: `list[Frame]`

<a id="mvconnect.frame.Report.credits"></a>

#### credits: `int`

<a id="mvconnect.frame.Report.flags"></a>

#### flags: `int`

<a id="mvconnect.frame.pack_report"></a>

#### pack\_report

```python
def pack_report(frames: list[Frame]) -> bytes
```

Return the 64 payload bytes of an output report carrying *frames*, 1 to 4 of them.

Raise ValueError for any other number of frames.

<a id="mvconnect.frame.unpack_report"></a>

#### unpack\_report

```python
def unpack_report(payload: bytes) -> Report
```

Return the Report decoded from *payload*, the 64 payload bytes of an input report.

Slots beyond the record count hold stale data and are ignored. Each frame's timestamp gets its low byte
from the report, the record's own byte becoming the high one. Raise ValueError if the payload is short,
the count exceeds 4 or a record's data length exceeds 8.

