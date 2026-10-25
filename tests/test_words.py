"""Picking devices and values by words: an exact id wins outright, then an exact name or label, then word
starts, with names split at punctuation and case changes."""

from dataclasses import dataclass

from mvconnect.words import fold, select, tokens


@dataclass
class V:
    index: int
    name: str
    long: str = ""
    groups: tuple[str, ...] = ()


# a page as the devices on the boat lay one out: names repeat, run together, carry punctuation
PAGE = [
    V(0, "Nominal voltage", "Nominal battery voltage", ("General",)),
    V(1, "Battery capacity", groups=("Battery",)),
    V(2, "Battery type", groups=("Battery",)),
    V(3, "Date"), V(4, "Date"),
    V(5, "Event 1 target", groups=("Events",)),
    V(6, "Event 1 command", groups=("Events",)),
    V(7, "AmbTempDerating", groups=("Charger",)),
    V(8, "BattTempComp", groups=("Charger",)),
    V(9, "Min/Max history", groups=("Min/Max history",)),
    V(10, "Amp=Full"),
    V(11, "HMA Reset"), V(12, "Reset setup"),
    V(13, "Battery", groups=("Battery 1",)),
    V(14, "Battery low", groups=("Alarm levels",)), V(15, "Battery pre low", groups=("Alarm levels",)),
    V(21, "Timer 1 on", groups=("Timers",)), V(22, "Timer 1 off", groups=("Timers",)),
]


def pick(*words):
    return [v.index for v in select(PAGE, words, lambda v: str(v.index), lambda v: v.name,
                                    lambda v: (v.long, *v.groups))]


def test_words_split_at_punctuation_and_case_changes():
    assert tokens("Min/Max history") == ["min", "max", "history"]
    assert tokens("AmbTempDerating") == ["amb", "temp", "derating"]
    assert tokens("ResetCount Rst.") == ["reset", "count", "rst"]
    assert tokens("Amp=Full") == ["amp", "full"]
    assert tokens("Current >100A") == ["current", "100a"]
    assert tokens("Li-ion") == ["li", "ion"]
    assert tokens("AGM/GEL float") == ["agm", "gel", "float"]  # no case change inside AGM
    assert fold("  Battery   Type ") == "battery type"


def test_an_exact_index_picks_that_and_nothing_else():
    assert pick("2") == [2]  # not 21, 22
    assert pick(21) == [21]  # fire passes numbers as ints
    assert pick("1", "1") == [1, 5, 6, 13, 21, 22]  # two words: each equals the index 1 or starts a word "1"
    assert pick("#2") == [] and pick("[2]") == []


def test_a_name_picks_the_values_of_that_name_and_those_whose_words_start_so():
    assert pick("date") == [3, 4]  # names repeat within a page
    assert pick("Battery", "type") == [2]  # typed as two words
    assert pick("battery") == [0, 1, 2, 13, 14, 15]  # the long name, the group Battery's values, Battery..., in table order
    assert pick("battery", "low") == [14, 15]  # Battery pre low has words starting with battery and low too
    assert pick("amp=full") == [10]  # a name with '=' in it is just a name


def test_an_index_among_the_words_pins_the_value():
    assert pick("battery", "low", "15") == [15]
    assert pick("battery", "low", "14") == [14]
    assert pick("battery", "low", "16") == []  # no such value among them
    assert pick("batt", "2") == [2]


def test_an_exact_group_name_picks_its_values_and_nothing_else():
    assert pick("timers") == [21, 22]
    assert pick("min/max", "history") == [9]
    assert pick("battery", "1") == [1, 13]  # the group Battery 1, and the index 1 among the Battery values


def test_otherwise_every_word_must_start_a_word_of_name_label_or_group():
    assert pick("batt") == [0, 1, 2, 8, 13, 14, 15]  # names, the long name, the Battery groups; BattTempComp by its case split
    assert pick("temp") == [7, 8]  # inside AmbTempDerating and BattTempComp, at a case change
    assert pick("comp") == [8]
    assert pick("derat") == [7]
    assert pick("apacity") == []  # only the start of a word counts
    assert pick("ev") == [5, 6]
    assert pick("event", "1", "comm") == [6]
    assert pick("nominal", "batt") == [0]  # the long name counts too
    assert pick("tim", "off") == [22]
    assert pick("reset") == [11, 12]  # a verb-looking word is a selector word like any other
    assert pick("hma", "reset") == [11]


def test_a_run_together_name_is_found_as_written_too():
    assert pick("ambtempderating") == [7] and pick("batttempcomp") == [8]  # typed in lower case
    assert pick("batttemp") == [8]  # its start
    assert pick("AmbTempDerating") == [7]  # as the device writes it


def test_an_index_never_prefix_matches():
    assert pick("2", "1") == []  # neither "2 1" nor words of a name
    assert pick("0") == [0] and pick("00") == []


def test_no_words_pick_everything_and_no_match_picks_nothing():
    assert len(select(PAGE, [], lambda v: str(v.index), lambda v: v.name)) == len(PAGE)
    assert select(PAGE, ["", " "], lambda v: str(v.index), lambda v: v.name) == PAGE
    assert pick("zzz") == []


def test_a_word_of_only_punctuation_is_no_word():
    assert pick("battery", "/", "low") == pick("battery", "low") == [14, 15]
    assert pick("-") == pick()


def test_devices_pick_by_selector_name_type_or_product_name():
    @dataclass
    class D:
        selector: str
        name: str
        kind: str
        product_name: str

    bus = [D("1b:0cad9", "House Bank", "MSH", "MasterShunt"), D("0a:1062b", "Charger", "CHG", "ChargeMaster"),
           D("0c:1a760", "APR Generator", "APR", "Alpha Pro")]
    found = lambda *w: [d.selector for d in select(bus, w, lambda d: d.selector, lambda d: d.name,
                                                   lambda d: (d.kind, d.product_name))]
    assert found("1B:0CAD9") == ["1b:0cad9"]
    assert found("msh") == ["1b:0cad9"] and found("alpha", "pro") == ["0c:1a760"]
    assert found("house") == ["1b:0cad9"]  # a word start is enough when it is unique
    assert found("charge") == ["0a:1062b"]  # Charger and ChargeMaster, the same device
    assert found("master") == ["1b:0cad9", "0a:1062b"]  # MasterShunt, ChargeMaster: the caller refuses two
    assert found("gen") == ["0c:1a760"]
