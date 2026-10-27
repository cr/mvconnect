"""A server's bus state: the open link with its devices identified, served to the processes it starts.

start_in_thread() opens the link, identifies the devices once and serves
the bus on a Unix socket; the processes the server starts are told where
by STATE_VARIABLE, and open_system() gives them a StateSystem instead of
the link. See notes/state.md.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from mvconnect import cache
from mvconnect.frame import Address, Frame
from mvconnect.hidlink import LinkError
from mvconnect.masterbus import Device, MasterBus, login_level_name, winding_down
from mvconnect.pages import PAGE_BY_NAME, PAGES, Page, PageTable, Value, table_from_dict, table_to_dict
from mvconnect.system import AddressError, System, check_identity

logger = logging.getLogger(__name__)

STREAM_TIMEOUT = 10.0  # seconds a monitor may leave its stream unread before the server lets it go
STATE_VARIABLE = "MVCONNECT_STATE"  # the socket the server's state answers at, set for the commands it runs
ERRORS = {"AddressError": AddressError, "TimeoutError": TimeoutError, "ValueError": ValueError,
          "LinkError": LinkError}


def socket_path() -> Path:
    """Return the path the server's state listens at: next to the cache, one per user."""
    return cache.cache_dir() / "state.sock"


def encode_device(d: Device) -> dict:
    """Return Device *d* as a dict for JSON, its address as 'hh:lllll'."""
    return {"address": str(d.address), "info": d.info.hex(), "product": d.product, "serial": d.serial,
            "product_name": d.product_name, "revision": d.revision, "version": d.version}


def decode_device(d: dict) -> Device:
    """Return the Device the dict *d*, from encode_device(), describes."""
    return Device(Address.parse(d["address"]), info=bytes.fromhex(d.get("info", "")), product=d.get("product", ""),
                  serial=d.get("serial", ""), product_name=d.get("product_name", ""),
                  revision=d.get("revision", ""), version=d.get("version", ""))


# -- the server side


