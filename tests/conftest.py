"""No test reads or writes the user's cache: $XDG_CACHE_HOME is a fresh, short directory for each test (short,
since the server's socket lives there and macOS allows about a hundred bytes for its path)."""

import shutil
import socket
import sys
import tempfile

import pytest


def pytest_collection_modifyitems(items):
    """Tests marked unix are skipped where Python has no Unix sockets, as on Windows: the server's state needs them."""
    if sys.platform == "win32" or not hasattr(socket, "AF_UNIX"):
        skip = pytest.mark.skip(reason="no Unix sockets here")
        for item in items:
            if item.get_closest_marker("unix"):
                item.add_marker(skip)


@pytest.fixture(autouse=True)
def private_cache(monkeypatch):
    directory = tempfile.mkdtemp(prefix="mv")
    monkeypatch.setenv("XDG_CACHE_HOME", directory)
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


FAKE_STATE = ("STRINGS", "MON_ITEMS", "CFG_ITEMS", "MON_GROUPS", "CFG_GROUPS", "VALUES", "COUNTS", "JUNK_FROM",
              "ADDRESS_FRAMES", "GROUP_LENGTHS", "NO_LENGTH", "PASSWORDS", "TABLES")


@pytest.fixture(autouse=True)
def fake_shunt_as_it_was():
    """The fake shunt keeps its device in module-level tables that tests change; each test gets them back as they
    were, so that a test that fails part way cannot fail the ones after it."""
    import copy

    import test_pages
    live = [getattr(test_pages, name) for name in FAKE_STATE]
    saved = copy.deepcopy(live)  # one copy, so that TABLES and CFG_ITEMS stay one dict in it too
    yield
    for now, then in zip(live, saved, strict=True):
        _put_back(now, then)


def _put_back(now, then):
    """Make *now* equal *then* in place, keeping every dict and list both still hold."""
    if isinstance(now, dict):
        for key in set(now) - set(then):
            del now[key]
        for key, value in then.items():
            if type(now.get(key)) is type(value) and isinstance(value, (dict, list)):
                _put_back(now[key], value)
            else:
                now[key] = value
    else:
        now[:] = then


@pytest.fixture(autouse=True)
def own_logging():
    """The commands set up logging on the stderr pytest captures for their test; it is closed after the test,
    so the root logger is put back as it was, and a later test logs nowhere stale."""
    import logging
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture
def command(monkeypatch):
    """Run the command line on a list of words as the shell would: sys.argv holds them, and fire parses them.
    The commands read some options as typed, from sys.argv (mvconnect.options)."""
    import sys

    import mvconnect.cli as cli

    def run(words, tool=None):
        monkeypatch.setattr(sys, "argv", ["mvconnect", *words])
        return cli.fire.Fire(tool if tool is not None else cli.Tool(), command=list(words))
    return run


@pytest.fixture(autouse=True)
def no_ending_announced():
    """Each test starts as a fresh process would: no line about what ends it logged yet, so its first is a warning."""
    import mvconnect.masterbus as masterbus
    masterbus._announced = False
    yield
    masterbus._announced = False
