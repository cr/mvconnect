"""The data command against the fake shunt: a read never writes, a write needs exactly one value and is
checked before anything is sent, --reset writes the default, and the lines a script relies on."""

import logging
import struct

import pytest

import mvconnect.cli as cli
from expect import has, line_with, logged
from mvconnect.frame import KIND_REQUEST
from test_login import _system_on
from test_pages import ADDRESS_FRAMES, CFG_ITEMS, ELSEWHERE, SHUNT, STRINGS, VALUES, FakeShunt, address_bytes

CFG = 0x37  # the config page's value kind: a read with DLC 2, a write with DLC 6


def value_writes(fake):
    return [(f.data[0] | f.data[1] << 8, struct.unpack_from("<f", f.data, 2)[0])
            for f in fake.sent if f.kind == CFG and len(f.data) == 6 and f.data[2] != 0x14]


def text_writes(fake):
    return [f.data for f in fake.sent if f.kind == KIND_REQUEST and f.data[0] == 0x30 and len(f.data) > 4]


@pytest.fixture
def details(monkeypatch):
    """The values shown in detail, one list per detail view; a listing adds nothing."""
    shown, original = [], cli.Tool._print_details

    def spy(device, table, found, values, texts):
        shown.append([v.index for v in found])
        original(device, table, found, values, texts)
    monkeypatch.setattr(cli.Tool, "_print_details", staticmethod(spy))
    return shown


@pytest.fixture
def tool(monkeypatch):
    fake = FakeShunt()
    monkeypatch.setattr(cli, "open_system", _system_on(fake))
    values, name, frames = dict(VALUES[CFG]), STRINGS[219], list(ADDRESS_FRAMES)
    yield cli.Tool(), fake
    VALUES[CFG].clear(), VALUES[CFG].update(values)
    STRINGS[219] = name
    ADDRESS_FRAMES[:] = frames


def test_a_devices_text_cannot_command_the_terminal(tool, capsys):
    t, fake = tool
    STRINGS[219] = "MSH \x1b[2J House\x07"  # a name that would clear the screen and ring the bell
    t.data(timeout=0.02)
    cli.setup_logging()
    logging.getLogger("mvconnect").warning("a name\x1b[31m\n2026-01-01 forged line")
    out, err = capsys.readouterr()
    assert "\x1b" not in out + err and "\x07" not in out and has(out, "House")
    assert line_with(err, "a name", "forged")  # the warning, on one line


@pytest.mark.parametrize("words", [
    ["0", "--set", "13", "--forse"],  # a misspelt --force
    ["0", "--set", "13", "--dry-run"],  # an option data does not have
    ["7", "--set", "-Bow"],  # a value with a leading dash, which fire would take for a flag
    ["0", "--reset", "-x"],
])
def test_an_unknown_option_stops_the_command_before_it_writes(tool, command, capsys, words):
    t, fake = tool
    with pytest.raises(SystemExit):
        command(["data", "house", "bank", "config", *words, "--timeout", "0.02"], t)
    assert value_writes(fake) == [] and text_writes(fake) == []
    assert line_with(capsys.readouterr().err, "ERROR", words[-1])


def test_a_bare_short_set_writes_nothing_and_nothing_after_the_separator_is_written(tool, command):
    t, fake = tool
    with pytest.raises(SystemExit):
        command(["data", "house", "bank", "config", "7", "-s", "--timeout", "0.02"], t)  # fire hands over "True"
    command(["data", "house", "bank", "config", "7", "--timeout", "0.02", "--", "--set", "X"], t)  # a read
    assert STRINGS[219] == "MSH House Bank" and text_writes(fake) == [] and value_writes(fake) == []


def test_an_empty_set_writes_only_when_forced(tool, command):
    t, fake = tool
    with pytest.raises(SystemExit):
        command(["data", "house", "bank", "config", "7", "--set=", "--timeout", "0.02"], t)  # --set "$UNSET"
    assert text_writes(fake) == []
    command(["data", "house", "bank", "config", "7", "--set", "", "--force", "--timeout", "0.02"], t)
    assert STRINGS[219] == ""


