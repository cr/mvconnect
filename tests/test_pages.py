"""The page model and the path resolver against a fake device shaped like the House Bank shunt."""

import dataclasses
import struct

import pytest
from expect import has, line_with, logged, mentions

from mvconnect import cache
from mvconnect.frame import KIND_ANNOUNCE, KIND_ENUMERATE, KIND_NA, KIND_REPLY, KIND_REQUEST, Address, Frame
from mvconnect.masterbus import MasterBus
from mvconnect.pages import PAGE_BY_NAME, PAGES, PageReader, Value, table_from_dict, table_to_dict
from mvconnect.system import AddressError, Refused, System

SHUNT = Address(0x1B, 0x0CAD9)
ELSEWHERE = Address(0x0A, 0x1062B)  # a charger the fake bus does not have


def address_bytes(address: Address) -> bytes:
    """An entry of a device value as the device answers it: the high field, then the low field in three bytes."""
    return bytes([address.hi]) + address.lo.to_bytes(3, "little")


NO_ENTRY = b"\xff\xff\xff\xff"  # an empty entry of a device value: a high field above 0x1f

STRINGS = {0: "E819L0421", 0x12: "77020100", 219: "MSH House Bank", 0x14: "V", 0x18: "%", 0x25: "State of Charge",
           0x23: "Battery", 0x16: "Ah", 70: "Nominal voltage", 71: "Battery capacity", 72: "Battery type",
           150: "General", 151: "Battery", 73: "Date", 0xC3: "Gel", 0xC4: "AGM", 0xC5: "Wet", 0xC6: "Li-ion",
           74: "Event 1 target", 75: "Event 1 command", 0xD1: "House Bank", 0xD2: "Switch", 0xD5: "Toggle",
           76: "Device name", 77: "Nominal battery voltage", 78: "The voltage the capacity is rated at"}

# page kind -> index -> attributes; attr 0x03, 0x0F, 0x20 and 0x26 keyed by (attr, sub); a bytes value is
# answered as it is, a float in 4 bytes, an int in 2
MON_ITEMS = {0: {0x28: 0x25, 0x2C: 0x18, 0x02: 1, 0x08: 1.0, 0x06: 0.0, 0x07: 100.0},
             1: {0x28: 0x23, 0x2C: 0x14, 0x02: 1, 0x08: 0.01}}
CFG_ITEMS = {0: {0x28: 70, 0x2C: 0x14, 0x02: 1, 0x08: 1.0, 0x09: 12.0, 0x0B: 1, 0x06: 12.0, 0x07: 48.0,
                 0x0C: 1, 0x0D: 2, 0x29: 77, 0x2A: 78},
             1: {0x28: 71, 0x2C: 0x16, 0x02: 1, 0x08: 1.0},
             3: {0x28: 73, 0x02: 1, 0x08: 1.0}, 4: {0x28: 73, 0x02: 1, 0x08: 1.0},  # two values named 'Date'
             2: {0x28: 72, 0x02: 3, 0x08: 1.0, (0x26, 0): 0xC3, (0x26, 1): 0xC4, (0x26, 2): 0xC5, (0x26, 3): 0xC6},
             # a device value and its command, as the CHG has them: as many entries as the maximum says, the
             # device value's as addresses behind 0x20 (an empty one first), the command's as texts behind 0x0F
             5: {0x28: 74, 0x02: 9, 0x08: 1.0, 0x06: 0.0, 0x07: 3.0, 0x0B: 1, (0x20, 0): NO_ENTRY,
                 (0x20, 1): address_bytes(SHUNT), (0x20, 2): address_bytes(ELSEWHERE)},
             6: {0x28: 75, 0x02: 10, 0x08: 1.0, 0x06: 0.0, 0x07: 2.0, 0x0B: 1, (0x0F, 0): 0xD2, (0x0F, 1): 0xD5},
             # a text value: its value names the device string that holds the text
             7: {0x28: 76, 0x02: 6, 0x08: 1.0, 0x06: 0.0, 0x07: 12.0, 0x0B: 1}}
MON_GROUPS = {0: {0x28: 150, (0x03, 0): 1, (0x03, 1): 0}}
# group 1 answers garbage beyond its end instead of "not available"
CFG_GROUPS = {0: {0x28: 150, (0x03, 0): 0}, 1: {0x28: 151, (0x03, 0): 1, (0x03, 1): 2, (0x03, 2): 16608, (0x03, 3): 1}}
VALUES = {0x30: {0: 98.9, 1: 13.4}, 0x37: {0: 12.0, 1: 510.0, 2: 3.0, 5: 0.0, 6: 0.0, 7: 219.0}}
COUNTS = {0x01: 2, 0x02: 1, 0x03: 0, 0x04: 0, 0x05: 8, 0x06: 2, 0x07: 0, 0x08: 0, 0x0B: 0x0201,
          0x1C: 5, 0x1D: 1, 0x1E: 7, 0x1F: 1, 0x60: 14,  # page version numbers and the status version
          0x19: 0}  # the login level
NO_COUNT: set[int] = set()  # count kinds the fake answers 'not available' for, as an EasyView does for its alarm page
JUNK_FROM = [None]  # when set: the fake answers attributes of config values from this index on with zeros
ADDRESS_FRAMES = []  # the 0x14 frames the fake device received
GROUP_LENGTHS = {(0x39, 1): 2}  # config group 1 says it has 2 members, though it answers 4
NO_LENGTH = [set()]  # (kind, index) groups that answer "not available" for their length
PASSWORDS = {0: 0.0, 1: 1111.0, 2: -2222.0, 3: -33333.0}  # the fake's own login passwords, not the devices'
TABLES = {0x31: MON_ITEMS, 0x32: MON_GROUPS, 0x38: CFG_ITEMS, 0x39: CFG_GROUPS, 0x33: {}, 0x34: {}, 0x35: {}, 0x36: {}}


class FakeShunt:
    def __init__(self):
        self.listener = None
        self.sent = []

    def open(self, listener):
        self.listener = listener
        return self

    def close(self):
        pass

    def reply(self, kind, data):
        self.listener(Frame(kind, SHUNT, data))

    def send(self, *frames, timeout=1.0):
        for f in frames:
            self.sent.append(f)
            k, d = f.kind, f.data
            if k == KIND_ENUMERATE:
                self.reply(KIND_ANNOUNCE, bytes.fromhex("1bd9ca0000020000"))
            elif k == KIND_REQUEST and d[0] == 0x30:
                sid = d[1] | d[2] << 8
                if len(d) > 4:  # a write of one chunk, kept until a NUL ends the text
                    buf = bytearray(STRINGS.get(sid, "").encode("latin-1").ljust(4 * d[3], b"\0"))
                    buf[4 * d[3]:4 * d[3] + len(d) - 4] = d[4:]
                    STRINGS[sid] = bytes(buf).split(b"\0")[0].decode("latin-1")
                text = (STRINGS.get(sid, "").encode("latin-1") + b"\0")
                self.reply(KIND_REPLY, d[:4] + text[4 * d[3]:4 * d[3] + 4])
            elif k == KIND_REQUEST and d[0] == 0x09:
                sid = {1: 0x12, 2: 0, 3: 219}.get(d[1])
                if sid is not None:
                    self.reply(KIND_REPLY, d[:2] + sid.to_bytes(2, "little"))
            elif k == KIND_REQUEST and d[0] == 0x08 and len(d) > 2:
                # a count write; the login (0x19) takes a level whose password follows, 0 when left out
                password = struct.unpack_from("<f", d, 4)[0] if len(d) == 8 else 0.0
                level = d[2] | d[3] << 8
                if d[1] == 0x19 and d[1] in COUNTS and PASSWORDS.get(level) == password:
                    COUNTS[0x19] = level
                    self.reply(KIND_REPLY, d)
                elif d[1] in COUNTS:
                    self.reply(KIND_NA, d)
            elif k == KIND_REQUEST and d[0] == 0x08:
                if d[1] in NO_COUNT:
                    self.reply(KIND_NA, d)
                elif d[1] in COUNTS:
                    self.reply(KIND_REPLY, d[:2] + COUNTS[d[1]].to_bytes(2, "little"))
            elif k in VALUES and len(d) == 2:
                i = d[0] | d[1] << 8
                if i in VALUES[k]:
                    self.reply(k - 0x20, d + struct.pack("<f", VALUES[k][i]))
                else:
                    self.reply(k - 0x10, d)
            elif k in TABLES and k in (0x32, 0x34, 0x36, 0x39) and len(d) == 2:
                # a group's value is its length: the number of members it really has
                i = d[0] | d[1] << 8
                members = [key for key in TABLES[k].get(i, {}) if isinstance(key, tuple) and key[0] == 0x03]
                if i in TABLES[k] and i not in NO_LENGTH[0]:
                    self.reply(k - 0x20, d + struct.pack("<f", GROUP_LENGTHS.get((k, i), len(members))))
                else:
                    self.reply(k - 0x10, d)
            elif k in VALUES and len(d) == 6 and d[2] == 0x14:
                # the frame MasterAdjust sends after an eventable write, its own address: counted, so that tests
                # can show mvconnect sends none; echoed, as MasterAdjust's own simulator does
                ADDRESS_FRAMES.append(d)
                self.reply(k - 0x20, d)
            elif k in VALUES and len(d) == 6:
                # a write: accepted within the value's range, refused otherwise
                i = d[0] | d[1] << 8
                v = struct.unpack_from("<f", d, 2)[0]
                attrs = TABLES[k + 1].get(i, {})
                if i in VALUES[k] and attrs.get(0x06, -1e9) <= v <= attrs.get(0x07, 1e9):
                    VALUES[k][i] = v
                    self.reply(k - 0x20, d)
                else:
                    self.reply(k - 0x10, d)
            elif k in TABLES and len(d) in (3, 4):
                attr, i = d[0], d[1] | d[2] << 8
                key = (attr, d[3]) if len(d) == 4 else attr
                v = TABLES[k].get(i, {}).get(key)
                if k == 0x38 and JUNK_FROM[0] is not None and i >= JUNK_FROM[0]:
                    v = 0  # junk: every attribute reads as 0, and string 0 is the serial number
                if v is None:
                    self.reply(k - 0x10, d)
                elif isinstance(v, bytes):
                    self.reply(k - 0x20, d + b"\0" * (4 - len(d)) + v)
                elif isinstance(v, float):
                    self.reply(k - 0x20, d + b"\0" * (4 - len(d)) + struct.pack("<f", v))
                else:
                    self.reply(k - 0x20, d + b"\0" * (4 - len(d)) + v.to_bytes(2, "little"))



