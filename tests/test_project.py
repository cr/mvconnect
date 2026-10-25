"""pyproject.toml agrees with itself: a raised Python floor is raised everywhere it is named."""

import tomllib
from pathlib import Path

PYPROJECT = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
FLOOR = PYPROJECT["project"]["requires-python"].removeprefix(">=")


def test_the_oldest_python_is_the_oldest_classifier_and_the_type_check_target():
    classified = [c.rsplit(" :: ", 1)[1] for c in PYPROJECT["project"]["classifiers"]
                  if c.startswith("Programming Language :: Python :: 3.")]
    assert min(classified, key=lambda v: tuple(map(int, v.split(".")))) == FLOOR
    assert PYPROJECT["tool"]["mypy"]["python_version"] == FLOOR
