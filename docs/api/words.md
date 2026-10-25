<a id="mvconnect.words"></a>

# mvconnect.words

Selection of devices and values by words.

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

<a id="mvconnect.words.PUNCTUATION"></a>

#### PUNCTUATION

where a name splits into words

<a id="mvconnect.words.CASE_CHANGE"></a>

#### CASE\_CHANGE

a lower-case letter followed by an upper-case one

<a id="mvconnect.words.fold"></a>

#### fold

```python
def fold(text: str) -> str
```

Return *text* in lower case with its whitespace collapsed, for an exact comparison.

<a id="mvconnect.words.tokens"></a>

#### tokens

```python
def tokens(text: str) -> list[str]
```

Return the words of *text*, in lower case.

Words split at punctuation and at changes of case: `'Min/Max history'` gives `['min', 'max', 'history']`,
`'AmbTempDerating'` gives `['amb', 'temp', 'derating']`.

<a id="mvconnect.words.select"></a>

#### select

```python
def select(
    items: Iterable[T],
    words: Iterable[object],
    key: Callable[[T], str],
    name: Callable[[T], str],
    labels: Callable[[T], Iterable[str]] = lambda _: ()
) -> list[T]
```

Return the items that *words* select, in their original order, by the rules in the module docstring.

*key*, *name* and *labels* are functions that return an item's id, name and other labels; *labels*
defaults to none. Each word is converted to a string first, so a number may be passed as it is.

