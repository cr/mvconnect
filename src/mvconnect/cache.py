"""A cache of the static part of a device's pages: names, units, groups, options.

Keyed by the device's serial number and firmware version, under
$XDG_CACHE_HOME/mvconnect (default ~/.cache/mvconnect). Values are never
cached; only what the device's firmware defines.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from mvconnect.pages import PageTable, table_from_dict, table_to_dict

logger = logging.getLogger(__name__)


def cache_dir() -> Path:
    """Return the cache directory: $XDG_CACHE_HOME/mvconnect, or ~/.cache/mvconnect."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "mvconnect"


FORMAT = 7  # bumped when the tables change: 2 = option texts of device and command values, 3 = gray,
# eventable, long name and description, 4 = the page's version number, 5 = no junk option texts, 6 = 'values'
# where the tables said 'items', 7 = no entries of device and command values kept


def _path(serial: str, version: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in f"{serial}-{version}")
    return cache_dir() / f"{safe}.v{FORMAT}.json"


def load_tables(serial: str, version: str) -> dict[str, PageTable] | None:
    """Return the cached page tables of a device, by page name.

    Return None if there is no readable cache for its *serial* number and firmware *version*; an unreadable
    one is logged as a warning.
    """
    path = _path(serial, version)
    try:
        with open(path) as f:
            tables = {name: table_from_dict(t) for name, t in json.load(f)["pages"].items()}
    except FileNotFoundError:
        logger.debug(f"no cache for serial {serial!r} version {version!r} at {path}")
        return None
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:  # read again, and written anew
        logger.warning(f"ignoring unreadable cache {path}: {e!r}")
        return None
    logger.debug(f"cache {path}: pages {sorted(tables)}")
    return tables


def save_tables(serial: str, version: str, tables: dict[str, PageTable]) -> None:
    """Write the page *tables* of a device to the cache, keyed by its *serial* number and firmware *version*.

    The file is written whole or not at all: two commands saving at once, or one stopped half way, never
    leave half a file. A cache that cannot be written is logged and skipped.
    """
    path = _path(serial, version)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, part = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".part")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"pages": {name: table_to_dict(t) for name, t in tables.items()}}, f)
            os.replace(part, path)
        except BaseException:
            os.unlink(part)
            raise
        logger.debug(f"cached pages in {path}")
    except OSError as e:
        logger.warning(f"cannot write cache {path}: {e}")
