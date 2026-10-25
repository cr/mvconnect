"""Selection of devices and values by words.

Each thing has an id, a name and perhaps other labels: a device its selector
(hh:lllll), its name, its type and product name; a value its index, its name,
its long name and the names of the groups it sits in. Words select things in
this order of precedence:

1. A single word equal to a thing's id selects that thing alone.
2. Otherwise, a thing is selected if every word either equals its id or is
   the start of a word of its name or labels. `batt cap` selects Battery
   capacity, `battery low` selects Battery low and Battery pre low alike,
   and `battery low 22` the one of them at index 22; `apacity` selects
   nothing, since only the start of a word counts, and `2` never stands
   for 21.

Words are separated by spaces, punctuation and changes of case, in the names
and in the words typed: `Min/Max history` is the words min, max and history,
`AmbTempDerating` the words amb, temp and derating, and `Amp=Full` the words
amp and full. A name's words as written count too, so `ambtempderating`, typed
in lower case, selects AmbTempDerating. Case is ignored. A word of nothing
but punctuation, as `/` or `-`, is no word. No words select everything.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import TypeVar

PUNCTUATION = re.compile(r"[\s/\-:>=<()*.,;]+")  # where a name splits into words
CASE_CHANGE = re.compile(r"(?<=[a-z])(?=[A-Z])")  # a lower-case letter followed by an upper-case one
T = TypeVar("T")


def fold(text: str) -> str:
    """Return *text* in lower case with its whitespace collapsed, for an exact comparison."""
    return " ".join(str(text).split()).lower()


def tokens(text: str) -> list[str]:
    """Return the words of *text*, in lower case.

    Words split at punctuation and at changes of case: `'Min/Max history'` gives `['min', 'max', 'history']`,
    `'AmbTempDerating'` gives `['amb', 'temp', 'derating']`.
    """
    return [w.lower() for part in PUNCTUATION.split(str(text)) for w in CASE_CHANGE.split(part) if w]


def select(items: Iterable[T], words: Iterable[object], key: Callable[[T], str], name: Callable[[T], str],
           labels: Callable[[T], Iterable[str]] = lambda i: ()) -> list[T]:
    """Return the items that *words* select, in their original order, by the rules in the module docstring.

    *key*, *name* and *labels* are functions that return an item's id, name and other labels; *labels*
    defaults to none. Each word is converted to a string first, so a number may be passed as it is.
    """
    items = list(items)
    said = [str(w) for w in words if tokens(str(w))]
    if not said:
        return items
    if len(said) == 1 and (by_id := [i for i in items if fold(key(i)) == fold(said[0])]):
        return by_id

    def starts(i: T) -> list[str]:  # the words split at case changes, and as written
        texts = (name(i), *labels(i))
        return [t for text in texts for t in tokens(text)] + [w.lower() for text in texts
                                                              for w in PUNCTUATION.split(str(text)) if w]

    def matches(i: T, word: str) -> bool:  # a word typed with punctuation, as min/max, is split too
        return fold(word) == fold(key(i)) or all(any(t.startswith(p) for t in starts(i)) for p in tokens(word))

    return [i for i in items if all(matches(i, w) for w in said)]
