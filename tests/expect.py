"""Checks that pin what a message is about, not how it is worded.

A test that expects an error, a warning or a line of output names the things it is about (a device, a
value, a number, a level) and leaves the sentence around them free, so that rewording a message, adding a
comma or changing case does not break tests that are not about wording. A thing is matched as a whole word
or number: 7 is not found in 17, nor "no" in "not".
"""

import re


def _pattern(thing) -> str:
    text = " ".join(str(thing).split())
    # a word or number continues through letters, digits and a decimal point, but not a full stop
    return ((r"(?<!\w)(?<!\w\.)" if re.match(r"\w", text) else "") + re.escape(text)
            + (r"(?!\w|\.\w)" if re.search(r"\w$", text) else ""))


def has(text: str, *things) -> bool:
    """Whether *text* mentions every thing, in any order and case, with runs of white space counted as one."""
    flat = " ".join(str(text).split())
    return all(re.search(_pattern(t), flat, re.I) for t in things)


def mentions(*things) -> str:
    """A pytest.raises match pattern: the message mentions every thing, in any order and case."""
    return "(?is)" + "".join(f"(?=.*{_pattern(t)})" for t in things)


def logged(caplog, level: str, *things) -> bool:
    """Whether a log record at *level* (WARNING, ERROR, INFO...) mentions every thing."""
    return any(r.levelname == level and has(r.getMessage(), *things) for r in caplog.records)


def line_with(text: str, *things) -> bool:
    """Whether one line of *text* mentions every thing: a line of output, a row of a table, or a log line on
    stderr, whose level is a thing like any other (line_with(err, "WARNING", "installer"))."""
    return any(has(line, *things) for line in str(text).splitlines())