def test_a_repeated_set_writes_nothing(tool, command, capsys):
    t, fake = tool
    with pytest.raises(SystemExit):
        command(["data", "house", "bank", "config", "0", "--set", "13", "--set", "14", "--timeout", "0.02"], t)
    assert value_writes(fake) == [] and line_with(capsys.readouterr().err, "ERROR", "--set")


def test_the_words_are_taken_as_typed_not_as_python(tool, command, capsys):
    t, fake = tool
    STRINGS[219] = "MSH Bow, port"
    command(["data", "Bow, port", "config", "0", "--timeout", "0.02"], t)  # one word, not a tuple
    assert has(capsys.readouterr().out, "Nominal voltage")
    with pytest.raises(SystemExit):
        command(["data", "msh", "config", "0x7", "--timeout", "0.02"], t)  # not index 7, Device name
    assert line_with(capsys.readouterr().err, "ERROR", "0x7")


def test_a_short_answer_during_a_read_is_an_error_not_a_traceback(tool, capsys):
    from types import SimpleNamespace

    from test_pages import answering
    t, fake = tool
    answering(SimpleNamespace(link=fake), lambda kind, data: (kind, data[:2]), to=lambda f: f.data[:2] == b"\x08\x05")
    with pytest.raises(SystemExit) as e:  # the count of config values, cut short
        t.data("house", "bank", "config", timeout=0.02)
    assert e.value.code == 1 and line_with(capsys.readouterr().err, "ERROR")


@pytest.mark.parametrize("timeout", ["nan", "inf", "1e300", "0", "-1", "soon"])
def test_a_timeout_is_a_finite_number_of_seconds(tool, command, capsys, timeout):
    t, fake = tool
    with pytest.raises(SystemExit):
        command(["data", "house", "bank", "--timeout", timeout], t)
    assert fake.sent == [] and line_with(capsys.readouterr().err, "ERROR", "timeout")


def test_no_device_answering_is_an_error(tool, monkeypatch, capsys):
    import mvconnect.firmware_cli as fcli
    from mvconnect.frame import KIND_ENUMERATE
    from mvconnect.masterbus import MasterBus
    t, fake = tool
    send = fake.send
    fake.send = lambda *frames, **options: send(*[f for f in frames if f.kind != KIND_ENUMERATE], **options)
    with pytest.raises(SystemExit) as e:
        t.data(timeout=0.02)
    assert e.value.code == 1 and line_with(capsys.readouterr().err, "ERROR")
    monkeypatch.setattr(fcli, "MasterBus", lambda: MasterBus(fake, reply_timeout=0.02, retries=0))
    with pytest.raises(SystemExit) as e:
        t.firmware.list(timeout=0.02)
    assert e.value.code == 1


@pytest.mark.unix
def test_a_link_a_server_here_holds_is_explained(monkeypatch, capsys):
    import socket
    from contextlib import contextmanager

    from mvconnect import state
    from mvconnect.hidlink import LinkError

    @contextmanager
    def held(refresh=False):
        raise LinkError("cannot open MasterBus USB Link")
        yield
    monkeypatch.setattr(cli, "open_system", held)
    monkeypatch.delenv(state.STATE_VARIABLE, raising=False)
    with pytest.raises(SystemExit):
        cli.Tool().data(timeout=0.02)
    assert not has(capsys.readouterr().err, "server")  # none running: the error as it is
    state.socket_path().parent.mkdir(parents=True, exist_ok=True)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(state.socket_path()))
        server.listen(1)
        with pytest.raises(SystemExit):
            cli.Tool().data(timeout=0.02)
    assert line_with(capsys.readouterr().err, "ERROR", "server", "--remote localhost")


