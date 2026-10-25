# The server's bus state: the open link and the identified devices

Implemented 2026-10-09 in `src/mvconnect/state.py`, cut down 2026-10-10.

## Why

A command that opens the link itself pays, every time, for the listen
window for the announcements, the identification of each device and the
page version reads before it reads anything of its own: tens of seconds.
The server pays it once. Since most values on this bus have to be asked
for anyway (see below), there is nothing else worth holding: a value is a
request and a reply, a few milliseconds, read when a command asks.

## The seam

Everything the command line does goes through `mvconnect.system.System`. Ten
of its methods talk to the bus: devices, traffic, login_level, login,
tables, read_values, read_options, read_text, write_value and write_text.
The rest (device, locate, find, find_selection, read_texts, write,
write_default, resolve_value and the lookups) are built on those. The
server holds one `System` on the link, with the devices identified, and
serves the ten over a Unix socket, one request on the bus at a time
(`StateServer`). `StateSystem` is a `System` whose ten go to the socket;
everything else runs in the command itself. A value or text request carries the page's
name only, so the server reads exactly that value and nothing else. The
server sets `MVCONNECT_STATE` to the socket for the commands it runs;
`open_system()` uses the state when that variable is set and the link
otherwise, and a state that does not answer is an error, not a fallback.

Pages come from the disk cache, checked against their version numbers on
every use, so a page that changed on the device, its writeable flags
after a lock for one, is read again. The devices are enumerated again
every few minutes, so one plugged in later appears.

## What the bus publishes, and why nothing is mirrored

Devices broadcast only their eventable values (attribute 0x0d, a chosen
few per device, kind 0x00 frames); everything else is request and reply,
and the EasyView polls what it shows. A mirror of the values would have to
poll too, which is what a command does anyway when asked, so the server
does not keep one.

## The link has one owner

While the server runs, nothing else can open the link. That is expected
and accepted: on that computer, run commands through the server.

## Where it runs

`mvconnect server` (fireaid's remote server) opens the link in a thread
before serving and points the commands it runs at the state, so they skip
the listen window and the identification; they run as fresh processes,
which is what keeps colour and terminals right, and the Python start-up
of each is what a remote command still costs. For a local user:
`mvconnect server --bind 127.0.0.1` and `MVCONNECT_REMOTE=localhost`.

## Protocol

Newline-delimited JSON on the socket. A request is `{"method": name,
"params": {...}}`; the reply `{"result": ...}` or `{"error": {"type":
name, "message": text}}`, the type being one of AddressError, TimeoutError,
ValueError, LinkError, re-raised as such by the client. A device travels
as its fields with the address as `hh:lllll`; a page table as
`pages.table_to_dict`; values as a map from index to float, NaN allowed.

What a command costs through the server: one `devices` request, one
`tables` request (the page version reads, the layout from the cache), and
one request per value read or written, each a single conversation on the
bus. Plus the Python start-up of the command's own process.