def lines(described) -> str:
    """Value.describe()'s pairs as lines of text, a label and its text on each."""
    return "\n".join(f"{label}: {text}" for label, text in described)


@pytest.fixture
def bus():
    return MasterBus(FakeShunt(), reply_timeout=0.02, retries=0).open()


def test_locate_splits_device_page_and_value_words(bus):
    system = System(bus)
    sel = system.locate(["house", "bank"], timeout=0.02)
    assert (sel.device.address, sel.page, sel.words) == (SHUNT, None, "")
    sel = system.locate(["1b:0cad9", "Config"], timeout=0.02)
    assert (sel.page, sel.words) == ("config", "")
    sel = system.locate(["house", "bank", "history", "min/max"], timeout=0.02)
    assert (sel.page, sel.words) == ("history", "min/max")
    sel = system.locate(["house", "bank", "battery", "low", "config"], timeout=0.02)  # the page anywhere
    assert (sel.page, sel.words) == ("config", "battery low")
    sel = system.locate(["msh", "config", 21], timeout=0.02)
    assert sel.words == "21"
    sel = system.locate(["msh", "Amp=Full"], timeout=0.02)
    assert sel.words == "Amp=Full"  # every word is a selector word; '=' is nothing special
    with pytest.raises(AddressError, match=mentions("config 21")):
        system.locate(["config", "21"], timeout=0.02)
    sel = system.locate(["msh", "config", "alarm", "levels"], timeout=0.02)  # two page words: two readings
    assert sel.readings == [("config", "alarm levels"), ("alarm", "config levels"), (None, "config alarm levels")]


def test_a_page_word_in_a_name_is_a_word_of_the_name(bus):
    system = System(bus)
    sel = lambda *words: system.locate(["msh", *words], timeout=0.02)
    names = lambda found: [(t.page.name, v.index) for t, v in found]
    STRINGS[151] = "Alarm levels"  # the config group Battery, named as the shunt names one of its groups
    try:
        for words in (["alarm", "levels"], ["config", "alarm", "levels"], ["levels", "alarm"]):
            assert names(system.find_selection(sel(*words))[2]) == [("config", 1), ("config", 2)]
        assert system.find_selection(sel("config", "battery"))[:2] == ("config", "battery")  # a page, as before
        assert system.find_selection(sel("alarm"))[:2] == ("alarm", "")  # a page alone stands, empty or not
    finally:
        STRINGS[151] = "Battery"


def test_a_frame_from_before_the_request_is_never_its_reply(bus):
    mon = PAGE_BY_NAME["monitor"]
    bus._receive(Frame(0x10, SHUNT, bytes([0, 0]) + struct.pack("<f", 11.0)))  # someone's poll, answered earlier
    assert bus._queue.get(0) is None  # nothing is queued between conversations
    bus._receive(Frame(0x10, SHUNT, bytes([0, 0]) + struct.pack("<f", 11.0)))
    assert PageReader(bus, SHUNT, reply_timeout=0.02, retries=0).read_value(mon, 0) == pytest.approx(98.9)


def test_read_value_and_na(bus):
    r = PageReader(bus, SHUNT, reply_timeout=0.02, retries=0)
    mon = PAGE_BY_NAME["monitor"]
    assert r.read_value(mon, 0) == pytest.approx(98.9)
    assert r.read_value(mon, 7) is None
    assert r.read_value(PAGE_BY_NAME["alarm"], 0) is None


def test_read_item_and_group(bus):
    r = PageReader(bus, SHUNT, reply_timeout=0.02, retries=0)
    cfg = PAGE_BY_NAME["config"]
    value = r.read_attributes(cfg, 2)
    assert value.name == "Battery type" and value.visualization == 3
    assert value.options == ["Gel", "AGM", "Wet", "Li-ion"]
    assert value.format(2.0) == "Wet"
    v = r.read_attributes(cfg, 0)
    assert (v.name, v.unit, v.default, v.writeable) == ("Nominal voltage", "V", 12.0, True)
    assert v.format(12.0) == "12"
    g = r.read_group(cfg, 1, n_values=3)
    assert (g.name, g.values) == ("Battery", [1, 2])
    assert r.read_group(cfg, 1).values == [1, 2]  # it says two members: the garbage third is never asked for


def test_item_format_precision():
    assert Value(0, step=0.01).format(13.4) == "13.40"
    assert Value(0, step=1.0).format(98.9) == "98.9"
    assert Value(0, step=1.0).format(500.697) == "500.7"
    assert Value(0).format(0.0938657) == "0.09"
    assert Value(0).format(-486.0) == "-486"
    assert Value(0).format(None) == "-"
    assert Value(0).format(float("nan")) == "n/a"


def test_read_page_and_roundtrip(bus):
    r = PageReader(bus, SHUNT, reply_timeout=0.02, retries=0)
    steps = []
    table = r.read_page(PAGE_BY_NAME["monitor"], progress=lambda n, t: steps.append((n, t)))
    assert sorted(table.values) == [0, 1] and [g.values for g in table.groups] == [[1, 0]]
    assert steps[-1] == (3, 3)
    again = table_from_dict(table_to_dict(table))
    assert again == table


