"""Print the oldest versions pyproject.toml allows, for CI to test against.

    python tools/floors.py --python    the oldest Python, from requires-python: 3.11
    python tools/floors.py             each dependency at its floor, as constraints: rich==13.5
    python tools/floors.py fireaid     the floors of an installed package's own dependencies: fire==0.2.0

A dependency without a lower bound is left out. Where markers apply, they are evaluated for the running Python,
and of several requirements on one name the highest floor counts. Run from the top of the repository; all but
--python need the packaging module.
"""

import re
import sys
import tomllib
from importlib import metadata

PROJECT = tomllib.load(open("pyproject.toml", "rb"))["project"]
LOWER = (">=", "==", "~=", "===")  # operators that set a floor


def python_floor() -> str:
    """Return the oldest Python that requires-python allows."""
    found = re.search(r">=\s*([\d.]+)", PROJECT["requires-python"])
    if not found:
        raise SystemExit(f"no lower bound in requires-python: {PROJECT['requires-python']!r}")
    return found.group(1)


def floors(requirements: list[str]) -> dict[str, str]:
    """Return the floor of each of *requirements* whose markers hold here, by name."""
    from packaging.requirements import Requirement
    from packaging.version import Version

    found: dict[str, str] = {}
    for text in requirements:
        req = Requirement(text)
        if req.marker and not req.marker.evaluate({"extra": ""}):
            continue  # another Python's, or an extra's
        for spec in req.specifier:
            if spec.operator in LOWER and (req.name not in found or Version(spec.version) > Version(found[req.name])):
                found[req.name] = spec.version
    return found


def main(args: list[str]) -> None:
    if args == ["--python"]:
        print(python_floor())
        return
    requirements = [r for name in args for r in metadata.requires(name) or []] if args else PROJECT["dependencies"]
    for name, version in floors(requirements).items():
        print(f"{name}=={version}")


if __name__ == "__main__":
    main(sys.argv[1:])
