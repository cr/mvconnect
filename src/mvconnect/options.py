"""Option values as typed on the command line.

fire hands a command an option's text as typed where the option is annotated str, but a bare `--set` as the
text "True", and parses an unannotated one as a Python literal: `--erase-size 0x800` arrives as 2048. For the
options whose text matters, a command reads it from the command line itself, which also tells a bare option
from one given the text "True", and which fire's `--` ends.
"""

from __future__ import annotations

import inspect
import re
import sys
from typing import Any

NUMBER = re.compile(r"-\.?\d")  # -5, -0.5, -.5: a value, not a flag
REMOTE = "--remote"  # fireaid's flag, taken out before the command runs: no command's word, wherever it stands


def _words(command: str) -> list[str]:
    """Return the words of the command line before fire's `--` when it runs *command*.

    *command* is the command's word, as in `data ...` or `firmware flash ...`. The list is empty when the
    command line is another program's, as when a command is called from Python under pytest. fireaid's
    --remote, with its value, is left out: an empty --remote= runs the command here.
    """
    words = sys.argv[1:]
    words = words[:words.index("--")] if "--" in words else words
    words = _without_remote(words)
    leading = []
    for w in words:
        if w.startswith("-"):
            break
        leading.append(w)
    return words if command in leading[:2] else []


def _without_remote(words: list[str]) -> list[str]:
    """Return *words* without fireaid's --remote and the value it takes, given as --remote=VALUE or --remote VALUE."""
    kept = []
    skip = False
    for word in words:
        if skip:
            skip = False
        elif word == REMOTE:
            skip = True  # its value is the next word; fireaid refuses the flag without one
        elif not word.startswith(REMOTE + "="):
            kept.append(word)
    return kept


def occurrences(name: str, short: str | None = None, *, command: str) -> list[str | None]:
    """Return the texts typed for the option --NAME, or its *short* form such as -s, in the order given.

    An occurrence given without a value is None. Only a command line that runs *command* counts, and only its
    words before fire's `--`.

    The texts come from sys.argv, as the shell passed them; the server runs each command with the command line
    it was given. A word after the option that starts with a dash is another option, not its value, unless it
    is a negative number; such a value goes as --NAME=VALUE.
    """
    flags = {"--" + name.replace("_", "-"), "--" + name} | ({short} if short else set())
    words = _words(command)
    found: list[str | None] = []
    for i, word in enumerate(words):
        flag, eq, text = word.partition("=")
        if flag not in flags:
            continue
        if eq:
            found.append(text)
        else:
            following = words[i + 1] if i + 1 < len(words) else None
            is_value = following is not None and (not following.startswith("-") or NUMBER.match(following))
            found.append(following if is_value else None)
    return found


def typed(name: str, given: Any, short: str | None = None, *, command: str) -> str | None:
    """Return the text typed for the option --NAME (or *short*) of *command*.

    The result is None when the option was given without a value. Where it is given more than once, the last
    counts, as with fire. Where it is not on the command line, as for a call from Python, *given* stands: as
    text, or None when *given* is None or a bare flag's True or False.
    """
    found = occurrences(name, short, command=command)
    if found:
        return found[-1]
    return None if given is None or isinstance(given, bool) else str(given)


def unknown_flags(routine: Any) -> list[str]:
    """Return the flags on the command line that *routine*, the command being run, does not take.

    fire calls a command with the flags it knows and only afterwards complains about the others, so a command
    that changes something checks first: `--set 13 --forse` must not write 13. Nothing is returned when the
    command line is not this command's, as for a call from Python; nor for anything after fire's `--`.
    """
    path = _words(routine.__name__)
    if not path:
        return []
    parameters = inspect.signature(routine).parameters
    names = {n for n, p in parameters.items() if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}
    switches = {n for n in names if isinstance(parameters[n].default, bool)}
    unknown = []
    for word in path:
        if not word.startswith("-") or word == "-" or NUMBER.match(word):
            continue
        if word.startswith("--"):
            name = word[2:].partition("=")[0].replace("-", "_")
            if name in names or (name.startswith("no") and name[2:] in switches) or name == "help":
                continue
        elif len(word) == 2 and (word == "-h" or sum(n.startswith(word[1]) for n in names) == 1):
            continue  # fire's short form: -s for the one option starting with s
        unknown.append(word)
    return unknown