def test_known_options_in_their_other_forms_pass(tool, command):
    t, fake = tool
    command(["data", "house", "bank", "config", "0", "-s", "13", "--timeout", "0.02"], t)
    command(["data", "house", "bank", "config", "7", "--set=-Bow", "--force", "--nocache", "--timeout=0.02"], t)
    assert STRINGS[219] == "-Bow" and value_writes(fake) == [(0, 13.0)]


def test_a_write_the_device_does_not_take_exits_1(tool, capsys):
    t, fake = tool
    send = fake.send

    def ignoring_writes(*frames, **options):  # the device takes no write, and answers none
        return send(*[f for f in frames if not (f.kind == CFG and len(f.data) == 6)], **options)
    fake.send = ignoring_writes
    with pytest.raises(SystemExit) as e:
        t.data("house", "bank", "config", "0", set=13, timeout=0.02)
    out, err = capsys.readouterr()
    assert e.value.code == 1 and line_with(out, 12) and line_with(err, "ERROR", 13, 12)
    assert VALUES[CFG][0] == 12.0


def test_a_read_never_writes(tool, capsys):
    t, fake = tool
    t.data(timeout=0.02)
    t.data("house", "bank", "config", timeout=0.02)
    t.data("house", "bank", "config", "0", timeout=0.02)
    t.data("house", "bank", "config", "batt", timeout=0.02)
    out = capsys.readouterr().out
    assert "1b:0cad9" in out and "Nominal voltage" in out and "Battery capacity" in out
    assert value_writes(fake) == [] and text_writes(fake) == []


def test_set_writes_the_one_value_the_words_select_and_reads_it_back(tool, capsys):
    t, fake = tool
    t.data("house", "bank", "config", "0", set=24, timeout=0.02)  # fire hands a number over as an int
    assert VALUES[CFG][0] == 24.0 and value_writes(fake) == [(0, 24.0)]
    assert has(capsys.readouterr().out, 12, 24)  # before and after
    t.data("house", "bank", "config", "nominal", set="13", timeout=0.02)  # by a word start
    assert VALUES[CFG][0] == 13.0


def test_several_values_are_listed_and_nothing_is_written(tool, capsys):
    t, fake = tool
    for words in (["date"], ["batt"], []):
        with pytest.raises(SystemExit):
            t.data("house", "bank", "config", *words, set=1, timeout=0.02)
    out = capsys.readouterr().out
    assert "Date" in out and "Battery type" in out  # the listing came out before the refusal
    assert value_writes(fake) == []


@pytest.mark.parametrize("words, options", [
    (["0"], dict(set=99)),  # above the maximum
    (["0"], dict(set="seven")),  # not a number
    (["2"], dict(set="AGM")),  # the device marks it read-only
    (["2"], dict(set="NiCd")),  # not an option
    (["0"], dict(set=True)),  # --set without a value
    (["0"], dict(set=1, reset=True)),  # both actions
    (["7"], dict(set="A name longer than twelve")),  # too long for the text value
    (["1"], dict(reset=True)),  # no default given
    (["nowhere"], dict(set=1)),  # no such value
    (["7", "zzz"], dict(set=1)),
])
def test_a_refused_write_sends_nothing(tool, words, options, capsys):
    t, fake = tool
    with pytest.raises(SystemExit):
        t.data("house", "bank", "config", *words, timeout=0.02, **options)
    assert value_writes(fake) == [] and text_writes(fake) == []
    assert line_with(capsys.readouterr().err, "ERROR")  # refused, and said so


@pytest.mark.parametrize("words", [[], ["house", "bank"], ["house", "bank", "config"]])
def test_a_write_needs_device_page_and_value(tool, words, capsys):
    t, fake = tool
    with pytest.raises(SystemExit):
        t.data(*words, set=1, timeout=0.02)
    assert value_writes(fake) == [] and line_with(capsys.readouterr().err, "ERROR")


def test_force_sends_what_the_checks_refuse(tool):
    t, fake = tool
    t.data("house", "bank", "config", "2", set="AGM", force=True, timeout=0.02)
    assert VALUES[CFG][2] == 1.0 and value_writes(fake) == [(2, 1.0)]


