<a id="mvconnect.download"></a>

# mvconnect.download

Mastervolt's firmware bundle: find it, fetch it when it changed, unpack it.

Mastervolt lists all MasterBus firmware as one zip on its downloads page,
protected by the MasterAdjust installer password. Its hosts sit behind
Cloudflare, which refuses ordinary HTTP clients by their TLS fingerprint
and answers HEAD with 403 even to browsers; a GET through a browser-like
TLS session with a Referer is served, with ETag and Last-Modified, so a
download is skipped when the server says the file is unchanged.

<a id="mvconnect.download.DOWNLOADS_PAGES"></a>

#### DOWNLOADS\_PAGES

<a id="mvconnect.download.DOWNLOADS_PAGE"></a>

#### DOWNLOADS\_PAGE

<a id="mvconnect.download.IMPERSONATE"></a>

#### IMPERSONATE

<a id="mvconnect.download.DownloadError"></a>

## DownloadError

```python
class DownloadError(Exception)
```

The firmware bundle cannot be found, downloaded or unpacked.

<a id="mvconnect.download.cache_dir"></a>

#### cache\_dir

```python
def cache_dir() -> Path
```

Return the firmware cache directory, $XDG_CACHE_HOME/mvconnect/firmware.

It holds the bundles as downloaded, each unpacked into a folder of its own name. A bundle downloaded again
replaces its zip and, unpacked again, its folder; other bundles are never deleted, so firmware versions
collect over time.

<a id="mvconnect.download.bundle_dir"></a>

#### bundle\_dir

```python
def bundle_dir(bundle: Path) -> Path
```

Return the directory a bundle unpacks into: beside it, under its own name without the extension.

<a id="mvconnect.download.find_bundle_url"></a>

#### find\_bundle\_url

```python
def find_bundle_url(page_html: str) -> str | None
```

Return the URL of the firmware zip in *page_html*, or None if the page has none.

The URL is the first .zip link after 'MasterBus Firmware'.

<a id="mvconnect.download.bundle_url"></a>

#### bundle\_url

```python
def bundle_url(session=None) -> str
```

Return the URL of the current firmware bundle, from the first official page that lists it.

*session* is the HTTP session to use; a new browser-like one if not given. Raise DownloadError if no page
lists it, or if the network fails.

<a id="mvconnect.download.Fetched"></a>

## Fetched

```python
@dataclass
class Fetched()
```

The result of fetch().

It holds the file's *path*, whether it was *updated* (False when the server said the cached copy is
current), its *size* in bytes and the server's *last_modified* date.

<a id="mvconnect.download.Fetched.path"></a>

#### path: `Path`

<a id="mvconnect.download.Fetched.updated"></a>

#### updated: `bool`

<a id="mvconnect.download.Fetched.size"></a>

#### size: `int`

<a id="mvconnect.download.Fetched.last_modified"></a>

#### last\_modified: `str`

<a id="mvconnect.download.fetch"></a>

#### fetch

```python
def fetch(url: str, directory: Path, session=None, progress=None) -> Fetched
```

Download *url* into *directory* and return a Fetched.

The download is skipped when the server reports the cached copy current. *session* is the HTTP session
to use; a new browser-like one if not given. *progress*, if given, is called with the bytes done and the
total. Raise DownloadError on any HTTP status other than 200 or 304, when the network fails or the
download breaks off, and when a file cannot be written.

<a id="mvconnect.download.KEEP"></a>

#### KEEP

<a id="mvconnect.download.extract"></a>

#### extract

```python
def extract(bundle: Path, directory: Path, password: str | None) -> list[Path]
```

Unpack the firmware files of the zip *bundle* into *directory* and return the paths of the .hex files.

The notes beside the firmware are unpacked too. The archive's folders are kept, since they name the
product families and the note files beside the firmware map product numbers to them; its top-level folder
is dropped. *password* is the MasterAdjust installer password. Raise DownloadError if the file is not a
zip, a member cannot be extracted or a file cannot be written.

The files are unpacked beside *directory* and take its place only when every one is out, so *directory*
holds a whole bundle or what it held before, never part of one, whatever stops the unpacking.

