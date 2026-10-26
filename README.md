# mvconnect

Talk to a Mastervolt MasterBus system from macOS or Linux through the
Mastervolt "MasterBus USB Link" (USB VID 0x1A64, PID 0x0000), the adapter
that Mastervolt's Windows-only MasterAdjust uses.

> **It is very easy to screw up your devices with low-level tooling like this, so**
> **BE EXTREMELY CAREFUL and get help from your geek friend if you're out of your depth.**
> **The software probably contains bugs that can permanently damage your devices.**
> **Although it has been used a lot on my personal MasterBus, I cannot guarantee**
> **that it works on your devices, too. Use it AT YOUR OWN RISK!**

The package `mvconnect` is a library and the command line built on it.
The library is the USB HID framing (`mvconnect.frame`, `mvconnect.hidlink`),
the MasterBus request/reply protocol (`mvconnect.masterbus`), a device's
property pages (`mvconnect.pages`), a cache of their static part
(`mvconnect.cache`) and the bus addressed by words (`mvconnect.system`);
the command line is `mvconnect.cli`.

The library reference, generated from the docstrings, is in
[docs/api](docs/api/README.md); `mvconnect --help` documents the command line.

mvconnect needs Python 3.11 or newer. Install it into an environment of
yours:

    pip install git+https://github.com/cr/mvconnect
    mvconnect data

To work on it, from a clone:

    python3 -m venv .venv && . .venv/bin/activate
    pip install -e '.[test,docs]'
    make test check docs

Things are named with plain words: a device by its address, its name or
its type (`msh`, `chg`, `dis`... when there is one of that type), then,
in any order, a page to look at that page only, and a value by its index
or its name:

    mvconnect data                             the devices and their selectors
    mvconnect data house bank                  every value of a device, grouped as the device groups them
    mvconnect data msh config 21               the one MasterShunt, by type
    mvconnect data 1b:0cad9 config             one page: monitor, alarm, history or config
    mvconnect data house bank config timers    the values of the Timers group
    mvconnect data house bank config 21        one value: its range or options, default, writeable
    mvconnect data house bank config date      every value of that name (day, month and year)
    mvconnect data house bank battery          every value called Battery..., on every page
    mvconnect data house bank battery low config   those called Battery low on the config page
    mvconnect data house bank battery low 22   the one of them at index 22
    mvconnect data house bank config 32 --set 7   write one value and read it back
    mvconnect data house bank config 32 --reset   write its default
    mvconnect login                            log in on every device as installer (asks for the password)
    mvconnect logout                           log out on every device
    mvconnect login --status                   the level each device is logged in at
    mvconnect monitor --filter msh,apr         log the bus traffic to and from those devices, decoded

The device is the longest run of leading words that names exactly one
device. Among the rest, a page keyword anywhere restricts the search to
that page, and the other words select the value. On either side a bare
selector or index wins; otherwise every word must equal the index or start
a word of the name, the long name or the group, so an index among the
words pins one value:
`batt cap` is Battery capacity, `house` the House Bank when it is the only
match. Every word is a selector word, so values called `HMA Reset` or
`Amp=Full` are reached like any other. Names repeat within a page, so the
index shown in brackets is the unambiguous handle. A group's name selects
its values, and a listing keeps the device's grouping, so `data apr config
alpha pro` shows the "Alpha pro" groups.

`--set` takes a number, an option name for a list or command value, or
on/off; a device value takes a device among its entries by its name, type
or address, or `none`; a text value the text; a date value a date written
`YYYY-MM-DD`. A listing shows a date's day behind its number, and a time's
seconds as a duration, as another MasterBus implementation documents them
and as the MasterShunt's Date and Time read. It checks the value against
the range and the writeable flag (`--force` skips both; a device value's
device must be among the entries, forced or not), writes it, and shows the
value before and after. `--reset` writes the default the device gives;
never to a button, which it would press, nor to a time or a date without
`--force`. Either needs the words to select exactly one value; otherwise
the matches are listed and nothing is written. An index alone is one value
per page, so a write by index names the page. The device checks the value
again and may refuse it.

A write to a switch, an alarm or a button or flag of range 0..1, is
followed by the frame that names this computer as the writer, as
MasterAdjust sends it and as a relay or button may need before it acts
(so another implementation found). The EasyView takes it; nothing else on
my bus has a place for it.

Firmware:

    mvconnect firmware fetch --password N      download Mastervolt's firmware bundle if it changed, unpack it
    mvconnect firmware list                    installed versions per device, and the newest file in the library
    mvconnect firmware info FILE.hex           what a Mastervolt firmware file says about itself
    mvconnect firmware library                 the files in ~/.cache/mvconnect/firmware (or --library DIR), by product

The bundle is the "MasterBus Firmware" zip from mastervolt.com/downloads,
protected by the MasterAdjust installer password; `fetch` finds the
current link on the downloads page and skips the download when the server
reports the file unchanged.

    mvconnect firmware flash msh               check the shunt against the newest MSH file: a dry run
    mvconnect firmware flash msh --version 2.1 ... against an older version from the library
    mvconnect firmware flash msh --really --erase-size N
                                               a rehearsal: the device enters flash mode FOR REAL

`flash` without `--really` queries the device, runs every compatibility
check, shows the plan and a warning, and writes nothing. With `--really`
it puts the device into flash mode for real and takes it out again, and
only logs the programming in between, frame by frame, without sending
it: no sector is erased and no byte is written. Entering flash mode is
still a real change of state, so rehearse on a device you can recover;
the device must stay powered and on the bus until it is out again. The
programming itself is not enabled in this build.

The commands need read and write access to the link's USB device. `mvconnect
link --debug` shows its device node and whether this user may open it;
`--debug` on any command also logs every HID device of Mastervolt's, the
report descriptor, each report in and out, and the transmit credits.

Every command opens the link, listens for the devices and identifies
them before it reads what it needs, which takes a while. `mvconnect
server` does that once and holds the link; the commands run through it
skip it. It owns the link while it runs, so on that computer run the
commands through it too. A server that cannot open the link says so,
and does not start:

    mvconnect server --bind 127.0.0.1
    MVCONNECT_REMOTE=localhost mvconnect data msh monitor

Whoever reaches the server may write to every device on the bus. A
server that other computers can reach warns when it starts without a
password, and runs: on a network of one's own that may be what is wanted.
With a password, which clients give before the host, the connection is
authenticated, but never encrypted, so keep the server to a network you
trust either way:

    MVCONNECT_SERVER_PASSWORD=... mvconnect server
    mvconnect --remote PASSWORD@boat.local data msh monitor

The firmware commands run on the computer the link is plugged into, never
through the server: a command line with the word `firmware` anywhere in it
is refused remotely, a value or device named so included; where
`MVCONNECT_REMOTE` is set, an empty `--remote=` runs a command here. Since
the server holds the link, stop it on that computer before running them.
Through the server a command does not hold the link, so its `--debug`
shows no frames: `mvconnect server --debug` logs them.

Logging and progress go to stderr, results to stdout. A device's
names, units and groups take a while to read the first time and are then
cached under `~/.cache/mvconnect` (`--nocache` reads them again and updates
the cache).

The protocol is what MasterAdjust 2.36 puts on the bus, as observed on the
link and verified against live devices: the device list, the pages and
their values, writes of numbers, options, texts and device targets, login
and logout, the monitor and the server, on a MasterShunt, a ChargeMaster, an
Alpha Pro and an EasyView. The firmware transfer is not, as said above. The
notes are in `notes/`.

mvconnect is free software under the GNU General Public License, version 3
([LICENSE.md](LICENSE.md)).
