<a id="mvconnect.hidlink"></a>

# mvconnect.hidlink

The MasterBus USB Link as a USB HID device.

The link is a composite USB device with one HID interface (usage page 1,
usage 0x3A), 64-byte input and output reports and no report ids. Every
report carries up to four CAN records, see frame.py. Reports from the
link also tell how many transmit slots it has free; the host must not
send more records than that.

<a id="mvconnect.hidlink.VENDOR_ID"></a>

#### VENDOR\_ID

Mastervolt International B.V.

<a id="mvconnect.hidlink.PRODUCT_ID"></a>

#### PRODUCT\_ID

MasterBus USB Link

<a id="mvconnect.hidlink.REPORT_ID"></a>

#### REPORT\_ID

the interface has no report ids; HID APIs still want the byte in front

<a id="mvconnect.hidlink.USAGE_PAGE"></a>

#### USAGE\_PAGE

<a id="mvconnect.hidlink.USAGE"></a>

#### USAGE

<a id="mvconnect.hidlink.LIBUSB_PATH"></a>

#### LIBUSB\_PATH

<a id="mvconnect.hidlink.SYSFS_USB"></a>

#### SYSFS\_USB

<a id="mvconnect.hidlink.SILENCE"></a>

#### SILENCE

seconds without a report from the link before saying so

<a id="mvconnect.hidlink.LinkInfo"></a>

## LinkInfo

```python
@dataclass(frozen=True)
class LinkInfo()
```

One MasterBus USB Link as enumerated.

It holds the link's HID *path*, its *serial* number, and its *product* and *manufacturer* strings; a string
hidapi does not give is empty.

<a id="mvconnect.hidlink.LinkInfo.path"></a>

#### path: `bytes`

<a id="mvconnect.hidlink.LinkInfo.serial"></a>

#### serial: `str`

<a id="mvconnect.hidlink.LinkInfo.product"></a>

#### product: `str`

<a id="mvconnect.hidlink.LinkInfo.manufacturer"></a>

#### manufacturer: `str`

<a id="mvconnect.hidlink.find_links"></a>

#### find\_links

```python
def find_links() -> list[LinkInfo]
```

Return a LinkInfo for every MasterBus USB Link plugged in.

What else was seen, and whether this user may open each link, is logged at debug level.

<a id="mvconnect.hidlink.find_link"></a>

#### find\_link

```python
def find_link() -> LinkInfo
```

Return the MasterBus USB Link to use.

That is the only one plugged in, or the first one found, with a warning naming the others. Raise LinkError
if there is none.

<a id="mvconnect.hidlink.describe_link"></a>

#### describe\_link

```python
def describe_link(link: LinkInfo) -> str
```

Return *link* as `mvconnect link` shows it.

The text holds its product, serial number and manufacturer as it names them, and its path. A link whose
strings this user cannot read is said to be one, as libusb leaves them empty then.

<a id="mvconnect.hidlink.LinkError"></a>

## LinkError

```python
class LinkError(Exception)
```

The link cannot be found, opened, written to or read.

<a id="mvconnect.hidlink.HidLink"></a>

## HidLink

```python
class HidLink()
```

One open MasterBus USB Link.

Create one for *info*, or for the first link found when open() is called without one. A reader thread
turns every input report into frames and hands them to the listener given to open(); send() respects the
link's transmit credits.

<a id="mvconnect.hidlink.HidLink.open"></a>

#### open

```python
def open(listener: Callable[[Frame], None]) -> HidLink
```

Open the link, start the reader thread and return the link.

The reader thread hands every frame received to *listener*. Without a LinkInfo, the first link found
is opened, with a warning when there are several, and found again when a closed link is opened again:
plugged in again, it may be at another path. Raise LinkError if no link is found or it cannot be
opened.

<a id="mvconnect.hidlink.HidLink.close"></a>

#### close

```python
def close() -> None
```

Stop the reader thread and close the device.

Closing a closed link does nothing.

<a id="mvconnect.hidlink.HidLink.send"></a>

#### send

```python
def send(*frames: Frame, timeout: float = 1.0) -> None
```

Send *frames* in reports of up to four, waiting for transmit credits.

A report carries no more frames than the link has credits for. The wait for credits for all the
reports together lasts up to *timeout* seconds (1.0 by default). Raise LinkError if the link is not
open or has failed, the credits do not come or the write fails.

<a id="mvconnect.hidlink.FrameQueue"></a>

## FrameQueue

```python
class FrameQueue()
```

A listener that queues the frames it is given for a consumer, with timeouts.

<a id="mvconnect.hidlink.FrameQueue.get"></a>

#### get

```python
def get(timeout: float) -> Frame | None
```

Return the next frame, waiting up to *timeout* seconds, or None if none arrives.

<a id="mvconnect.hidlink.FrameQueue.drain"></a>

#### drain

```python
def drain() -> None
```

Discard every queued frame.

<a id="mvconnect.hidlink.FrameQueue.until"></a>

#### until

```python
def until(deadline: float) -> Iterator[Frame]
```

Yield the frames arriving until *deadline*, a time.monotonic() value, and then those queued by then.

A frame that came in time is not lost to a consumer that looks late.

