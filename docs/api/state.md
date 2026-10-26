<a id="mvconnect.state"></a>

# mvconnect.state

A server's bus state: the open link with its devices identified, served to the processes it starts.

start_in_thread() opens the link, identifies the devices once and serves
the bus on a Unix socket; the processes the server starts are told where
by STATE_VARIABLE, and open_system() gives them a StateSystem instead of
the link. See notes/state.md.

<a id="mvconnect.state.STREAM_TIMEOUT"></a>

#### STREAM\_TIMEOUT

seconds a monitor may leave its stream unread before the server lets it go

<a id="mvconnect.state.STATE_VARIABLE"></a>

#### STATE\_VARIABLE

the socket the server's state answers at, set for the commands it runs

<a id="mvconnect.state.ERRORS"></a>

#### ERRORS

<a id="mvconnect.state.socket_path"></a>

#### socket\_path

```python
def socket_path() -> Path
```

Return the path the server's state listens at: next to the cache, one per user.

<a id="mvconnect.state.encode_device"></a>

#### encode\_device

```python
def encode_device(d: Device) -> dict
```

Return Device *d* as a dict for JSON, its address as 'hh:lllll'.

<a id="mvconnect.state.decode_device"></a>

#### decode\_device

```python
def decode_device(d: dict) -> Device
```

Return the Device the dict *d*, from encode_device(), describes.

<a id="mvconnect.state.StateServer"></a>

## StateServer

```python
class StateServer()
```

A System on the open link, served on a Unix socket, one request on the bus at a time.

Create one serving *system* at *path* (socket_path() by default), enumerating the bus again every
*re_enumerate* seconds so that a device plugged in later appears, and listening *timeout* seconds for
announcements. The devices are identified once, so that a command skips the listen window.

<a id="mvconnect.state.StateServer.bind"></a>

#### bind

```python
def bind() -> None
```

Create the socket, replacing an earlier server's, and listen on it.

<a id="mvconnect.state.StateServer.serve_forever"></a>

#### serve\_forever

```python
def serve_forever() -> None
```

Accept clients and serve each in a thread until stop() is called.

Then close and remove the socket, end every client's connection and wait up to STREAM_TIMEOUT seconds
for each one's thread.

<a id="mvconnect.state.StateServer.stop"></a>

#### stop

```python
def stop() -> None
```

Have serve_forever() return, and wait for it when it runs in another thread.

The wait lasts up to 2 * STREAM_TIMEOUT seconds. A request still on the bus may outlast that; shutdown()
waits for it.

<a id="mvconnect.state.StateServer.shutdown"></a>

#### shutdown

```python
def shutdown() -> None
```

Stop serving, wait until the request on the bus, if any, is done, and close the link.

This is what a process that holds a server does before it exits, so that a write of several frames is
never cut between two of them. The client threads are daemons, which the end of the process would stop
wherever they are.

<a id="mvconnect.state.StateServer.handle"></a>

#### handle

```python
def handle(method: str, p: dict) -> Any
```

Carry out one request, *method* with its parameters *p*, on the system.

Return the result as plain values for JSON. Raise ValueError for an unknown method.

<a id="mvconnect.state.start_in_thread"></a>

#### start\_in\_thread

```python
def start_in_thread(path: Path | None = None,
                    use_cache: bool = True,
                    timeout: float = 1.5) -> StateServer
```

Open the link, identify the devices, serve them in a thread and return the StateServer.

Serve at *path* (socket_path() by default), with the page cache unless *use_cache* is False, listening
*timeout* seconds for the devices. Point the commands the server runs at it through STATE_VARIABLE. Raise
LinkError when any of that fails.

<a id="mvconnect.state.StateSystem"></a>

## StateSystem

```python
class StateSystem(System)
```

A System whose bus conversations go to the server's state; everything else runs here.

Create one on *sock*, connected to the server at *path*; connect() does. *refresh* is as for System. The
methods that talk to the bus (devices, traffic, login_level, login, tables, read_values, read_options,
read_text, write_value, write_text, name_writer) send a request to the server instead and return what it
answers. They take no *progress* to report: the server reads a page in one request, whose progress stays
there. listen() is not served: the server's traffic comes through traffic().

<a id="mvconnect.state.StateSystem.listen"></a>

#### listen

```python
def listen() -> tuple[queue.Queue, Callable[[], None], list[int]]
```

Raise LinkError: the bus is the server's, and its traffic comes through traffic().

