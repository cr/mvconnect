"""Login and logout: the login number (Number.LOGIN) written to every device, as MasterAdjust's login sends it.

The fake device has passwords of its own (test_pages.PASSWORDS); the real ones are the user's to give."""

import struct

import pytest
from expect import has, line_with
from test_pages import COUNTS, PASSWORDS, SHUNT, FakeShunt

from mvconnect.frame import KIND_REQUEST
from mvconnect.masterbus import LOGIN_LEVELS, MasterBus, Number


@pytest.fixture
def bus():
    COUNTS[Number.LOGIN] = 0
    yield MasterBus(FakeShunt(), reply_timeout=0.02, retries=0).open()
    COUNTS[Number.LOGIN] = 0


def login_frames(sent):
    return [f.data for f in sent if f.kind == KIND_REQUEST and f.data[:2] == bytes([0x08, Number.LOGIN])
            and len(f.data) > 2]


def test_the_levels_are_masteradjusts():
    assert LOGIN_LEVELS == {"installer": 1, "distributor": 2, "service": 3}


@pytest.mark.parametrize("level", [1, 2, 3])
def test_a_login_frame_carries_the_password_as_a_float(bus, level):
    bus.login(SHUNT, level, PASSWORDS[level])
    assert login_frames(bus.link.sent) == [bytes([0x08, 0x19, level, 0]) + struct.pack("<f", PASSWORDS[level])]
    assert bus.login_level(SHUNT) == level


def test_logout_is_level_0_without_a_password(bus):
    bus.login(SHUNT, 1, PASSWORDS[1])
    bus.login(SHUNT, 0)
    assert login_frames(bus.link.sent)[-1] == bytes([0x08, 0x19, 0, 0])  # DLC 4: a password of 0 is left out
    assert bus.login_level(SHUNT) == 0


def test_an_unknown_level_sends_nothing(bus):
    with pytest.raises(ValueError):
        bus.login(SHUNT, 4, 1.0)
    assert login_frames(bus.link.sent) == []


@pytest.fixture
def tool(monkeypatch):
    import mvconnect.cli as cli
    COUNTS[Number.LOGIN] = 0
    fake = FakeShunt()
    monkeypatch.setattr(cli, "open_system", _system_on(fake))
    yield cli.Tool(), fake
    COUNTS[Number.LOGIN] = 0


def test_login_by_name_and_logout_reach_every_device(tool, capsys):
    t, fake = tool
    t.login(level="Distributor", password=PASSWORDS[2], timeout=0.02)
    assert COUNTS[Number.LOGIN] == 2
    assert line_with(capsys.readouterr().out, "1b:0cad9", "distributor")  # the device's row shows the new level
    t.logout(timeout=0.02)
    assert COUNTS[Number.LOGIN] == 0


def test_the_password_is_asked_for(tool, monkeypatch):
    t, fake = tool
    asked = []
    monkeypatch.setattr("getpass.getpass", lambda prompt: asked.append(prompt) or str(PASSWORDS[3]))
    t.login(level="service", timeout=0.02)
    assert len(asked) == 1 and has(asked[0], "service") and COUNTS[Number.LOGIN] == 3


def test_no_terminal_to_ask_on_is_an_error(tool, monkeypatch, capsys):
    t, fake = tool

    def eof(prompt):
        raise EOFError
    monkeypatch.setattr("getpass.getpass", eof)
    with pytest.raises(SystemExit):
        t.login(timeout=0.02)
    assert line_with(capsys.readouterr().err, "ERROR", "password") and login_frames(fake.sent) == []


def test_a_password_on_the_command_line_is_taken_as_typed(tool, monkeypatch, command):
    import mvconnect.cli as cli
    t, fake = tool
    monkeypatch.setattr(cli, "setup_logging", lambda debug=False: None)
    command(["login", "--level", "service", "--password", str(PASSWORDS[3]), "--timeout", "0.02"], t)
    assert COUNTS[Number.LOGIN] == 3


def test_installer_is_the_default_level(tool, monkeypatch):
    t, fake = tool
    asked = []
    monkeypatch.setattr("getpass.getpass", lambda prompt: asked.append(prompt) or str(PASSWORDS[1]))
    t.login(timeout=0.02)
    assert len(asked) == 1 and has(asked[0], "installer") and COUNTS[Number.LOGIN] == 1


def test_a_wrong_password_is_reported_not_hidden(tool, capsys):
    t, fake = tool
    with pytest.raises(SystemExit) as e:
        t.login(level="installer", password=5, timeout=0.02)
    assert e.value.code == 1 and COUNTS[Number.LOGIN] == 0  # a script can tell
    captured = capsys.readouterr()
    assert line_with(captured.err, "WARNING", "installer") and line_with(captured.out, "1b:0cad9")  # and the table


@pytest.mark.parametrize("level, password", [("admin", 1), (1, 1), ("installer", "abc"), ("installer", 0),
                                             ("installer", "1e40"), ("installer", "nan")])
def test_bad_input_sends_nothing(tool, level, password):
    t, fake = tool
    with pytest.raises(SystemExit):
        t.login(level=level, password=password, timeout=0.02)
    assert login_frames(fake.sent) == []


@pytest.mark.parametrize("level", ["admin", 1])
def test_an_unknown_level_names_the_levels(tool, capsys, level):
    t, fake = tool
    with pytest.raises(SystemExit):
        t.login(level=level, timeout=0.02)
    err = capsys.readouterr().err
    assert line_with(err, "ERROR", level)
    assert has(err, "installer", "distributor", "service")


def test_status_shows_each_level_and_sends_nothing(tool, capsys):
    t, fake = tool
    COUNTS[Number.LOGIN] = 2
    t.login(status=True, timeout=0.02)
    assert line_with(capsys.readouterr().out, "1b:0cad9", "distributor")
    assert login_frames(fake.sent) == []


def test_status_warns_about_what_it_ignores(tool, capsys):
    t, fake = tool
    t.login(status=True, level="service", password=PASSWORDS[3], timeout=0.02)
    err = capsys.readouterr().err
    assert line_with(err, "WARNING", "--level", "--password") and str(PASSWORDS[3]) not in err
    assert login_frames(fake.sent) == []


def test_a_device_without_login_is_skipped(tool, capsys):
    t, fake = tool
    COUNTS.pop(Number.LOGIN)
    t.login(password=PASSWORDS[1], timeout=0.02)
    assert login_frames(fake.sent) == []
    assert line_with(capsys.readouterr().out, "1b:0cad9")  # still listed, left alone


def test_a_device_answering_too_briefly_for_its_level_is_skipped(tool, capsys):
    t, fake = tool
    reply = fake.reply

    def short_level(kind, data):  # the level cut off
        reply(kind, data[:2] if data[:2] == bytes([0x08, Number.LOGIN]) else data)

    fake.reply = short_level
    t.login(status=True, timeout=0.02)
    assert line_with(capsys.readouterr().out, "1b:0cad9")  # still listed, left alone


def test_an_unknown_option_stops_a_logout_before_it_sends(tool, command, capsys):
    t, fake = tool
    COUNTS[Number.LOGIN] = 1
    with pytest.raises(SystemExit):
        command(["logout", "--status", "--timeout", "0.02"], t)  # --status is login's, not logout's
    assert login_frames(fake.sent) == [] and COUNTS[Number.LOGIN] == 1


def _system_on(fake):
    from contextlib import contextmanager

    from mvconnect.system import System

    @contextmanager
    def open_system(refresh=False):
        yield System(MasterBus(fake, reply_timeout=0.02, retries=0).open(), use_cache=False)
    return open_system
