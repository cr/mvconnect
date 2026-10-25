# Build targets, run in the active Python environment (pip install -e '.[test,docs]'), or through pipenv
# when a Pipfile is there.

RUN ?= $(if $(wildcard Pipfile),pipenv run,)

.PHONY: docs test check lint

docs:  ## the library reference from the docstrings, as Markdown in docs/api/ (committed, GitHub renders it)
	$(RUN) python tools/build_docs.py

test:
	$(RUN) python -m pytest -q

check:  ## the type hints, checked
	$(RUN) python -m mypy

lint:  ## what ruff finds: errors, unused names, import order
	$(RUN) ruff check
