<a id="mvconnect.cache"></a>

# mvconnect.cache

A cache of the static part of a device's pages: names, units, groups, options.

Keyed by the device's serial number and firmware version, under
$XDG_CACHE_HOME/mvconnect (default ~/.cache/mvconnect). Values are never
cached; only what the device's firmware defines.

<a id="mvconnect.cache.cache_dir"></a>

#### cache\_dir

```python
def cache_dir() -> Path
```

Return the cache directory: $XDG_CACHE_HOME/mvconnect, or ~/.cache/mvconnect.

<a id="mvconnect.cache.FORMAT"></a>

#### FORMAT

bumped when the tables change: 2 = option texts of device and command values, 3 = gray,

<a id="mvconnect.cache.load_tables"></a>

#### load\_tables

```python
def load_tables(serial: str, version: str) -> dict[str, PageTable] | None
```

Return the cached page tables of a device, by page name.

Return None if there is no readable cache for its *serial* number and firmware *version*; an unreadable
one is logged as a warning.

<a id="mvconnect.cache.save_tables"></a>

#### save\_tables

```python
def save_tables(serial: str, version: str, tables: dict[str,
                                                        PageTable]) -> None
```

Write the page *tables* of a device to the cache, keyed by its *serial* number and firmware *version*.

The file is written whole or not at all: two commands saving at once, or one stopped half way, never
leave half a file. A cache that cannot be written is logged and skipped.

