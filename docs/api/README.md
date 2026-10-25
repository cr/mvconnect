# mvconnect library reference

Generated from the docstrings by `make docs`; do not edit. The command line, `mvconnect`, is documented by `mvconnect --help`.

- [`mvconnect.system`](system.md): The bus as devices, pages and values, addressed by words
- [`mvconnect.words`](words.md): Selection of devices and values by words
- [`mvconnect.pages`](pages.md): A device's property pages: monitor, alarm, history, configuration
- [`mvconnect.masterbus`](masterbus.md): The MasterBus request/reply protocol, as far as it is understood
- [`mvconnect.frame`](frame.md): MasterBus CAN records as the MasterBus USB Link carries them
- [`mvconnect.hidlink`](hidlink.md): The MasterBus USB Link as a USB HID device
- [`mvconnect.cache`](cache.md): A cache of the static part of a device's pages: names, units, groups, options
- [`mvconnect.state`](state.md): A server's bus state: the open link with its devices identified, served to the processes it starts
- [`mvconnect.decode`](decode.md): What a frame means, as far as it is known: the monitor's reading of the bus
- [`mvconnect.firmware`](firmware.md): Firmware files and the bootloader's identification queries
- [`mvconnect.download`](download.md): Mastervolt's firmware bundle: find it, fetch it when it changed, unpack it
- [`mvconnect.preflight`](preflight.md): What has to be true before a device may be flashed; read-only
- [`mvconnect.flasher`](flasher.md): The bootloader transfer protocol: how a firmware image is written to a device