class StateServer:
    """A System on the open link, served on a Unix socket, one request on the bus at a time.

    Create one serving *system* at *path* (socket_path() by default), enumerating the bus again every
    *re_enumerate* seconds so that a device plugged in later appears, and listening *timeout* seconds for
    announcements. The devices are identified once, so that a command skips the listen window. A link that
    fails under the server, unplugged say, is looked for every *reopen* seconds and opened again; a request
    meanwhile is a LinkError that says the link is lost.
    """

    def __init__(self, system: System, path: Path | None = None, re_enumerate: float = 300.0, timeout: float = 1.5,
                 reopen: float = 2.0):
        self.system = system
        self.path = path or socket_path()
        self.re_enumerate = re_enumerate
        self.timeout = timeout
        self.reopen = reopen
        self.lost: str | None = None  # why the link is lost, while it is
        self._looked = 0.0  # when the lost link was last looked for
        self.lock = threading.Lock()  # the bus takes one conversation at a time
        self.listener: socket.socket | None = None
        self._stop = threading.Event()
        self._serving: threading.Thread | None = None  # the thread in serve_forever()
        self._ended = threading.Event()

    def bind(self) -> None:
        """Create the socket, replacing an earlier server's, and listen on it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.path.unlink()  # the link has one owner, so an earlier server is gone
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.path))
        os.chmod(self.path, 0o600)
        self.listener.listen(8)
        self.listener.settimeout(0.5)
        logger.info(f"bus state served at {self.path}")

    def serve_forever(self) -> None:
        """Accept clients and serve each in a thread until stop() is called.

        Then close and remove the socket, end every client's connection and wait up to STREAM_TIMEOUT seconds
        for each one's thread.
        """
        assert self.listener is not None
        self._serving = threading.current_thread()
        enumerated = time.monotonic()
        clients: list[tuple[threading.Thread, socket.socket]] = []
        try:
            while not self._stop.is_set():
                self._mind_link()
                if time.monotonic() - enumerated >= self.re_enumerate:
                    self._enumerate_again()
                    enumerated = time.monotonic()
                try:
                    conn, _ = self.listener.accept()
                except TimeoutError:
                    continue
                except OSError as e:  # out of file descriptors, a client gone while queued: the next may do
                    logger.warning(f"bus state: accepting a client failed: {e}")
                    self._stop.wait(0.5)
                    continue
                clients = [(t, c) for t, c in clients if t.is_alive()]
                thread = threading.Thread(target=self._serve_client, args=(conn,), daemon=True)
                try:
                    thread.start()
                except RuntimeError as e:  # no thread to be had: this client goes unserved, the server goes on
                    logger.warning(f"bus state: cannot serve a client: {e}")
                    conn.close()
                    continue
                clients.append((thread, conn))
        finally:
            self.listener.close()
            try:
                self.path.unlink()
            except OSError:
                pass
            self._stop.set()  # a stream ends at its next look, should serve_forever() end otherwise
            for _thread, conn in clients:
                try:
                    conn.shutdown(socket.SHUT_RDWR)  # a client waiting to send its next request is let go
                except OSError:
                    pass
            for thread, _conn in clients:
                thread.join(STREAM_TIMEOUT)
            self._ended.set()

    def stop(self) -> None:
        """Have serve_forever() return, and wait for it when it runs in another thread.

        The wait lasts up to 2 * STREAM_TIMEOUT seconds. A request still on the bus may outlast that; shutdown()
        waits for it.
        """
        self._stop.set()
        if self._serving is not None and self._serving is not threading.current_thread():
            self._ended.wait(2 * STREAM_TIMEOUT)

    def shutdown(self) -> None:
        """Stop serving, wait until the request on the bus, if any, is done, and close the link.

        This is what a process that holds a server does before it exits, so that a write of several frames is
        never cut between two of them. The client threads are daemons, which the end of the process would stop
        wherever they are.
        """
        logger.info("the server is ending: it takes no more requests, and lets the one on the bus finish")
        self.stop()
        with self.lock:
            self.system.bus.close()
        logger.info("the server has ended: its link is closed")

    def _mind_link(self) -> None:
        """Notice a link that failed under its reader, and open it again once it can be.

        A lost link is looked for every self.reopen seconds. Back, the bus is enumerated again: it may have
        changed meanwhile.
        """
        failure = getattr(self.system.bus.link, "failure", None)
        if failure and not self.lost:
            self.lost = failure
            self._looked = time.monotonic()
            logger.error(f"bus state: the link is lost ({failure}); looking for it every {self.reopen:g} s")
        if not self.lost or time.monotonic() - self._looked < self.reopen:
            return
        self._looked = time.monotonic()
        with self.lock:
            try:
                self.system.bus.reopen()
            except (LinkError, OSError) as e:  # not found yet, or its device node not closable or openable
                logger.debug(f"bus state: the link is still lost: {e}")
                return
        self.lost = None
        logger.info("bus state: the link is back")
        self._enumerate_again()

    def _enumerate_again(self) -> None:
        try:
            with self.lock:
                before = {d.address for d in self.system.devices(self.timeout)}
                devices = self.system.devices(self.timeout, refresh=True)
            for d in devices:
                if d.address not in before:
                    logger.info(f"bus state: new device {d.address} {d.product_name}")
        except Exception as e:  # the next round may do better
            logger.warning(f"bus state: enumeration failed: {e}")

    def _serve_client(self, conn: socket.socket) -> None:
        try:
            with conn, conn.makefile("rwb") as f:
                for line in f:
                    try:
                        request = json.loads(line)
                        if request.get("method") == "traffic":
                            return self._stream_traffic(conn, f)  # the connection is the stream's now
                        with self.lock:
                            result = self.handle(request.get("method", ""), request.get("params") or {})
                        reply = {"result": result}
                    except Exception as e:
                        name = type(e).__name__ if type(e).__name__ in ERRORS else "ValueError"
                        reply = {"error": {"type": name, "message": str(e)}}
                        if type(e).__name__ not in ERRORS:
                            logger.exception("state request failed")
                    f.write((json.dumps(reply) + "\n").encode())
                    f.flush()
        except OSError as e:  # the client went away mid-reply
            logger.debug(f"state client gone: {e}")

    def _stream_traffic(self, conn: socket.socket, f) -> None:
        """Write every frame on the bus to the client on *conn*, through its file *f*, one JSON line each.

        Go on until the client goes away or stops reading for STREAM_TIMEOUT seconds, the server stops, or the
        link fails, which the client is told as an error line. This runs outside the bus lock: it only listens;
        a client that falls behind loses frames (System.listen()).
        """
        frames, remove, dropped = self.system.listen()
        conn.settimeout(STREAM_TIMEOUT)
        logger.info("bus state: a monitor is listening")
        try:
            while not self._stop.is_set():
                try:
                    t, frame, sent = frames.get(timeout=0.5)
                except queue.Empty:
                    failure = getattr(self.system.bus.link, "failure", None)
                    if failure:
                        error = {"error": {"type": "LinkError", "message": failure}}
                        f.write((json.dumps(error) + "\n").encode())
                        f.flush()
                        return
                    continue
                if dropped[0]:
                    logger.warning(f"bus state: {dropped[0]} frames dropped for a monitor that fell behind")
                    dropped[0] = 0
                f.write((json.dumps({"time": t, "kind": frame.kind, "address": str(frame.address),
                                     "data": frame.data.hex(), "sent": sent}) + "\n").encode())
                f.flush()
        except OSError:
            pass
        finally:
            remove()
            logger.info("bus state: the monitor is gone")

    def handle(self, method: str, p: dict) -> Any:
        """Carry out one request, *method* with its parameters *p*, on the system.

        Return the result as plain values for JSON. Raise LinkError while the link is lost, and ValueError for
        an unknown method.
        """
        lost = self.lost or getattr(self.system.bus.link, "failure", None)  # failed, if not yet noticed
        if lost:
            raise LinkError(f"the link is lost ({lost}); the server opens it again once it is back")
        s = self.system
        if method == "devices":
            return [encode_device(d) for d in s.devices(float(p.get("timeout", self.timeout)),
                                                        refresh=bool(p.get("refresh")))]
        if method == "host":
            return str(s.host)
        device = decode_device(p["device"])
        if method == "login_level":
            return s.login_level(device)
        if method == "login":
            level = int(p["level"])
            acknowledged, after = s.login(device, level, float(p.get("password", 0.0)))
            what = f"login {login_level_name(level)}" if level else "logout"
            logger.info(f"bus state: {device.address} {what}: {'ok' if after == level else 'failed'}")
            return [acknowledged, after]
        if method == "tables":
            pages = [PAGE_BY_NAME[n] for n in p["pages"]]
            return {name: table_to_dict(t) for name, t in s.tables(device, pages, afresh=p.get("afresh", [])).items()}
        table = PageTable(PAGE_BY_NAME[p["page"]])  # values and texts need the page only
        if method == "read_values":
            return {str(i): v for i, v in s.read_values(device, table, [int(i) for i in p["indices"]]).items()}
        if method == "read_options":
            return s.read_options(device, table, Value(int(p["index"]), visualization=int(p["visualization"])))
        if method == "read_text":
            sid, text = s.read_text(device, table, int(p["index"]))
            return {"sid": sid, "text": text}
        if method == "write_value":
            return s.write_value(device, table, int(p["index"]), float(p["value"]))
        if method == "write_text":
            return s.write_text(device, table, int(p["index"]), str(p["text"]))
        if method == "name_writer":
            return s.name_writer(device, table, int(p["index"]))
        raise ValueError(f"unknown method {method!r}")


def start_in_thread(path: Path | None = None, use_cache: bool = True, timeout: float = 1.5) -> StateServer:
    """Open the link, identify the devices, serve them in a thread and return the StateServer.

    Serve at *path* (socket_path() by default), with the page cache unless *use_cache* is False, listening
    *timeout* seconds for the devices. Point the commands the server runs at it through STATE_VARIABLE. Raise
    LinkError when any of that fails.
    """
    bus = MasterBus().open()
    try:
        system = System(bus, use_cache=use_cache)
        devices = system.devices(timeout)
        server = StateServer(system, path, timeout=timeout)
        server.bind()
    except Exception as e:
        bus.close()
        raise LinkError(f"the bus state did not start: {e}") from e
    logger.info(f"bus state: {len(devices)} devices identified")
    threading.Thread(target=server.serve_forever, name="mvconnect-bus-state", daemon=True).start()
    os.environ[STATE_VARIABLE] = str(server.path)
    return server


# -- the client side


class StateSystem(System):
    """A System whose bus conversations go to the server's state; everything else runs here.

    Create one on *sock*, connected to the server at *path*; connect() does. *refresh* is as for System. The
    methods that talk to the bus (devices, traffic, host, login_level, login, tables, read_values,
    read_options, read_text, write_value, write_text, name_writer) send a request to the server instead and
    return what it answers. They take no *progress* to report: the server reads a page in one request, whose
    progress stays there. listen() is not served: the server's traffic comes through traffic().
    """

    def __init__(self, sock: socket.socket, path: Path, refresh: bool = False):
        super().__init__(bus=None, refresh=refresh)  # type: ignore[arg-type]
        self.sock = sock
        self.path = path
        self.file = sock.makefile("rwb")

    def listen(self) -> tuple[queue.Queue, Callable[[], None], list[int]]:
        """Raise LinkError: the bus is the server's, and its traffic comes through traffic()."""
        raise LinkError("listening on the bus is the server's; its traffic comes through traffic()")

    def close(self) -> None:
        """Close the connection to the server; one already broken closes quietly."""
        try:
            self.file.close()
        except OSError:  # a request still buffered for a server that is gone
            pass
        finally:
            self.sock.close()

    def call(self, method: str, **params: Any) -> Any:
        """Send *method* with *params* to the server and return its result.

        Raise the error the server reports, of the type it names (ValueError for a type not in ERRORS). Raise
        LinkError if the connection is closed already, or if the server closed it, did not answer in time or
        sent a reply that cannot be read; the connection is closed then, since a reply that comes later would
        be taken for the next request's. It is closed too when anything else, Ctrl-C say, stops the call
        between the request and its reply.
        """
        if self.file.closed:
            raise LinkError(f"the connection to the server's state at {self.path} is closed")
        try:
            self.file.write((json.dumps({"method": method, "params": params}) + "\n").encode())
            self.file.flush()
            line = self.file.readline()
        except OSError as e:  # a timeout among them, after which the socket's file reads nothing more
            self.close()
            raise LinkError(f"the server's state at {self.path} did not answer: {e}") from None
        except BaseException:  # its reply would come as the next request's
            self.close()
            raise
        if not line.endswith(b"\n"):  # nothing, or a reply cut off: the server, or its thread, is gone
            self.close()
            raise LinkError(f"the server's state at {self.path} closed the connection"
                            + (" mid-reply" if line else ""))
        try:
            reply = json.loads(line)
        except ValueError as e:
            self.close()
            raise LinkError(f"the server's state at {self.path} sent an unreadable reply: {e}") from None
        if "error" in reply:
            error = reply["error"]
            raise ERRORS.get(error.get("type"), ValueError)(error.get("message", "state error"))
        return reply["result"]

    def devices(self, timeout: float = 1.5, progress=None, refresh: bool = False) -> list[Device]:  # noqa: ARG002
        """Ask the server for the devices; see System.devices(). *progress* is not used."""
        if self._devices is None or refresh:
            got = self.call("devices", timeout=timeout, refresh=refresh)
            self._devices = check_identity([decode_device(d) for d in got])
        return self._devices

    def traffic(self) -> Iterator[tuple[float, Frame, bool]]:
        """Yield the server's bus traffic, streamed on this connection, which serves nothing else after.

        Each frame comes as in System.traffic(). Raise LinkError when the stream ends, as the server stops or
        lets a reader go that fell behind, or when a line of it comes cut off or unreadable; the server's own
        failure is raised as it names it.
        """
        self.file.write((json.dumps({"method": "traffic", "params": {}}) + "\n").encode())
        self.file.flush()
        self.sock.settimeout(None)  # a quiet bus is not an error
        for line in self.file:
            try:
                if not line.endswith(b"\n"):
                    raise ValueError("cut off")
                m = json.loads(line)
                if "error" not in m:
                    item = m["time"], Frame(m["kind"], Address.parse(m["address"]), bytes.fromhex(m["data"])), m["sent"]
            except (ValueError, KeyError, TypeError) as e:
                self.close()
                raise LinkError(f"the server's state at {self.path} sent an unreadable line of traffic: {e}") from None
            if "error" in m:  # the server's link failed
                raise ERRORS.get(m["error"].get("type"), LinkError)(m["error"].get("message", "state error"))
            yield item
        self.close()
        raise LinkError(f"the server's state at {self.path} ended the stream of traffic")

    def login_level(self, device: Device) -> int:
        """Ask the server; see System.login_level()."""
        return int(self.call("login_level", device=encode_device(device)))

    def login(self, device: Device, level: int, password: float = 0.0) -> tuple[bool, int | None]:
        """Ask the server to log *device* in or out; see System.login()."""
        acknowledged, after = self.call("login", device=encode_device(device), level=level, password=password)
        return bool(acknowledged), after

    def tables(self, device: Device, pages: list[Page] | None = None, progress=None,  # noqa: ARG002
               afresh=None) -> dict[str, PageTable]:
        """Ask the server for the page tables, one page per request; see System.tables().

        *progress* is not used: the server reads a page in one request. A page read waits as long as it takes,
        whatever the connection's timeout.
        """
        pages = pages or list(PAGES)
        afresh = self.to_refresh(device, pages) if afresh is None else list(afresh)
        waits = self.sock.gettimeout()
        self._set_timeout(None)  # a cold page read can take minutes; a server that fails says so
        tables: dict[str, PageTable] = {}
        try:
            # one page per request: the bus lock is let go between pages, so a command stopped part way
            # leaves the server at most the page it is reading, not all four, for the next command to wait on
            for page in pages:
                got = self.call("tables", device=encode_device(device), pages=[page.name],
                                afresh=[name for name in afresh if name == page.name])
                tables.update((name, table_from_dict(t)) for name, t in got.items())
        finally:
            self._set_timeout(waits)  # call() closes the connection when it is stopped mid-reply, Ctrl-C included
        return tables

    def _set_timeout(self, seconds: float | None) -> None:
        """Set the connection's timeout to *seconds*, None for none, while it is open; a closed one is left be.

        call() closes the connection when anything stops it between a request and its reply, so that a late
        reply is never taken for the next one's; a timeout set on it then would only replace what stopped the
        call, a KeyboardInterrupt say, with an error about a closed socket.
        """
        if self.sock.fileno() != -1:
            self.sock.settimeout(seconds)

    def read_values(self, device: Device, table: PageTable, indices: list[int]) -> dict[int, float | None]:
        """Ask the server; see System.read_values()."""
        got = self.call("read_values", device=encode_device(device), page=table.page.name, indices=indices)
        return {int(i): (None if v is None else float(v)) for i, v in got.items()}

    def read_options(self, device: Device, table: PageTable, value: Value) -> list[str]:
        """Ask the server; see System.read_options()."""
        return [str(o) for o in self.call("read_options", device=encode_device(device), page=table.page.name,
                                          index=value.index, visualization=value.visualization)]

    def read_text(self, device: Device, table: PageTable, index: int) -> tuple[int | None, str]:
        """Ask the server; see System.read_text()."""
        got = self.call("read_text", device=encode_device(device), page=table.page.name, index=index)
        return got["sid"], got["text"]

    def write_value(self, device: Device, table: PageTable, index: int, value: float) -> float:
        """Ask the server; see System.write_value()."""
        return float(self.call("write_value", device=encode_device(device), page=table.page.name, index=index,
                               value=value))

    def write_text(self, device: Device, table: PageTable, index: int, text: str) -> str:
        """Ask the server; see System.write_text()."""
        return str(self.call("write_text", device=encode_device(device), page=table.page.name, index=index,
                             text=text))

    @property
    def host(self) -> Address:
        """Ask the server; see System.host."""
        return Address.parse(str(self.call("host")))

    def name_writer(self, device: Device, table: PageTable, index: int) -> bool | None:
        """Ask the server; see System.name_writer()."""
        named = self.call("name_writer", device=encode_device(device), page=table.page.name, index=index)
        return None if named is None else bool(named)