def test_system_resolves_by_name_and_lists(bus, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    system = System(bus)
    device = system.device("house bank", timeout=0.02)
    assert device.address == SHUNT
    tables = system.tables(device, [PAGE_BY_NAME["config"]])
    cfg = tables["config"]
    assert [i.index for i in System.find_values(cfg, "battery")] == [0, 1, 2]  # the long name, the group's values
    assert [i.name for i in System.find_values(cfg, "2")] == ["Battery type"]
    assert System.find_values(cfg, "#2") == [] and System.find_values(cfg, "[2]") == []
    assert [i.index for i in System.find_values(cfg, "batt")] == [0, 1, 2]  # word starts: names, long name, groups
    assert [i.index for i in System.find_values(cfg, "batt cap")] == [1]
    assert [i.index for i in System.find_values(cfg, "nominal batt")] == [0]  # the long name counts
    assert System.find_values(cfg, "ER") == []  # not the start of a word
    assert System.find_values(cfg, "nowhere") == [] and System.find_values(cfg, "9") == []
    assert len(System.find_values(cfg, "")) == len(cfg.values)
    assert System.sections(cfg, System.find_values(cfg, "batt cap")) == [("Battery", [1])]
    assert System.sections(cfg, System.find_values(cfg, "batt")) == [("General", [0]), ("Battery", [1, 2])]
    whole = System.sections(cfg, list(cfg.values.values()))
    assert whole[:2] == [("General", [0]), ("Battery", [1, 2])] and whole[-1][0] == "other"
    assert system.read_values(device, cfg, [0, 2]) == {0: 12.0, 2: 3.0}
    # a repeated name selects every value of that name, in table order
    assert [i.index for i in System.find_values(cfg, "date")] == [3, 4]
    assert cfg.ungrouped() == [3, 4, 5, 6, 7]
    with pytest.raises(AddressError):
        system.device("1b:00001", timeout=0.02)
    # the full set of pages is cached and read back without the link
    system.tables(device)
    sent = len(bus.link.sent)
    cached = System(bus).tables(device)
    # From the cache: nothing is read but each page's version number, one number read each.
    added = bus.link.sent[sent:]
    assert [f.data for f in added] == [bytes([0x08, p.version]) for p in PAGES] and cached["config"] == cfg


def test_parse_values():
    number = Value(0, minimum=0.0, maximum=59.0, step=1.0, writeable=True)
    assert number.parse("7") == 7.0 and number.parse(" 59 ") == 59.0
    with pytest.raises(ValueError):
        number.parse("60")
    with pytest.raises(ValueError):
        number.parse("seven")
    listed = Value(0, visualization=3, options=["Gel", "AGM", "Wet", "Li-ion"], minimum=0.0, maximum=3.0)
    assert listed.parse("agm") == 1.0 and listed.parse("Li-ion") == 3.0
    with pytest.raises(ValueError):
        listed.parse("1.5")
    with pytest.raises(ValueError):
        listed.parse("NiCd")
    alarm = Value(0, visualization=4, minimum=0.0, maximum=1.0)
    assert alarm.parse("on") == 1.0 and alarm.parse("Off") == 0.0
    with pytest.raises(ValueError):
        Value(0, visualization=1, minimum=0.0, maximum=1.0).parse("on")  # a number is no switch


def test_an_action_or_flag_of_range_0_to_1_takes_on_and_off():
    from mvconnect.pages import VIS_ACTION, VIS_FLAG
    for vis in (VIS_ACTION, VIS_FLAG):
        assert Value(0, visualization=vis, minimum=0.0, maximum=1.0).parse("On") == 1.0
        assert Value(0, visualization=vis, minimum=0.0, maximum=1.0).parse("off") == 0.0
        with pytest.raises(ValueError):  # 0..2 has a third state: on says nothing about which
            Value(0, visualization=vis, minimum=0.0, maximum=2.0).parse("on")


def test_list_device_and_command_values_show_their_entries():
    from mvconnect.pages import VIS_COMMAND, VIS_DEVICE, VIS_LIST
    for kind in (VIS_LIST, VIS_DEVICE, VIS_COMMAND):
        value = Value(0, visualization=kind, options=["none", "1b:0cad9"], minimum=0.0, maximum=4.0)
        assert value.format(1.0) == "1b:0cad9" and value.format(0.0) == "none"
        assert value.format(3.0) == "3" and value.format(0.5) != "none"  # no such entry: the number
    assert Value(0, visualization=VIS_DEVICE, minimum=0.0, maximum=1.0).format(1.0) == "1"  # entries unknown


def test_a_write_whose_reply_is_lost_is_not_sent_again():
    class LosesTheFirstReply(FakeShunt):
        def reply(self, kind, data):
            if kind == 0x17 and not getattr(self, "lost", False):
                self.lost = True  # the device acts on the write, its answer never arrives
                return
            super().reply(kind, data)
    shunt = LosesTheFirstReply()
    r = PageReader(MasterBus(shunt, reply_timeout=0.02, retries=2).open(), SHUNT, reply_timeout=0.02, retries=2)
    try:
        with pytest.raises(TimeoutError):
            r.write_value(PAGE_BY_NAME["config"], 0, 24.0)
        assert [f.data[:2] for f in shunt.sent if f.kind == 0x37 and len(f.data) == 6] == [b"\0\0"]  # once
    finally:
        VALUES[0x37][0] = 12.0


def test_a_write_echoed_with_the_value_the_device_stored_is_acknowledged():
    class Rounds(FakeShunt):
        def reply(self, kind, data):
            if kind == 0x17 and len(data) == 6:  # it stores the nearest whole volt and says so
                stored = float(round(struct.unpack_from("<f", data, 2)[0]))
                VALUES[0x37][data[0]] = stored
                data = data[:2] + struct.pack("<f", stored)
            super().reply(kind, data)
    shunt = Rounds()
    r = PageReader(MasterBus(shunt, reply_timeout=0.02, retries=0).open(), SHUNT, reply_timeout=0.02, retries=0)
    try:
        assert r.write_value(PAGE_BY_NAME["config"], 0, 23.7) == 24.0
        assert len([f for f in shunt.sent if f.kind == 0x37 and len(f.data) == 6]) == 1
    finally:
        VALUES[0x37][0] = 12.0


@pytest.mark.parametrize("step, value, shown", [
    (0.5, 1.5, "1.5"), (0.25, 1.25, "1.25"), (0.05, 13.35, "13.35"), (0.05, 0.05, "0.05"), (0.01, 13.4, "13.40"),
    (0.1, 2.0, "2.0"), (struct.unpack("<f", struct.pack("<f", 0.1))[0], 2.5, "2.5"),  # a step as the bus gives it
    (0.00001, 1.0, "1.00000"),
])
def test_a_value_is_shown_with_the_decimals_its_step_has(step, value, shown):
    assert Value(0, step=step).format(value) == shown


def test_a_text_value_takes_no_more_than_a_string_write_sends():
    from mvconnect.pages import VIS_TEXT
    assert Value(0, visualization=VIS_TEXT, maximum=12.0).text_length == 12
    assert Value(0, visualization=VIS_TEXT, maximum=40.0).text_length == 16  # the write cuts at 16
    assert Value(0, visualization=VIS_TEXT).text_length == 16


def test_a_number_within_the_slack_past_a_bound_is_sent_as_the_bound():
    import struct
    maximum = struct.unpack("<f", struct.pack("<f", 48.0))[0]
    volts = Value(0, minimum=12.0, maximum=maximum, step=0.1)
    assert volts.parse("48.00004") == maximum  # 48.00004 would go out as the float32 48.000038
    assert volts.parse("11.99999") == 12.0


def test_a_bound_typed_as_shown_is_the_bound_though_the_bus_gives_it_as_float32():
    f32 = lambda x: struct.unpack("<f", struct.pack("<f", x))[0]
    amp = Value(4, name="Amp=Full", minimum=f32(0.1), maximum=f32(13.4), step=f32(0.1), writeable=True)
    assert f32(amp.parse("0.1")) == f32(0.1) and f32(amp.parse("13.4")) == f32(13.4)  # as shown, as sent
    for text in ("0.09", "13.5"):
        with pytest.raises(ValueError):
            amp.parse(text)


def test_non_finite_numbers_are_neither_written_nor_a_crash():
    volts = Value(0, minimum=0.0, maximum=10.0, step=1.0, writeable=True)
    for text in ("nan", "NaN", "inf", "-inf"):
        with pytest.raises(ValueError):
            volts.parse(text)
    for step in (1.0, 0.1, None):
        assert has(Value(0, step=step).format(float("inf")), "inf")
        assert has(Value(0, step=step).format(float("-inf")), "-inf")


def test_a_poll_answered_during_a_write_is_not_its_acknowledgement():
    class PolledWhileWritten(FakeShunt):
        def send(self, *frames, timeout=1.0):
            for f in frames:
                if f.kind == 0x37 and len(f.data) == 6:
                    self.sent.append(f)
                    self.reply(0x17, f.data[:2] + struct.pack("<f", 12.0))  # the EasyView's read of #0, answered
                    self.reply(0x27, f.data)  # and the write refused
                    continue
                super().send(f, timeout=timeout)
    r = PageReader(MasterBus(PolledWhileWritten(), reply_timeout=0.02, retries=0).open(), SHUNT,
                   reply_timeout=0.02, retries=0)
    with pytest.raises(ValueError):
        r.write_value(PAGE_BY_NAME["config"], 0, 30.0)


def test_a_forced_number_past_the_range_is_sent_for_the_device_to_judge(bus):
    system = System(bus)
    device = system.device("msh", timeout=0.02)
    table = system.tables(device, [PAGE_BY_NAME["config"]])["config"]
    with pytest.raises(Refused):
        system.write(device, table, table.values[0], "60", timeout=0.02)
    assert not [f for f in bus.link.sent if f.kind == 0x37 and len(f.data) == 6]
    with pytest.raises(ValueError) as e:  # the fake refuses what is past its range, as a device does
        system.write(device, table, table.values[0], "60", force=True, timeout=0.02)
    assert not isinstance(e.value, Refused)
    assert [f.data[2:] for f in bus.link.sent if f.kind == 0x37 and len(f.data) == 6] == [struct.pack("<f", 60.0)]


@pytest.fixture
def config(bus):
    """The system, the shunt and its config page, on the fake bus."""
    system = System(bus, use_cache=False)
    device = system.device("msh", timeout=0.02)
    return system, device, system.tables(device, [PAGE_BY_NAME["config"]])["config"]


def answering(bus, change, to=lambda f: f.kind == 0x37 and len(f.data) == 6 and f.data[2] != 0x14):
    """Have the fake shunt's answers to the frames *to* picks (by default a config value's write) pass through
    *change*(kind, data), which returns what it says instead, or None for silence. Return what undoes it."""
    link = bus.link
    send, reply = link.send, link.reply

    def sending(*frames, **options):
        for f in frames:
            if to(f):
                link.reply = lambda kind, data: (said := change(kind, data)) is not None and reply(*said)
            try:
                send(f, **options)
            finally:
                link.reply = reply
    link.send = sending
    return lambda: setattr(link, "send", send)


def test_a_reset_presses_no_button_and_writes_no_marker(config, bus):
    from mvconnect.pages import VIS_ACTION, VIS_COUNTER, VIS_LIST
    system, device, table = config
    nominal = table.values[0]
    button = dataclasses.replace(nominal, visualization=VIS_ACTION, minimum=0.0, maximum=1.0, default=1.0)
    with pytest.raises(ValueError):
        system.write_default(device, table, button, force=True)
    with pytest.raises(Refused):
        system.write_default(device, table, dataclasses.replace(nominal, visualization=VIS_COUNTER, default=12.0))
    assert dataclasses.replace(nominal, minimum=None, maximum=None, default=65535.0).default_value is None
    listed = dataclasses.replace(nominal, visualization=VIS_LIST, options=["A", "B", "C"], minimum=0.0, maximum=8.0)
    assert dataclasses.replace(listed, default=4.0).default_value is None
    assert dataclasses.replace(listed, default=2.0).default_value == 2.0
    assert not [f for f in bus.link.sent if f.kind == 0x37 and len(f.data) == 6]


def test_a_reset_of_a_read_only_value_is_refused_unless_forced(config):
    system, device, table = config
    VALUES[0x37][0] = 24.0
    nominal = dataclasses.replace(table.values[0], writeable=False)  # default 12
    with pytest.raises(Refused):
        system.write_default(device, table, nominal)
    assert VALUES[0x37][0] == 24.0
    assert system.write_default(device, table, nominal, force=True).after == "12"


def test_a_number_write_without_its_echo_is_read_back_with_a_warning(config, bus):
    system, device, table = config
    answering(bus, lambda kind, data: None)  # taken, echo lost
    written = system.write(device, table, table.values[0], "24")
    assert written.after == "24" and len(written.warnings) == 1


def test_a_write_the_device_stores_otherwise_is_warned_of(config, bus):
    system, device, table = config

    def clamped(kind, data):
        VALUES[0x37][0] = 36.0  # the device echoes what was sent, but keeps something else
        return kind, data
    answering(bus, clamped)
    written = system.write(device, table, table.values[0], "24")
    assert written.after == "36" and written.warnings


def test_a_device_value_is_written_as_its_entry_alone(config, bus):
    system, device, table = config
    ADDRESS_FRAMES.clear()
    written = system.write(device, table, table.values[5], "msh", timeout=0.02)
    assert written.after == str(SHUNT) and VALUES[0x37][5] == 1.0 and not ADDRESS_FRAMES
    sent = [f.data for f in bus.link.sent if f.kind == 0x37 and len(f.data) == 6]
    assert sent == [bytes.fromhex("0500") + struct.pack("<f", 1.0)]  # the entry's index, as any value; no second frame


def test_a_number_the_bus_cannot_carry_is_a_value_error_at_every_layer(config):
    system, device, table = config
    for value in (1e40, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            system.write_value(device, table, 0, value)  # not OverflowError, as through the server too


def test_no_number_the_bus_cannot_carry_is_sent_even_when_forced(bus):
    system = System(bus)
    device = system.device("msh", timeout=0.02)
    table = system.tables(device, [PAGE_BY_NAME["config"]])["config"]
    sent = len(bus.link.sent)
    for text in ("nan", "inf", "1e39", "lots"):
        with pytest.raises(ValueError) as e:
            system.write(device, table, table.values[0], text, force=True, timeout=0.02)
        assert not isinstance(e.value, Refused)  # not a check that --force skips
        with pytest.raises(ValueError):
            Value(0).parse(text)  # nor without --force, on a value without a range
    assert not [f for f in bus.link.sent[sent:] if f.kind == 0x37 and len(f.data) == 6]
    with pytest.raises(ValueError):
        bus.login(SHUNT, 1, 1e40)


def test_write_value_echo_and_refusal(bus):
    r = PageReader(bus, SHUNT, reply_timeout=0.02, retries=0)
    cfg = PAGE_BY_NAME["config"]
    assert r.write_value(cfg, 0, 24.0) == 24.0
    assert r.read_value(cfg, 0) == 24.0
    with pytest.raises(ValueError):
        r.write_value(cfg, 0, 60.0)  # above the maximum: the device refuses
    assert r.read_value(cfg, 0) == 24.0
    with pytest.raises(ValueError):
        r.write_value(PAGE_BY_NAME["alarm"], 0, 1.0)
    VALUES[0x37][0] = 12.0
    sent = [f for f in bus.link.sent if f.kind == 0x37 and len(f.data) == 6]
    assert sent[0].data == bytes.fromhex("0000") + struct.pack("<f", 24.0)


def test_write_string_in_chunks_with_a_terminator(bus):
    bus.write_string(SHUNT, 219, "Bow Bank")
    assert bus.read_string(SHUNT, 219) == "Bow Bank"
    sent = [f.data for f in bus.link.sent if f.kind == KIND_REQUEST and f.data[0] == 0x30 and len(f.data) > 4]
    assert sent == [b"\x30\xdb\x00\x00Bow ", b"\x30\xdb\x00\x01Bank", b"\x30\xdb\x00\x02\x00"]
    bus.write_string(SHUNT, 219, "A name longer than sixteen characters")
    assert bus.read_string(SHUNT, 219) == "A name longer th"  # cut to 16, as MasterAdjust does
    bus.write_string(SHUNT, 219, "MSH House Bank")
    assert bus.read_string(SHUNT, 219) == "MSH House Bank"


@pytest.mark.parametrize("content", ["{", "[]", '{"pages": 1}', '{"pages": {"config": {"page": "config"}}}',
                                     '{"pages": {"config": {"page": "config", "values": [{"bogus": 1}], '
                                     '"groups": []}}}'])
def test_a_malformed_cache_is_read_again_not_a_crash(bus, content):
    system = System(bus)
    device = system.device("1b:0cad9", timeout=0.02)
    cache._path(device.serial, device.version).parent.mkdir(parents=True, exist_ok=True)
    cache._path(device.serial, device.version).write_text(content)
    assert cache.load_tables(device.serial, device.version) is None
    assert system.tables(device, [PAGE_BY_NAME["config"]])["config"].values[2].name == "Battery type"
    assert sorted(cache.load_tables(device.serial, device.version)) == ["config"]  # and written anew


def test_a_device_without_a_serial_number_is_warned_of(bus, caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="mvconnect.system"):
        System(bus).devices(timeout=0.02)
        assert not logged(caplog, "WARNING", "serial")  # a device as it should be
        STRINGS[0] = ""  # the shunt answers for its serial number with nothing
        try:
            System(bus).devices(timeout=0.02)
        finally:
            STRINGS[0] = "E819L0421"
    assert logged(caplog, "WARNING", "1b:0cad9", "serial number")


def test_a_device_without_serial_or_version_is_never_cached(bus):
    from mvconnect.masterbus import Device
    cfg = PAGE_BY_NAME["config"]
    for unknown in (Device(SHUNT, serial="", version="2.1"), Device(SHUNT, serial="E819L0421", version="")):
        System(bus).tables(unknown, [cfg])
        assert not cache.cache_dir().exists() or not any(cache.cache_dir().iterdir())
        sent = len(bus.link.sent)
        System(bus).tables(unknown, [cfg])
        assert len(bus.link.sent) - sent > 10  # read again, not taken from another device's file


def test_a_single_page_read_is_cached_and_merged_with_the_rest(bus, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    system = System(bus)
    device = system.device("1b:0cad9", timeout=0.02)
    cfg, mon = PAGE_BY_NAME["config"], PAGE_BY_NAME["monitor"]
    system.tables(device, [cfg])
    assert sorted(cache.load_tables(device.serial, device.version)) == ["config"]  # one page is worth saving
    sent = len(bus.link.sent)
    assert System(bus).tables(device, [cfg])["config"].values[2].name == "Battery type"
    assert [f.data for f in bus.link.sent[sent:]] == [bytes([0x08, cfg.version])]  # from the cache: its version only
    System(bus).tables(device, [mon])
    assert sorted(cache.load_tables(device.serial, device.version)) == ["config", "monitor"]  # merged, not replaced
    sent = len(bus.link.sent)
    System(bus).tables(device, [cfg, mon])
    assert [f.data for f in bus.link.sent[sent:]] == [bytes([0x08, cfg.version]), bytes([0x08, mon.version])]


def test_refresh_reads_each_page_once_and_updates_the_cache(bus, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    device = System(bus).device("1b:0cad9", timeout=0.02)
    cfg = PAGE_BY_NAME["config"]
    System(bus).tables(device, [cfg])
    VALUES_BEFORE = cache.load_tables(device.serial, device.version)["config"].values[0].name
    CFG_ITEMS[0][0x28] = 71  # the device renames value 0 behind the cache's back
    try:
        assert System(bus).tables(device, [cfg])["config"].values[0].name == VALUES_BEFORE  # the cache wins
        fresh = System(bus, refresh=True)
        assert fresh.tables(device, [cfg])["config"].values[0].name == "Battery capacity"  # read afresh
        sent = len(bus.link.sent)
        fresh.tables(device, [cfg])
        assert [f.data for f in bus.link.sent[sent:]] == [bytes([0x08, cfg.version])]  # once per System
        assert cache.load_tables(device.serial, device.version)["config"].values[0].name == "Battery capacity"
    finally:
        CFG_ITEMS[0][0x28] = 70


def test_page_version_decides_whether_the_cache_is_used(bus, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    system = System(bus)
    device = system.device("1b:0cad9", timeout=0.02)
    cfg = PAGE_BY_NAME["config"]
    table = system.tables(device)["config"]  # every page, so that the cache is written
    assert table.version == 7

    def attribute_reads():
        return sum(1 for f in bus.link.sent if f.kind == 0x38)

    before = attribute_reads()
    assert system.tables(device, [cfg])["config"].version == 7
    assert attribute_reads() == before  # same version: the cache served it
    COUNTS[0x1E] = 8  # the device says its config page changed (the lock flipped, say)
    try:
        CFG_ITEMS[0][0x0B] = 0
        again = system.tables(device, [cfg])["config"]
    finally:
        COUNTS[0x1E] = 7
        CFG_ITEMS[0][0x0B] = 1
    assert again.version == 8 and again.values[0].writeable is False and attribute_reads() > before
    # The version moved again (back to 7): read again, and the flag is back too.
    later = system.tables(device, [cfg])["config"]
    assert later.version == 7 and later.values[0].writeable is True


def test_a_page_whose_version_is_not_answered_is_read_again(bus, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    system = System(bus)
    device = system.device("1b:0cad9", timeout=0.02)
    system.tables(device)  # into the cache
    before = sum(1 for f in bus.link.sent if f.kind == 0x38)
    undo = answering(bus, lambda kind, data: (KIND_NA, data), to=lambda f: f.data[:2] == b"\x08\x1e")  # none
    system.tables(device, [PAGE_BY_NAME["config"]])
    undo()
    assert sum(1 for f in bus.link.sent if f.kind == 0x38) == before  # 'not available': the cache serves
    answering(bus, lambda kind, data: None, to=lambda f: f.data[:2] == b"\x08\x1e")  # silence
    system.tables(device, [PAGE_BY_NAME["config"]])
    assert sum(1 for f in bus.link.sent if f.kind == 0x38) > before  # unknown: the page is read again


def test_a_page_read_asks_for_each_attribute_once(bus):
    r = PageReader(bus, SHUNT, reply_timeout=0.02, retries=0)
    sent = len(bus.link.sent)
    r.read_page(PAGE_BY_NAME["config"])
    asked = [bytes(f.data) for f in bus.link.sent[sent:] if f.kind == 0x38]
    assert asked and len(asked) == len(set(asked))  # the junk check's name and unit reads are not repeated


def test_a_page_read_reads_each_string_once(bus):
    r = PageReader(bus, SHUNT, reply_timeout=0.02, retries=0)
    sent = len(bus.link.sent)
    r.read_page(PAGE_BY_NAME["config"])
    chunks = [bytes(f.data[:4]) for f in bus.link.sent[sent:] if f.kind == KIND_REQUEST and f.data[0] == 0x30]
    assert chunks and len(chunks) == len(set(chunks))  # "Date" names two values and is read once


def test_page_ends_at_junk_items_named_after_the_serial(bus):
    r = PageReader(bus, SHUNT, reply_timeout=0.02, retries=0)
    cfg = PAGE_BY_NAME["config"]
    COUNTS[0x05], JUNK_FROM[0] = 40, 8  # the device counts 40 values, the real ones are 0..7
    steps = []
    try:
        table = r.read_page(cfg, progress=lambda done, total: steps.append((done, total)))
    finally:
        COUNTS[0x05], JUNK_FROM[0] = 8, None
    assert sorted(table.values) == list(range(8))
    assert table.values[7].name == "Device name" and not any(i.name == "E819L0421" for i in table.values.values())
    asked = max(f.data[1] | f.data[2] << 8 for f in bus.link.sent if f.kind == 0x38 and len(f.data) >= 3)
    assert asked == 9  # it stopped after the second junk value, not at 40
    assert steps[-1][0] == steps[-1][1]  # and the progress ends complete


def test_group_length_bounds_its_members(bus):
    """A group's value is its length; the fake's config group 1 answers four members but says two."""
    r = PageReader(bus, SHUNT, reply_timeout=0.02, retries=0)
    cfg = PAGE_BY_NAME["config"]
    assert r.group_length(cfg, 1) == 2 and r.group_length(cfg, 0) == 1
    assert r.read_group(cfg, 1, n_values=8).values == [1, 2]
    asked = [f.data[3] for f in bus.link.sent if f.kind == 0x39 and len(f.data) == 4 and f.data[0] == 0x03
             and f.data[1] == 1]
    assert max(asked) == 1  # it never asked for member 2
    NO_LENGTH[0].add(1)
    try:
        bus.link.sent.clear()
        assert r.read_group(cfg, 1, n_values=8).values == [1, 2]  # no length: the old stop rules
    finally:
        NO_LENGTH[0].clear()


def test_group_member_number_stays_a_byte(bus):
    """A page with more than 256 values and no group length: a group that keeps answering ends at 255."""
    r = PageReader(bus, SHUNT, reply_timeout=0.02, retries=0)
    asked = []

    def attr_u16(kind, attr, index, sub=None):
        asked.append(sub)
        return sub if attr == 0x03 else 0  # member n is value n, without end

    r.attr_u16 = attr_u16
    r.attr_string = lambda *a, **k: "Float"
    r.group_length = lambda page, index: None
    group = r.read_group(PAGE_BY_NAME["config"], 0, n_values=300)
    assert group.values == list(range(256)) and max(asked) == 255


def test_attributes_flags_and_texts(bus):
    r = PageReader(bus, SHUNT, reply_timeout=0.02, retries=0)
    cfg = PAGE_BY_NAME["config"]
    volts = r.read_attributes(cfg, 0)
    assert (volts.gray, volts.eventable, volts.long_name) == (True, 2, "Nominal battery voltage")
    assert volts.description == "The voltage the capacity is rated at"
    assert volts.flags == "nweg"
    described = lines(volts.describe(12.0))
    assert has(described, "Nominal battery voltage", "The voltage the capacity is rated at")
    assert line_with(described, "eventable", 2)
    assert lines(dataclasses.replace(volts, gray=False).describe(12.0)) != described  # greying is shown
    capacity = r.read_attributes(cfg, 1)
    assert (capacity.flags, capacity.gray, capacity.eventable, capacity.long_name) == ("n---", False, 0, "")
    assert not line_with(lines(capacity.describe(1.0)), "eventable")
    assert r.read_attributes(cfg, 2).flags == "l---" and r.read_attributes(cfg, 5).flags == "Dw--"
    assert Value(0, visualization=42, writeable=True, eventable=1).flags == "?we-"
    again = table_from_dict(table_to_dict(r.read_page(cfg)))
    assert again.values[0].description == volts.description and again.values[0].flags == "nweg"


def test_text_item_reads_and_writes_its_string(bus):
    from mvconnect.pages import VIS_TEXT
    r = PageReader(bus, SHUNT, reply_timeout=0.02, retries=0)
    cfg = PAGE_BY_NAME["config"]
    name = r.read_attributes(cfg, 7)
    assert (name.visualization, name.writer, name.text_length) == (VIS_TEXT, "text", 12)
    assert r.read_text(cfg, 7) == (219, "MSH House Bank")
    assert has(lines(name.describe(219.0, "MSH House Bank")), "MSH House Bank")
    assert line_with(lines(name.describe(219.0)), "writeable", 12)
    assert r.write_text(cfg, 7, "MSH Bow Bank") == "MSH Bow Bank"
    assert bus.read_string(SHUNT, 219) == "MSH Bow Bank"
    bus.write_string(SHUNT, 219, "MSH House Bank")
    VALUES[0x37][7] = float("nan")
    with pytest.raises(ValueError):
        r.write_text(cfg, 7, "x")  # no string to write
    VALUES[0x37][7] = 219.0
    system = System(bus, use_cache=False)
    device = system.device("1b:0cad9", timeout=0.02)
    table = system.tables(device, [cfg])["config"]
    assert system.read_texts(device, table, [0, 7]) == {7: "MSH House Bank"}


def test_an_empty_entry_is_an_entry(bus):
    r = PageReader(bus, SHUNT, reply_timeout=0.02, retries=0)
    saved = dict(CFG_ITEMS[2])
    STRINGS.update({0xE0: "", 0xE1: "Config locked"})
    CFG_ITEMS[2] = {0x28: 72, 0x02: 3, 0x08: 1.0, (0x26, 0): 0xE0, (0x26, 1): 0xE1, (0x26, 2): 0xE0}
    try:
        lock = r.read_attributes(PAGE_BY_NAME["config"], 2)
        assert lock.options == ["", "Config locked"]  # the empty first entry kept, the empty last one not
        assert lock.format(1.0) == "Config locked" and lock.parse("config locked") == 1.0
    finally:
        CFG_ITEMS[2] = saved


def test_options_with_the_serial_numbers_id_are_junk(bus):
    """The APR answers option texts it has none for with its serial number's string id."""
    r = PageReader(bus, SHUNT, reply_timeout=0.02, retries=0)
    cfg = PAGE_BY_NAME["config"]
    CFG_ITEMS[2][(0x26, 2)] = 0  # string 0 is the serial number on the fake shunt
    try:
        assert r.read_attributes(cfg, 2).options == ["Gel", "AGM"]  # it asks for the serial, and stops at the junk
        r.serial_sid, r._asked_serial = None, True  # a device that did not say its serial number
        assert r.read_attributes(cfg, 2).options[2] == "E819L0421"  # without knowing the serial, it would not
        # The list read on its own, as a write reads it live, is cut the same way: through the system too.
        battery_type = Value(2, visualization=3)
        assert PageReader(bus, SHUNT, reply_timeout=0.02, retries=0).read_options(cfg, battery_type) == ["Gel", "AGM"]
        system = System(bus, use_cache=False)
        device = system.device("msh", timeout=0.02)
        table = system.tables(device, [cfg])["config"]
        assert system.read_options(device, table, table.values[2]) == ["Gel", "AGM"] == table.values[2].options
    finally:
        CFG_ITEMS[2][(0x26, 2)] = 0xC5


def test_device_and_command_values_list_their_entries_from_their_own_attributes(bus):
    from mvconnect.pages import VIS_COMMAND, VIS_DEVICE
    r = PageReader(bus, SHUNT, reply_timeout=0.02, retries=0)
    cfg = PAGE_BY_NAME["config"]
    target, command = r.read_attributes(cfg, 5), r.read_attributes(cfg, 6)
    assert (target.visualization, target.options) == (VIS_DEVICE, [])  # the entries follow the bus: none kept
    assert (command.visualization, command.options) == (VIS_COMMAND, [])
    bus.link.sent.clear()
    entries, commands = r.read_options(cfg, target), r.read_options(cfg, command)
    assert entries == ["none", str(SHUNT), str(ELSEWHERE)] and commands == ["Switch", "Toggle"]
    asked = [f.data for f in bus.link.sent if f.kind == 0x38 and f.data[0] in (0x07, 0x0F, 0x20, 0x26)]
    # the maximum afresh, then as many entries as it says, through 0x20 and 0x0F; never through 0x26
    assert asked == ([bytes([0x07, 5, 0])] + [bytes([0x20, 5, 0, n]) for n in range(3)]
                     + [bytes([0x07, 6, 0])] + [bytes([0x0F, 6, 0, n]) for n in range(2)])
    import dataclasses
    target, command = dataclasses.replace(target, options=entries), dataclasses.replace(command, options=commands)
    assert target.option_for("none") == 0 and target.option_for(str(SHUNT)) == 1 and target.option_for("msh") is None
    assert line_with(lines(target.describe(1.0)), "writeable", "device", "none")
    assert line_with(lines(command.describe(0.0)), "writeable", "yes")
    assert target.format(0.0) == "none" and target.format(1.0) == str(SHUNT) and command.format(1.0) == "Toggle"


def test_an_unavailable_entry_is_empty_and_without_a_count_there_are_no_entries(bus):
    r = PageReader(bus, SHUNT, reply_timeout=0.02, retries=0)
    cfg, target = PAGE_BY_NAME["config"], Value(5, visualization=9)
    del CFG_ITEMS[5][(0x20, 1)]  # 'not available' for the second entry: an empty slot, as the maximum counts it
    assert r.read_options(cfg, target) == ["none", "none", str(ELSEWHERE)]
    CFG_ITEMS[5][(0x20, 1)] = b""  # an answer too short for an address ends the entries
    assert r.read_options(cfg, target) == ["none"]
    del CFG_ITEMS[5][0x07]  # without a maximum there are no entries: the CHG answers past its table with junk
    del CFG_ITEMS[5][(0x20, 1)]
    assert r.read_options(cfg, target) == [] and r.read_options(cfg, Value(6, visualization=10)) == ["Switch", "Toggle"]
    del CFG_ITEMS[6][0x07]
    assert r.read_options(cfg, Value(6, visualization=10)) == []


def test_a_list_is_read_for_as_many_entries_as_its_maximum_says(bus):
    r = PageReader(bus, SHUNT, reply_timeout=0.02, retries=0)
    cfg = PAGE_BY_NAME["config"]
    assert r.read_attributes(cfg, 2).options == ["Gel", "AGM", "Wet", "Li-ion"]  # no maximum: until it has none
    CFG_ITEMS[2][0x07] = 2.0
    assert r.read_attributes(cfg, 2).options == ["Gel", "AGM"]


def test_a_device_value_takes_a_device_among_its_entries_or_none(bus):
    import dataclasses

    from mvconnect.system import Refused
    system = System(bus)
    device = system.device("msh", timeout=0.02)
    table = system.tables(device, [PAGE_BY_NAME["config"]])["config"]
    target = table.values[5]  # Event 1 target: none, the shunt, a charger that is not on the bus
    with pytest.raises(AddressError, match=mentions(str(SHUNT))):
        system.write(device, table, target, "nobody", timeout=0.02)  # no such device: the entries are named
    assert system.write(device, table, target, str(ELSEWHERE), timeout=0.02).after == str(ELSEWHERE)
    assert VALUES[0x37][5] == 2.0  # an entry as shown, on the bus or not
    assert system.write(device, table, target, "none", timeout=0.02).after == "none" and VALUES[0x37][5] == 0.0
    CFG_ITEMS[5][(0x20, 1)] = NO_ENTRY  # the shunt is no entry any more
    for force in (False, True):  # not among the entries: an error, forced or not; a number is no device either
        with pytest.raises(AddressError, match=mentions("not among")):
            system.write(device, table, target, "msh", force=force, timeout=0.02)
        with pytest.raises(AddressError):
            system.write(device, table, target, "1", force=force, timeout=0.02)
    assert VALUES[0x37][5] == 0.0
    with pytest.raises(Refused, match=mentions("read-only")):  # the writeable flag is what force skips
        system.write(device, table, dataclasses.replace(target, writeable=False), "none", timeout=0.02)


def test_a_device_value_s_write_is_judged_by_the_entry_the_device_holds(bus):
    """The CHG keeps the current target first and its value at 0, so the entries say whether a write took."""
    system = System(bus)
    device = system.device("msh", timeout=0.02)
    table = system.tables(device, [PAGE_BY_NAME["config"]])["config"]
    target = table.values[5]  # none, the shunt, elsewhere
    link, send = bus.link, bus.link.send

    def rearranging(*frames, **options):  # a write of entry n puts that entry first and the value back to 0
        f = frames[0]
        if f.kind == 0x37 and len(f.data) == 6 and f.data[:2] == b"\x05\x00":
            n = int(struct.unpack_from("<f", f.data, 2)[0])
            entries = [CFG_ITEMS[5][(0x20, i)] for i in range(3)]
            entries.insert(0, entries.pop(n))
            CFG_ITEMS[5].update({(0x20, i): e for i, e in enumerate(entries)})
            link.reply(0x37 - 0x20, f.data[:2] + struct.pack("<f", 0.0))  # echoes what it holds: 0
            return
        return send(*frames, **options)

    link.send = rearranging
    try:
        written = system.write(device, table, target, "msh", timeout=0.02)
        assert (written.before, written.after, written.took) == ("none", str(SHUNT), True) and not written.warnings
        assert system.read_options(device, table, target) == [str(SHUNT), "none", str(ELSEWHERE)]
        written = system.write(device, table, target, "none", timeout=0.02)  # the entries as the device has them now
        assert (written.after, written.took) == ("none", True)
    finally:
        del link.send


def test_none_clears_a_target_whose_entries_have_no_empty_one(bus):
    """The CHG lists an empty entry only while no target is set; the maximum, just past the entries, clears it."""
    system = System(bus)
    device = system.device("msh", timeout=0.02)
    CFG_ITEMS[5].update({0x07: 2.0, (0x20, 0): address_bytes(SHUNT), (0x20, 1): address_bytes(ELSEWHERE)})
    del CFG_ITEMS[5][(0x20, 2)]
    table = system.tables(device, [PAGE_BY_NAME["config"]])["config"]
    target = table.values[5]  # the shunt, elsewhere; maximum 2
    link, send = bus.link, bus.link.send

    def clearing(*frames, **options):  # a write of the maximum empties the target: an empty entry first, three entries
        f = frames[0]
        if f.kind == 0x37 and len(f.data) == 6 and f.data[:2] == b"\x05\x00":
            assert struct.unpack_from("<f", f.data, 2)[0] == 2.0
            CFG_ITEMS[5].update({0x07: 3.0, (0x20, 0): NO_ENTRY, (0x20, 1): address_bytes(SHUNT),
                                 (0x20, 2): address_bytes(ELSEWHERE)})
            link.reply(0x37 - 0x20, f.data[:2] + struct.pack("<f", 0.0))
            return
        return send(*frames, **options)

    link.send = clearing
    try:
        written = system.write(device, table, target, "none", timeout=0.02)
        assert (written.before, written.after, written.took) == (str(SHUNT), "none", True)
    finally:
        del link.send
    # A device value that holds a plain index, with no empty entry, takes the value past its entries too, and
    # the read-back says the write did not take: it holds an index no entry backs.
    CFG_ITEMS[5].update({0x07: 2.0, (0x20, 0): address_bytes(SHUNT), (0x20, 1): address_bytes(ELSEWHERE)})
    del CFG_ITEMS[5][(0x20, 2)]
    written = system.write(device, table, target, "none", timeout=0.02)
    assert (written.after, written.took) == ("2", False) and VALUES[0x37][5] == 2.0 and written.warnings


def test_a_default_is_never_written_to_a_device_or_command_value(bus):
    system = System(bus)
    device = system.device("msh", timeout=0.02)
    CFG_ITEMS[5][0x09] = CFG_ITEMS[6][0x09] = 1.0  # even with a default the device gives
    table = system.tables(device, [PAGE_BY_NAME["config"]])["config"]
    for index in (5, 6):
        with pytest.raises(ValueError, match=mentions("no default")):
            system.write_default(device, table, table.values[index], force=True)
    assert not [f for f in bus.link.sent if f.kind == 0x37 and len(f.data) == 6]


def test_a_command_value_is_written_by_its_entries_as_the_device_has_them_now(bus):
    from mvconnect.system import Refused
    system = System(bus)
    device = system.device("msh", timeout=0.02)
    table = system.tables(device, [PAGE_BY_NAME["config"]])["config"]  # Switch, Toggle
    CFG_ITEMS[6][(0x0F, 1)] = 0xD1  # since then: Switch, House Bank (the target changed, and its commands with it)
    with pytest.raises(Refused, match=mentions("Switch", "House Bank")):
        system.write(device, table, table.values[6], "toggle", timeout=0.02)
    assert system.write(device, table, table.values[6], "house bank", timeout=0.02).after == "House Bank"
    assert VALUES[0x37][6] == 1.0


def test_a_text_the_device_does_not_answer_for_is_left_out_with_a_warning(bus, caplog):
    system = System(bus, use_cache=False)
    device = system.device("1b:0cad9", timeout=0.02)
    table = system.tables(device, [PAGE_BY_NAME["config"]])["config"]
    reply = bus.link.reply
    bus.link.reply = lambda kind, data: None if data[0] == 0x30 else reply(kind, data)  # strings unanswered
    try:
        assert system.read_texts(device, table, [0, 7]) == {}
    finally:
        del bus.link.reply
    assert logged(caplog, "WARNING", "#7")


def test_a_text_a_device_cannot_hold_is_not_written(config, bus):
    system, device, table = config
    for force in (False, True):
        with pytest.raises(ValueError):
            system.write(device, table, table.values[7], "Bow \u2600", force=force)  # a sun, not latin-1
    assert STRINGS[219] == "MSH House Bank" and not [f for f in bus.link.sent if f.data[:1] == b"\x30"
                                                     and len(f.data) > 4]


def test_a_text_write_the_device_does_not_acknowledge_is_read_back(bus, monkeypatch):
    system = System(bus, use_cache=False)
    device = system.device("1b:0cad9", timeout=0.02)
    table = system.tables(device, [PAGE_BY_NAME["config"]])["config"]
    link, send = bus.link, bus.link.send

    def last_chunk_unanswered(*frames, timeout=1.0):  # the device takes the text, its last answer is lost
        if any(f.kind == KIND_REQUEST and f.data[0] == 0x30 and 0 in f.data[4:] for f in frames):
            link.reply = lambda kind, data: None
            try:
                return send(*frames, timeout=timeout)
            finally:
                del link.reply
        return send(*frames, timeout=timeout)

    monkeypatch.setattr(link, "send", last_chunk_unanswered)
    try:
        written = system.write(device, table, table.values[7], "MSH Bow Bank")
        assert (written.before, written.after) == ("MSH House Bank", "MSH Bow Bank") and written.warnings
    finally:
        STRINGS[219] = "MSH House Bank"


def test_a_command_value_is_written_as_its_entry(bus):
    from mvconnect.system import Refused
    system = System(bus)
    device = system.device("msh", timeout=0.02)
    table = system.tables(device, [PAGE_BY_NAME["config"]])["config"]
    command = table.values[6]  # Event 1 command: Switch, Toggle
    ADDRESS_FRAMES.clear()
    written = system.write(device, table, command, "toggle", timeout=0.02)
    assert VALUES[0x37][6] == 1.0 and written.after == "Toggle" and not ADDRESS_FRAMES
    with pytest.raises(Refused, match=mentions("Switch", "Toggle")):
        system.write(device, table, command, "1", timeout=0.02)  # an entry's number is no name, as for any list


def test_a_device_value_is_written_by_the_entries_as_the_device_has_them_now(bus):
    system = System(bus)
    device = system.device("msh", timeout=0.02)
    table = system.tables(device, [PAGE_BY_NAME["config"]])["config"]  # none, the shunt, elsewhere
    CFG_ITEMS[5].update({(0x20, 1): address_bytes(ELSEWHERE), (0x20, 2): address_bytes(SHUNT)})  # swapped since
    written = system.write(device, table, table.values[5], "house bank", timeout=0.02)
    assert VALUES[0x37][5] == 2.0 and written.after == str(SHUNT)


def test_a_page_is_named_in_any_case_and_an_unknown_one_is_an_address_error(config):
    system, device, table = config
    assert [t.page.name for t, v in system.find(device, "nominal", "Config")] == ["config"]
    with pytest.raises(AddressError):
        system.find(device, "", "settings")


def test_resolve_value_for_writing(bus, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    system = System(bus)
    device = system.device("1b:0cad9", timeout=0.02)
    table, value = system.resolve_value(device, "battery type", "config")
    assert value.index == 2 and table.page.name == "config"
    assert system.resolve_value(device, "0", "config")[1].name == "Nominal voltage"
    assert system.resolve_value(device, "nominal")[1].name == "Nominal voltage"  # unique across the pages
    assert system.resolve_value(device, "batt 2")[1].name == "Battery type"  # the index pins it
    for words, page in (("date", "config"), ("nowhere", "config"), ("99", "config"), ("", "config"), ("1", None)):
        with pytest.raises(AddressError):
            system.resolve_value(device, words, page)


def test_describe():
    listed = Value(0, name="Battery type", visualization=3, options=["Gel", "AGM", "Wet", "Li-ion"],
                  minimum=0.0, maximum=3.0, step=1.0, default=1.0)
    text = lines(listed.describe(3.0))
    assert line_with(text, "Li-ion") and line_with(text, "Gel", "AGM", "Wet", "Li-ion")  # the value, the options
    assert line_with(text, "default", "AGM") and line_with(text, "writeable", "no")
    volts = Value(0, unit="V", minimum=10.0, maximum=16.0, step=0.1, default=13.4, writeable=True)
    text = lines(volts.describe(13.2))
    assert line_with(text, 13.2) and line_with(text, "V") and line_with(text, "range", "10.0", "16.0", "0.1")
    assert line_with(text, "default", 13.4) and line_with(text, "writeable", "yes")
    minutes = Value(0, unit="Minutes", minimum=0.0, maximum=59.0, step=1.0, default=0.0, writeable=True)
    assert line_with(lines(minutes.describe(7.0)), "range", 0, 59)
    master = Value(0, visualization=9, minimum=0.0, maximum=1.0, step=1.0)
    assert line_with(lines(master.describe(0.0)), "range", 0, 1)
    none = Value(0, minimum=0.0, maximum=2.0, step=1.0, default=65535.0)
    assert none.default_value is None and "65535" not in lines(none.describe(0.0))  # no default, not a bogus one


def test_writer_by_type():
    from mvconnect.pages import VIS_COMMAND, VIS_DEVICE, VIS_TEXT
    assert Value(0, visualization=1).writer == "float"
    assert Value(0, visualization=VIS_TEXT).writer == "text"
    assert Value(0, visualization=VIS_DEVICE).writer == "float"  # the entry's index, as MasterAdjust writes it
    assert Value(0, visualization=VIS_COMMAND).writer == "float"
    assert Value(0, visualization=42).writer == "float" and has(Value(0, visualization=42).type_name, 42)
    name = Value(0, name="Device name", visualization=VIS_TEXT, minimum=0.0, maximum=12.0, writeable=True)
    assert line_with(lines(name.describe(1.0)), "writeable", 12)
    target = Value(0, visualization=VIS_DEVICE, minimum=0.0, maximum=4.0, writeable=True)
    assert line_with(lines(target.describe(0.0)), "writeable", "device")
    assert line_with(lines(Value(0, visualization=VIS_DEVICE).describe(0.0)), "writeable", "no")


@pytest.mark.parametrize("text", ["2", "2 minute", "0", "3", "1.5", "always", "two minutes"])
def test_a_list_value_takes_only_its_option_names(text):
    """As the display's standby time: '2' is no option, not index 2 (10 minutes), and the refusal lists them."""
    standby = Value(11, name="Standby time", visualization=3, minimum=0.0, maximum=3.0, writeable=True,
                    options=["2 minutes", "5 minutes", "10 minutes", "Always on"])
    with pytest.raises(ValueError, match=mentions(text, "2 minutes", "5 minutes", "10 minutes", "Always on")):
        standby.parse(text)
    assert standby.parse("2 Minutes") == 0.0 and standby.parse(" always on ") == 3.0


def test_a_list_value_given_a_number_writes_nothing(config, bus):
    """Through System.write too: refused before anything is sent, and only --force sends the number as it is."""
    system, device, table = config
    battery_type = dataclasses.replace(table.values[2], writeable=True)  # Gel, AGM, Wet, Li-ion
    with pytest.raises(Refused, match=mentions("Gel", "Li-ion")):
        system.write(device, table, battery_type, "3")
    assert not [f for f in bus.link.sent if f.kind == 0x37 and len(f.data) == 6]
    try:
        assert system.write(device, table, battery_type, "3", force=True).after == "Li-ion"
    finally:
        VALUES[0x37][2] = 3.0


def test_date_values_show_and_take_a_date():
    from mvconnect.pages import VIS_DATE, date_text, date_value
    date = Value(0, visualization=VIS_DATE)
    assert date_text(843000.0) == "2026-05-24" and date_value("2026-05-24") == 843000.0
    assert date.format(843000.0) == "843000 (2026-05-24)"
    assert date.parse("2026-05-24") == 843000.0 and date.parse("843000") == 843000.0
    for no_date in (0.0, 5.0, 843000.5, float("inf"), 2026 * 416 + 13 * 32 + 1, 2026 * 416 + 0 * 32 + 1):
        assert date_text(no_date) is None
    assert date.format(5.0) == "5" and date.format(float("nan")) == "n/a"
    for bad in ("2026-13-01", "2026-05", "x-y-z", "2026-05-00"):
        with pytest.raises(ValueError):
            date_value(bad)
    assert Value(0).format(843000.0) == "843000"  # a number is just a number


def test_counter_values_show_a_duration_from_a_minute_up():
    from mvconnect.pages import VIS_COUNTER, duration_text
    counter = Value(0, visualization=VIS_COUNTER)
    assert duration_text(54496) == "15:08:16" and duration_text(90061) == "1d 1:01:01"
    assert counter.format(54496.0) == "54496 (15:08:16)" and counter.format(60.0) == "60 (0:01:00)"
    assert counter.format(59.0) == "59" and counter.format(0.0) == "0" and counter.format(61.5) == "61.5"
    assert Value(0).format(54496.0) == "54496"


@pytest.fixture
def switch(config):
    """The system, the shunt, its config page and value 7 made a button of range 0..1, whose next index is no
    value of the page; the fake's value 7 is restored afterwards."""
    from mvconnect.pages import VIS_ACTION
    system, device, table = config
    kept = VALUES[0x37][7]
    ADDRESS_FRAMES.clear()
    button = dataclasses.replace(table.values[7], visualization=VIS_ACTION, minimum=0.0, maximum=1.0)
    yield system, device, table, button
    VALUES[0x37][7] = kept


def test_a_switch_write_names_the_writer_at_the_next_index(switch, bus):
    system, device, table, button = switch
    writer = bytes([8, 0, 0x14, 0x34, 0x12, 0x00])  # the host address 14:01234, low field LE
    written = system.write(device, table, button, "on", timeout=0.02)
    assert written.took and VALUES[0x37][7] == 1.0 and not written.warnings and ADDRESS_FRAMES == [writer]
    written = system.write(device, table, button, "off", force=True, timeout=0.02)  # forced or not
    assert written.took and VALUES[0x37][7] == 0.0 and not written.warnings and ADDRESS_FRAMES == [writer, writer]
    sent = [f.data for f in bus.link.sent if f.kind == 0x37 and len(f.data) == 6]
    assert sent[-2][:2] == bytes([7, 0]) and sent[-1] == writer  # the value first, then the writer
    nominal = table.values[0]  # a number: no switch, no frame
    system.write(device, table, nominal, "24", timeout=0.02)
    assert ADDRESS_FRAMES == [writer, writer]


def test_no_writer_is_named_where_the_next_index_is_a_value(config, bus):
    from mvconnect.pages import VIS_ACTION, VIS_NUMBER
    system, device, table = config
    kept = dict(VALUES[0x37])
    ADDRESS_FRAMES.clear()
    try:
        at_5 = dataclasses.replace(table.values[5], visualization=VIS_ACTION, minimum=0.0, maximum=1.0)  # 6 is a value
        system.write(device, table, at_5, "on", force=True, timeout=0.02)
        number = dataclasses.replace(table.values[7], visualization=VIS_NUMBER, minimum=0.0, maximum=1.0)  # no switch
        system.write(device, table, number, "1", force=True, timeout=0.02)
        wide = dataclasses.replace(table.values[7], visualization=VIS_ACTION, minimum=0.0, maximum=2.0)  # three states
        system.write(device, table, wide, "1", force=True, timeout=0.02)
    finally:
        VALUES[0x37].clear(), VALUES[0x37].update(kept)
    assert not ADDRESS_FRAMES


def test_a_writer_frame_refused_or_unanswered_is_warned_of(switch, bus):
    system, device, table, button = switch
    naming = lambda f: f.kind == 0x37 and len(f.data) == 6 and f.data[2] == 0x14  # noqa: E731
    undo = answering(bus, lambda kind, data: (kind + 0x10, data), to=naming)  # 'not available' instead of the echo
    try:
        written = system.write(device, table, button, "on", force=True, timeout=0.02)
    finally:
        undo()
    assert written.took and [w for w in written.warnings if has(w, "writer", "refused")]
    undo = answering(bus, lambda kind, data: None, to=naming)  # silence
    try:
        written = system.write(device, table, button, "off", force=True, timeout=0.02)
    finally:
        undo()
    assert written.took and [w for w in written.warnings if has(w, "writer")]
    assert not any(has(w, "refused") for w in written.warnings)


def test_name_writer_needs_a_page_with_a_read_kind(bus):
    r = PageReader(bus, SHUNT, reply_timeout=0.02, retries=0)
    ADDRESS_FRAMES.clear()
    assert r.name_writer(PAGE_BY_NAME["config"], 7) is True
    assert ADDRESS_FRAMES == [bytes([8, 0, 0x14, 0x34, 0x12, 0x00])]
    with pytest.raises(ValueError):
        r.name_writer(PAGE_BY_NAME["alarm"], 0)


def test_the_writer_is_named_into_a_listed_register_but_not_over_a_value(switch, bus):
    system, device, table, button = switch
    register = Value(8, name="", writeable=True, minimum=float("nan"), maximum=65535.0, step=65535.0, default=65535.0)
    assert register.is_writer_register
    for other in (dataclasses.replace(register, name="Timer"), dataclasses.replace(register, writeable=False),
                  dataclasses.replace(register, maximum=1.0), dataclasses.replace(register, default=0.0),
                  dataclasses.replace(register, minimum=0.0)):
        assert not other.is_writer_register
    values = dict(table.values)
    try:
        table.values[8] = register
        system.write(device, table, button, "off", force=True, timeout=0.02)
        assert ADDRESS_FRAMES == [bytes([8, 0, 0x14, 0x34, 0x12, 0x00])]
        ADDRESS_FRAMES.clear()
        table.values[8] = dataclasses.replace(register, name="Timer")
        system.write(device, table, button, "off", force=True, timeout=0.02)
        assert not ADDRESS_FRAMES
    finally:
        table.values.clear(), table.values.update(values)


def test_a_page_the_device_has_not_got_is_empty(bus, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    NO_COUNT.add(0x07)  # the alarm page's values: 'not available', as an EasyView answers
    try:
        r = PageReader(bus, SHUNT, reply_timeout=0.02, retries=0)
        table = r.read_page(PAGE_BY_NAME["alarm"])
        assert table.values == {} and table.groups == [] and table.version is None
        system = System(bus)
        device = system.device("msh", timeout=0.02)
        tables = system.tables(device, list(PAGES))
        assert sorted(tables) == ["alarm", "config", "history", "monitor"] and tables["alarm"].values == {}
    finally:
        NO_COUNT.discard(0x07)


def test_a_writer_register_shows_the_address_it_holds():
    register = Value(3, name="", writeable=True, minimum=float("nan"), maximum=65535.0, step=65535.0, default=65535.0)
    held = struct.unpack("<f", bytes([0x14, 0x4D, 0x04, 0x03]))[0]  # 14:3044d as the frame puts it
    assert Value.writer_address(held) == Address(0x14, 0x3044D) and register.format(held) == "14:3044d"
    assert register.format(0.0) == "0" and register.format(65535.0) == "65535"
    assert register.format(float("nan")) == "n/a"
    assert Value.writer_address(65535.0) is None and Value.writer_address(float("inf")) is None
    assert Value(3).format(held) == "0"  # a number is just a number, and a tiny one
