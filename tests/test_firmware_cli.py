"""The firmware command line: what it takes from the command line, and that a dry run only asks."""

import pytest

import mvconnect.cli as cli
import mvconnect.firmware_cli as fcli
from mvconnect.masterbus import MasterBus
from test_pages import FakeShunt


def test_numbers_come_as_text_or_as_numbers():
    assert fcli._integer("0x800") == fcli._integer(2048) == fcli._integer("2048") == 2048
    with pytest.raises(ValueError):
        fcli._integer("lots")


def test_flash_options_are_taken_as_typed(monkeypatch, command):
    import mvconnect.preflight
    asked = {}

    def choose(files, product, version=None, file=None):
        asked.update(version=version)
        raise LookupError("no such file in this test")
    monkeypatch.setattr(mvconnect.preflight, "choose", choose)
    monkeypatch.setattr(fcli, "MasterBus", lambda: MasterBus(FakeShunt(), reply_timeout=0.02, retries=0))
    monkeypatch.setattr(cli, "setup_logging", lambda debug=False: None)
    with pytest.raises(SystemExit):
        command(["firmware", "flash", "msh", "--version", "2.10", "--erase-size", "0x800", "--timeout", "0.02"])
    assert asked["version"] == "2.10"  # fire would have made it 2.1, another version


# -- flash: a dry run only asks; --really enters and leaves flash mode and programs nothing

import struct

from test_flasher import FakeBootloader
from test_firmware import make_hex
from mvconnect.firmware import META_END, META_START
from mvconnect.flasher import FLASH_KIND, SEL_DONE
from mvconnect.frame import KIND_NA, KIND_REPLY, KIND_REQUEST

STRINGS = {1: "44010500", 2: "AD15E0947", 3: "CHG Charger"}  # product, serial, product name


class Charger(FakeBootloader):
    """The fake bootloader, which also identifies itself as a charger does in normal mode."""

    def _handshake(self, f):
        a, d = f.address, f.data
        if d[0] == 0x09:  # an identity string's id: the id is the number asked for
            self._reply(KIND_REPLY if d[1] in STRINGS else KIND_NA, a, d[:2] + bytes([d[1], 0]))
        elif d[0] == 0x30:  # four characters of a string
            text = STRINGS.get(d[1], "").encode() + b"\0"
            self._reply(KIND_REPLY, a, d[:4] + text[4 * d[3]:4 * d[3] + 4])
        elif d[0] == 0x08 and d[1] == 0x0B:  # the firmware version, 8.34
            self._reply(KIND_REPLY, a, d[:2] + (0x0822).to_bytes(2, "little"))
        elif d[0] == 0x08:
            self._reply(KIND_NA, a, d[:2])
        else:
            super()._handshake(f)


@pytest.fixture
def charger(monkeypatch, tmp_path, command):
    meta = (struct.pack("<HIH", 1, 44010500, 0x0100) + b"\0" + struct.pack("<H", 0x0822) + b"\0\0"
            + struct.pack("<II", 0x08003000, 0x08003040) + struct.pack("<H", 0x0880) + bytes(4))
    image = META_START + meta + META_END + bytes(range(16))
    path = tmp_path / "CHM1_V8_34.hex"
    path.write_text(make_hex([(0x3000 + i, image[i:i + 16]) for i in range(0, len(image), 16)]))
    boot = Charger()
    monkeypatch.setattr(fcli, "MasterBus", lambda: MasterBus(boot, reply_timeout=0.05, retries=0))
    monkeypatch.setattr(cli, "setup_logging", lambda debug=False: None)

    def flash(*options):
        return command(["firmware", "flash", "0a:1062b", "--file", str(path), "--timeout", "0.05", *options])
    return boot, flash


def loader_commands(boot):
    return [f.data[0] for f in boot.received if f.kind == KIND_REQUEST and f.data[0] >= 0x83]


def test_a_library_given_that_is_not_there_is_an_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "setup_logging", lambda debug=False: None)
    with pytest.raises(SystemExit) as e:
        cli.Tool().firmware.library(library=str(tmp_path / "typo"))
    assert e.value.code == 1
    cli.Tool().firmware.library()  # the default library, not fetched yet: no error


@pytest.mark.unix
def test_no_firmware_command_opens_the_link_beside_a_server(charger, monkeypatch, capsys):
    import socket

    from mvconnect import state
    boot, flash = charger
    state.socket_path().parent.mkdir(parents=True, exist_ok=True)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(state.socket_path()))
        server.listen(8)  # a server that never accepts: each look waits in its backlog
        with pytest.raises(SystemExit):
            flash()
        with pytest.raises(SystemExit):
            cli.Tool().firmware.list(timeout=0.05)
    assert boot.received == []


def test_a_dry_run_only_asks(charger):
    boot, flash = charger
    flash()
    assert loader_commands(boot) == [] and not any(f.kind == FLASH_KIND for f in boot.received)
    assert not boot.in_mode


def test_really_without_an_erase_size_does_nothing(charger):
    boot, flash = charger
    with pytest.raises(SystemExit):
        flash("--really")
    assert loader_commands(boot) == [] and not boot.in_mode


def test_a_failed_preflight_stops_really_before_the_handshake(charger, tmp_path, command):
    boot, flash = charger
    meta = (struct.pack("<HIH", 1, 44010599, 0x0100) + b"\0" + struct.pack("<H", 0x0822) + b"\0\0"
            + struct.pack("<II", 0x08003000, 0x08003040) + struct.pack("<H", 0x0880) + bytes(4))
    image = META_START + meta + META_END + bytes(range(16))
    path = tmp_path / "CHM2_V8_34.hex"  # a file for another product
    path.write_text(make_hex([(0x3000 + i, image[i:i + 16]) for i in range(0, len(image), 16)]))
    with pytest.raises(SystemExit):
        command(["firmware", "flash", "0a:1062b", "--file", str(path), "--timeout", "0.05", "--really",
                 "--erase-size", "0x20"])
    assert loader_commands(boot) == [] and not boot.in_mode


@pytest.mark.parametrize("option", [["--version="], ["--file="], ["--version"]])
def test_an_empty_version_or_file_is_no_choice_of_the_newest(charger, command, option):
    boot, flash = charger
    with pytest.raises(SystemExit):
        command(["firmware", "flash", "0a:1062b", *option, "--timeout", "0.05"])
    assert boot.received == []  # refused before the bus was opened


def test_a_misspelt_option_stops_really_before_the_handshake(charger):
    boot, flash = charger
    with pytest.raises(SystemExit):
        flash("--really", "--erase-size", "0x20", "--fil", "0xff")
    assert loader_commands(boot) == [] and not boot.in_mode


def test_really_enters_and_leaves_flash_mode_and_programs_nothing(charger):
    boot, flash = charger
    flash("--really", "--erase-size", "0x20")
    assert loader_commands(boot) == [0x83, 0x84, 0x85]  # the handshake, and the exit
    assert [f.address.hi for f in boot.received if f.kind == FLASH_KIND] == [SEL_DONE]  # only done, which writes nothing
    assert boot.flashed == {} and not boot.in_mode