def connect(path: Path, timeout: float | None = None, refresh: bool = False) -> StateSystem:
    """Return a StateSystem connected to the server's socket at *path*.

    It waits up to *timeout* seconds for each reply, as long as it takes by default: the server answers when the
    bus is free, after another command's page read if need be, and one that dies closes the connection. With
    *refresh*, the server is asked to read each page afresh once. A page read waits as long as it takes,
    whatever *timeout* says. Raise LinkError if nothing listens there.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)  # a cold page read takes a while
    try:
        sock.connect(str(path))
    except OSError as e:
        sock.close()
        raise LinkError(f"the server's bus state at {path} does not answer: {e}") from None
    return StateSystem(sock, path, refresh)


@contextmanager
def open_system(refresh: bool = False) -> Iterator[System]:
    """Yield the System for a command: the server's state, or else the link itself.

    The server's state serves when the server runs the command and says where it is (STATE_VARIABLE); else the
    link is opened for the block. Either is closed when the block ends. With *refresh*, page layouts are read
    from the devices again and the cache updated (see System).
    """
    path = os.environ.get(STATE_VARIABLE)
    if path:
        system = connect(Path(path), refresh=refresh)
        try:
            yield system
        except BaseException as e:
            winding_down(e, "closing the connection to the server", logger)
            raise
        finally:
            system.close()
        return
    with MasterBus() as bus:
        yield System(bus, refresh=refresh)
