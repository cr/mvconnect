<a id="mvconnect.decode"></a>

# mvconnect.decode

What a frame means, as far as it is known: the monitor's reading of the bus.

describe() turns one frame into words; line() lays a frame out as one line of
a listing: when, which way, the raw kind, address and data bytes, the node's
name, then the meaning. A login's password is never shown, neither as bytes
nor in words.

<a id="mvconnect.decode.KIND_EVENT"></a>

#### KIND\_EVENT

an eventable value, broadcast by the device: [idx16, float32]

<a id="mvconnect.decode.IDENTITY"></a>

#### IDENTITY: `dict[int, str]`

<a id="mvconnect.decode.LOADER"></a>

#### LOADER: `dict[int, str]`

<a id="mvconnect.decode.NUMBERS"></a>

#### NUMBERS: `dict[int, str]`

<a id="mvconnect.decode.ATTRS"></a>

#### ATTRS: `dict[int, str]`

<a id="mvconnect.decode.FLOAT_ATTRS"></a>

#### FLOAT\_ATTRS

<a id="mvconnect.decode.SUB_ATTRS"></a>

#### SUB\_ATTRS

<a id="mvconnect.decode.describe"></a>

#### describe

```python
def describe(frame: Frame) -> str
```

Return what *frame* means, as far as it is known; '?' where it is not.

<a id="mvconnect.decode.raw"></a>

#### raw

```python
def raw(frame: Frame) -> str
```

Return the data bytes of *frame* in hex, a login's password masked (see Frame.data_text()).

<a id="mvconnect.decode.line"></a>

#### line

```python
def line(frame: Frame, sent: bool, elapsed: float, names: dict[Address,
                                                               str]) -> str
```

Return *frame* as one line of a listing.

The line holds *elapsed*, the seconds since the start; tx if this computer sent the frame (*sent*), rx if
not; the kind, address and data; the node's name from *names*, which maps addresses to names; and the
meaning, as describe() gives it.