def test_reset_writes_the_default(tool):
    t, fake = tool
    t.data("house", "bank", "config", "0", set=24, timeout=0.02)
    t.data("house", "bank", "config", "0", reset=True, timeout=0.02)
    assert VALUES[CFG][0] == 12.0 and value_writes(fake) == [(0, 24.0), (0, 12.0)]


def test_text_and_device_values_are_written_as_what_they_are(tool):
    t, fake = tool
    before = len(ADDRESS_FRAMES)
    t.data("house", "bank", "config", "event", "1", "target", set="house bank", timeout=0.02)
    assert VALUES[CFG][5] == 1.0 and len(ADDRESS_FRAMES) == before  # the shunt's entry, as a number; no second frame
    t.data("house", "bank", "config", "5", set="none", timeout=0.02)
    assert VALUES[CFG][5] == 0.0 and len(ADDRESS_FRAMES) == before
    t.data("house", "bank", "config", "device", "name", set="MSH Bow", timeout=0.02)  # renames the shunt
    assert STRINGS[219] == "MSH Bow" and value_writes(fake) == [(5, 1.0), (5, 0.0)] and len(text_writes(fake)) == 2
    t.data("msh", "config", "7", timeout=0.02)  # it answers to its type still, and to its new name
    t.data("bow", "config", "7", timeout=0.02)


def test_without_a_page_the_words_select_on_every_page(tool, capsys, details):
    t, fake = tool
    t.data("house", "bank", "battery", timeout=0.02)  # monitor 1 Battery, and the config group Battery
    out = capsys.readouterr().out
    assert has(out, "monitor", "config", "Battery capacity") and not has(out, "State of Charge")
    t.data("house", "bank", "1", timeout=0.02)  # one value per page: a listing, not the details
    assert has(capsys.readouterr().out, "monitor", "config") and details == []
    t.data("house", "bank", "nominal", "batt", timeout=0.02)  # unique across the pages: the details
    assert details == [[0]]
    t.data("house", timeout=0.02)  # the words are all device: the whole device
    assert "State of Charge" in capsys.readouterr().out
    assert value_writes(fake) == []
    capsys.readouterr()
    with pytest.raises(SystemExit):
        t.data("house", "bank", "nowhere", timeout=0.02)
    assert line_with(capsys.readouterr().err, "ERROR", "nowhere")
    with pytest.raises(SystemExit):
        t.data("nobody", "battery", timeout=0.02)
    assert line_with(capsys.readouterr().err, "ERROR", "nobody")


def test_a_page_less_write_needs_one_value_across_the_pages(tool, capsys):
    t, fake = tool
    t.data("house", "bank", "nominal", set=13, timeout=0.02)  # Nominal voltage is unique on the device
    assert VALUES[CFG][0] == 13.0 and value_writes(fake) == [(0, 13.0)]
    with pytest.raises(SystemExit):
        t.data("house", "bank", "1", set=5, timeout=0.02)  # index 1 is on two pages
    assert value_writes(fake) == [(0, 13.0)]
    assert has(capsys.readouterr().out, "monitor", "config")  # the two candidates were listed


def test_the_page_keyword_filters_from_anywhere_and_an_index_pins(tool, capsys, details):
    t, fake = tool
    t.data("house", "bank", "battery", "config", timeout=0.02)  # the page after the words
    out = capsys.readouterr().out
    assert has(out, "Battery capacity", "Battery type") and not has(out, "monitor")
    t.data("house", "bank", "batt", "2", timeout=0.02)  # the index among the words: one value, in detail
    assert details == [[2]]
    t.data("house", "bank", "1", "config", timeout=0.02)
    assert "Battery capacity" in capsys.readouterr().out
    t.data("house", "bank", "batt", "2", set="AGM", force=True, timeout=0.02)
    assert VALUES[CFG][2] == 1.0 and value_writes(fake) == [(2, 1.0)]
    capsys.readouterr()
    with pytest.raises(SystemExit):
        t.data("house", "bank", "config", "monitor", timeout=0.02)  # one page at a time
    assert line_with(capsys.readouterr().err, "ERROR")