<a id="mvconnect.state.StateSystem.close"></a>

#### close

```python
def close() -> None
```

Close the connection to the server; one already broken closes quietly.

<a id="mvconnect.state.StateSystem.call"></a>

#### call

```python
def call(method: str, **params: Any) -> Any
```

Send *method* with *params* to the server and return its result.

Raise the error the server reports, of the type it names (ValueError for a type not in ERRORS). Raise
LinkError if the connection is closed already, or if the server closed it, did not answer in time or
sent a reply that cannot be read; the connection is closed then, since a reply that comes later would
be taken for the next request's. It is closed too when anything else, Ctrl-C say, stops the call
between the request and its reply.

<a id="mvconnect.state.StateSystem.devices"></a>

#### devices

```python
def devices(timeout: float = 1.5,
            progress=None,
            refresh: bool = False) -> list[Device]
```

Ask the server for the devices; see System.devices(). *progress* is not used.

<a id="mvconnect.state.StateSystem.traffic"></a>

#### traffic

```python
def traffic() -> Iterator[tuple[float, Frame, bool]]
```

Yield the server's bus traffic, streamed on this connection, which serves nothing else after.

Each frame comes as in System.traffic(). Raise LinkError when the stream ends, as the server stops or
lets a reader go that fell behind, or when a line of it comes cut off or unreadable; the server's own
failure is raised as it names it.

<a id="mvconnect.state.StateSystem.login_level"></a>

#### login\_level

```python
def login_level(device: Device) -> int
```

Ask the server; see System.login_level().

<a id="mvconnect.state.StateSystem.login"></a>

#### login

```python
def login(device: Device,
          level: int,
          password: float = 0.0) -> tuple[bool, int | None]
```

Ask the server to log *device* in or out; see System.login().

<a id="mvconnect.state.StateSystem.tables"></a>

#### tables

```python
def tables(device: Device,
           pages: list[Page] | None = None,
           progress=None,
           afresh=None) -> dict[str, PageTable]
```

Ask the server for the page tables, one page per request; see System.tables().

*progress* is not used: the server reads a page in one request. A page read waits as long as it takes,
whatever the connection's timeout.

<a id="mvconnect.state.StateSystem.read_values"></a>

#### read\_values

```python
def read_values(device: Device, table: PageTable,
                indices: list[int]) -> dict[int, float | None]
```

Ask the server; see System.read_values().

<a id="mvconnect.state.StateSystem.read_options"></a>

#### read\_options

```python
def read_options(device: Device, table: PageTable, value: Value) -> list[str]
```

Ask the server; see System.read_options().

<a id="mvconnect.state.StateSystem.read_text"></a>

#### read\_text

```python
def read_text(device: Device, table: PageTable,
              index: int) -> tuple[int | None, str]
```

Ask the server; see System.read_text().

<a id="mvconnect.state.StateSystem.write_value"></a>

#### write\_value

```python
def write_value(device: Device, table: PageTable, index: int,
                value: float) -> float
```

Ask the server; see System.write_value().

<a id="mvconnect.state.StateSystem.write_text"></a>

#### write\_text

```python
def write_text(device: Device, table: PageTable, index: int, text: str) -> str
```

Ask the server; see System.write_text().

<a id="mvconnect.state.StateSystem.name_writer"></a>

#### name\_writer

```python
def name_writer(device: Device, table: PageTable, index: int) -> bool | None
```

Ask the server; see System.name_writer().

<a id="mvconnect.state.connect"></a>

#### connect

```python
def connect(path: Path,
            timeout: float | None = None,
            refresh: bool = False) -> StateSystem
```

Return a StateSystem connected to the server's socket at *path*.

It waits up to *timeout* seconds for each reply, as long as it takes by default: the server answers when the
bus is free, after another command's page read if need be, and one that dies closes the connection. With
*refresh*, the server is asked to read each page afresh once. A page read waits as long as it takes,
whatever *timeout* says. Raise LinkError if nothing listens there.

<a id="mvconnect.state.open_system"></a>

#### open\_system

```python
@contextmanager
def open_system(refresh: bool = False) -> Iterator[System]
```

Yield the System for a command: the server's state, or else the link itself.

The server's state serves when the server runs the command and says where it is (STATE_VARIABLE); else the
link is opened for the block. Either is closed when the block ends. With *refresh*, page layouts are read
from the devices again and the cache updated (see System).

