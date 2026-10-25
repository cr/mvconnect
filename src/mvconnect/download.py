"""Mastervolt's firmware bundle: find it, fetch it when it changed, unpack it.

Mastervolt lists all MasterBus firmware as one zip on its downloads page,
protected by the MasterAdjust installer password. Its hosts sit behind
Cloudflare, which refuses ordinary HTTP clients by their TLS fingerprint
and answers HEAD with 403 even to browsers; a GET through a browser-like
TLS session with a Referer is served, with ETag and Last-Modified, so a
download is skipped when the server says the file is unchanged.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

from mvconnect import cache

logger = logging.getLogger(__name__)

# Both official pages carry the "MasterBus Firmware" bundle link: the MasterAdjust product page
# (its Downloads section, next to the MasterAdjust setup) and Downloads > Software.
DOWNLOADS_PAGES = ("https://www.mastervolt.com/products/miscellaneous3/masteradjust-software/",
                   "https://www.mastervolt.com/downloads/so/")
DOWNLOADS_PAGE = DOWNLOADS_PAGES[0]
IMPERSONATE = "chrome"


class DownloadError(Exception):
    """The firmware bundle cannot be found, downloaded or unpacked."""


def cache_dir() -> Path:
    """Return the firmware cache directory, $XDG_CACHE_HOME/mvconnect/firmware.

    It holds the bundles as downloaded, each unpacked into a folder of its own name. A bundle downloaded again
    replaces its zip and, unpacked again, its folder; other bundles are never deleted, so firmware versions
    collect over time.
    """
    return cache.cache_dir() / "firmware"


def bundle_dir(bundle: Path) -> Path:
    """Return the directory a bundle unpacks into: beside it, under its own name without the extension."""
    return bundle.with_suffix("")


def _session():
    try:
        from curl_cffi import requests
    except ImportError as e:
        raise DownloadError("the curl_cffi package is needed to download from Mastervolt") from e
    return requests.Session(impersonate=IMPERSONATE)


def _get(session, url: str, **options):
    """Return session.get(url, **options).

    Raise DownloadError for whatever the HTTP library raises when the network fails: offline, a name that
    does not resolve, a refused connection.
    """
    try:
        return session.get(url, **options)
    except Exception as e:  # curl_cffi's error classes differ between its versions; any of them is no download
        raise DownloadError(f"{url}: {e}") from e


def find_bundle_url(page_html: str) -> str | None:
    """Return the URL of the firmware zip in *page_html*, or None if the page has none.

    The URL is the first .zip link after 'MasterBus Firmware'.
    """
    i = page_html.find("MasterBus Firmware")
    if i < 0:
        return None
    m = re.search(r'href="([^"]+\.zip)"', page_html[i:])
    return m.group(1) if m else None


def bundle_url(session=None) -> str:
    """Return the URL of the current firmware bundle, from the first official page that lists it.

    *session* is the HTTP session to use; a new browser-like one if not given. Raise DownloadError if no page
    lists it, or if the network fails.
    """
    session = session or _session()
    problems = []
    for page in DOWNLOADS_PAGES:
        r = _get(session, page, timeout=30)
        if r.status_code != 200:
            problems.append(f"{page}: HTTP {r.status_code}")
            continue
        url = find_bundle_url(r.text)
        if url:
            return url
        problems.append(f"{page}: no MasterBus Firmware link")
    raise DownloadError("; ".join(problems))


@dataclass
class Fetched:
    """The result of fetch().

    It holds the file's *path*, whether it was *updated* (False when the server said the cached copy is
    current), its *size* in bytes and the server's *last_modified* date.
    """
    path: Path
    updated: bool
    size: int
    last_modified: str


def fetch(url: str, directory: Path, session=None, progress=None) -> Fetched:
    """Download *url* into *directory* and return a Fetched.

    The download is skipped when the server reports the cached copy current. *session* is the HTTP session
    to use; a new browser-like one if not given. *progress*, if given, is called with the bytes done and the
    total. Raise DownloadError on any HTTP status other than 200 or 304, when the network fails or the
    download breaks off, and when a file cannot be written.
    """
    session = session or _session()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise DownloadError(f"cannot make the firmware cache {directory}: {e}") from e
    name = url.rsplit("/", 1)[-1]
    target = directory / name
    stamp = directory / (name + ".json")
    headers = {"Referer": DOWNLOADS_PAGE}
    known = {}
    if target.exists() and stamp.exists():
        try:
            known = json.loads(stamp.read_text())
        except ValueError:
            known = {}
        if known.get("etag"):
            headers["If-None-Match"] = known["etag"]
        if known.get("last_modified"):
            headers["If-Modified-Since"] = known["last_modified"]
    r = _get(session, url, headers=headers, timeout=120, stream=True)
    if r.status_code == 304:
        r.close()
        logger.info(f"{name}: unchanged on the server")
        return Fetched(target, False, target.stat().st_size, known.get("last_modified", ""))
    if r.status_code != 200:
        r.close()
        raise DownloadError(f"{url}: HTTP {r.status_code}")
    total = int(r.headers.get("content-length") or 0)
    tmp = directory / (name + ".part")
    done = 0
    try:
        f = open(tmp, "wb")
    except OSError as e:
        r.close()
        raise DownloadError(f"cannot write {tmp}: {e}") from e
    try:
        with f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total or done)
    except Exception as e:  # the connection dropped part way: the HTTP library's error, whichever it is
        tmp.unlink(missing_ok=True)
        raise DownloadError(f"{url}: the download broke off after {done} bytes: {e}") from e
    finally:
        r.close()
    meta = {"url": url, "etag": r.headers.get("etag", ""), "last_modified": r.headers.get("last-modified", ""),
            "size": done}
    try:
        tmp.replace(target)
        stamp.write_text(json.dumps(meta))
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise DownloadError(f"cannot keep the download as {target}: {e}") from e
    logger.info(f"{name}: downloaded {done} bytes")
    return Fetched(target, True, done, meta["last_modified"])


KEEP = (".hex", ".txt", ".pdf", ".czfwp")


def extract(bundle: Path, directory: Path, password: str | None) -> list[Path]:
    """Unpack the firmware files of the zip *bundle* into *directory* and return the paths of the .hex files.

    The notes beside the firmware are unpacked too. The archive's folders are kept, since they name the
    product families and the note files beside the firmware map product numbers to them; its top-level folder
    is dropped. *password* is the MasterAdjust installer password. Raise DownloadError if the file is not a
    zip, a member cannot be extracted or a file cannot be written.

    The files are unpacked beside *directory* and take its place only when every one is out, so *directory*
    holds a whole bundle or what it held before, never part of one, whatever stops the unpacking.
    """
    part = directory.with_name(directory.name + ".part")
    old = directory.with_name(directory.name + ".old")
    try:
        if old.is_dir() and not directory.exists():  # an earlier swap stopped between its two renames
            old.rename(directory)
        shutil.rmtree(part, ignore_errors=True)
        shutil.rmtree(old, ignore_errors=True)
        out = _unpack(bundle, part, password)
        if directory.exists():
            directory.rename(old)
        try:
            part.rename(directory)
        except BaseException:
            if old.exists():
                old.rename(directory)
            raise
        shutil.rmtree(old, ignore_errors=True)
    except OSError as e:
        raise DownloadError(f"cannot unpack {bundle.name} into {directory}: {e}") from e
    finally:
        shutil.rmtree(part, ignore_errors=True)
    logger.info(f"{bundle.name}: {len(out)} firmware files in {directory}")
    return [directory / p.relative_to(part) for p in out]


def _unpack(bundle: Path, directory: Path, password: str | None) -> list[Path]:
    """Unpack the firmware files of *bundle* into *directory*, as extract() describes, and return their paths."""
    directory.mkdir(parents=True, exist_ok=True)
    out = []
    try:
        zf = zipfile.ZipFile(bundle)
    except zipfile.BadZipFile as e:
        raise DownloadError(f"{bundle.name}: {e}") from e
    with zf:
        pwd = password.encode() if password else None
        for info in zf.infolist():
            if info.is_dir() or not info.filename.lower().endswith(KEEP):
                continue
            parts = Path(info.filename).parts
            if ".." in parts or Path(info.filename).is_absolute():
                continue
            rel = Path(*parts[1:]) if len(parts) > 1 else Path(parts[0])
            dest = directory / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                data = zf.read(info, pwd=pwd)
            except RuntimeError as e:
                raise DownloadError(f"{bundle.name}: cannot extract {info.filename}: {e}") from e
            except (zipfile.BadZipFile, zlib.error, EOFError) as e:  # a damaged member, or a wrong password
                raise DownloadError(f"{bundle.name}: cannot extract {info.filename}: {e}") from e
            except NotImplementedError as e:
                raise DownloadError(f"{bundle.name}: {info.filename} uses an unsupported compression or "
                                    f"encryption: {e}") from e
            dest.write_bytes(data)
            if dest.suffix.lower() == ".hex":
                out.append(dest)
    return out