def test_a_page_without_values_is_an_empty_listing_not_an_error(tool, capsys):
    t, fake = tool
    t.data("house", "bank", "alarm", timeout=0.02)  # the fake's alarm page has no values
    assert not has(capsys.readouterr().out, "Battery capacity")


def test_set_writes_the_text_as_typed(tool, command):
    t, fake = tool
    with pytest.raises(SystemExit):
        t.data("house", "bank", "config", "device", "name", set=("Bow", "port"))  # several things, from Python
    command(["data", "house", "bank", "config", "device", "name", "--set", "Bow, port"], t)
    assert STRINGS[219] == "Bow, port"  # not "('Bow', 'port')", as fire would have parsed it
    command(["data", "1b:0cad9", "config", "device", "name", "--set=007"], t)  # its name and type are "Bow, port" now
    assert STRINGS[219] == "007"


@pytest.mark.parametrize("words", [["--set"], ["--set", "--force"], ["--set", "--timeout", "0.02"]])
def test_a_set_without_a_value_writes_nothing(tool, command, words):
    t, fake = tool
    with pytest.raises(SystemExit):
        command(["data", "house", "bank", "config", "device", "name", *words], t)  # fire would hand over "True"
    assert STRINGS[219] != "True" and text_writes(fake) == [] and value_writes(fake) == []


def test_the_help_lists_no_groups(command, capsys):
    for words in (["data"], ["login"], ["firmware", "fetch"], ["firmware", "flash"]):
        with pytest.raises(SystemExit):
            command([*words, "--help"])
        out = capsys.readouterr().out
        assert "FIRE_METADATA" not in out and "GROUP |" not in out  # a parse-function decorator showed as a group


def test_get_and_set_are_gone():
    assert not hasattr(cli.Tool, "get") and not hasattr(cli.Tool, "set")


def test_link_shows_the_first_and_warns_of_the_others(monkeypatch, caplog):
    import logging

    from mvconnect import hidlink
    from mvconnect.hidlink import LinkInfo
    one = LinkInfo(b"/dev/hidraw0", "F226", "MasterBus USB Link", "Mastervolt")
    two = LinkInfo(b"/dev/hidraw1", "F227", "MasterBus USB Link", "Mastervolt")
    monkeypatch.setattr(hidlink, "find_links", lambda: [one, two])
    monkeypatch.setattr(cli, "setup_logging", lambda debug=False: None)  # keeps pytest's log capture in place
    with caplog.at_level(logging.WARNING):
        shown = cli.Tool().link()
    assert has(shown, "F226") and not has(shown, "F227") and logged(caplog, "WARNING", "F227")
    monkeypatch.setattr(hidlink, "find_links", lambda: [])
    with pytest.raises(SystemExit):
        cli.Tool().link()
    assert not hasattr(cli.Tool, "links")


def test_a_listing_shows_a_device_value_s_entries_live_not_from_the_table(tool, capsys):
    t, fake = tool
    t.data("house", "bank", "config", "5", timeout=0.02)  # Event 1 target, in detail; the table is cached now
    assert has(capsys.readouterr().out, "none, 1b:0cad9, 0a:1062b")
    CFG_ITEMS[5].update({(0x20, 1): address_bytes(ELSEWHERE), (0x20, 2): address_bytes(SHUNT)})  # the device swaps them
    t.data("house", "bank", "config", "5", timeout=0.02)  # no --nocache: the table from the cache, the entries live
    assert has(capsys.readouterr().out, "none, 0a:1062b, 1b:0cad9")
    VALUES[CFG][5] = 2.0
    t.data("house", "bank", "config", "event", timeout=0.02)  # the listing formats the value by the live entries
    assert line_with(capsys.readouterr().out, "Event 1 target", "1b:0cad9")
