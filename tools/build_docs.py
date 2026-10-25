"""Generate the library reference from the docstrings, as Markdown.

The reference is docs/api/<module>.md for each module and docs/api/README.md as the index. Run with
`make docs`.

The command line, mvconnect.cli and mvconnect.firmware_cli, is left out; `mvconnect --help` documents it.
The modules are in the order of mvconnect.__all__.
"""

import importlib
import io
from pathlib import Path
from types import ModuleType

import docspec
from pydoc_markdown.contrib.loaders.python import PythonLoader
from pydoc_markdown.contrib.processors.filter import FilterProcessor
from pydoc_markdown.contrib.processors.smart import SmartProcessor
from pydoc_markdown.contrib.renderers.markdown import MarkdownRenderer
from pydoc_markdown.interfaces import Context

import mvconnect

OUT = Path("docs/api")
MODULES = [n for n in mvconnect.__all__ if isinstance(getattr(mvconnect, n), ModuleType)]  # the modules, not the names


def keep(obj: docspec.ApiObject) -> bool:
    """Tell whether *obj* belongs in the reference.

    It does if it has a docstring, and so do the fields and constants without one, which a user needs too;
    imports, loggers, type variables and undocumented functions do not. The members of a class or module are
    filtered the same way, in place.
    """
    if isinstance(obj, docspec.Indirection) or obj.name in ("logger", "T", "Progress"):
        return False
    if isinstance(obj, (docspec.Class, docspec.Module)):
        if isinstance(obj, docspec.HasMembers):
            obj.members = [m for m in obj.members if keep(m)]
        return True
    return obj.docstring is not None or isinstance(obj, docspec.Variable)


def render(module: str) -> str:
    """Return the Markdown reference of mvconnect.<module>."""
    context = Context(directory=".")
    loader = PythonLoader(search_path=["src"], modules=[f"mvconnect.{module}"])
    processors = [FilterProcessor(documented_only=False), SmartProcessor()]  # no private names; docstrings as written
    renderer = MarkdownRenderer(descriptive_class_title=False, render_typehint_in_data_header=True)
    for plugin in (loader, *processors, renderer):
        plugin.init(context)
    modules = [m for m in loader.load() if keep(m)]
    for processor in processors:
        processor.process(modules, None)
    out = io.StringIO()
    renderer.render_single_page(out, modules)
    return out.getvalue()


def summary(module: str) -> str:
    """Return the first sentence of the module's docstring."""
    doc = importlib.import_module(f"mvconnect.{module}").__doc__ or ""
    first = " ".join(doc.strip().split("\n\n")[0].split())
    return first.split(". ")[0].rstrip(".")


def main() -> None:
    """Write the reference of every module in mvconnect.__all__ and the index, replacing the old pages."""
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.md"):
        old.unlink()
    rows = []
    for module in MODULES:
        (OUT / f"{module}.md").write_text(render(module))
        rows.append(f"- [`mvconnect.{module}`]({module}.md): {summary(module)}")
    index = ("# mvconnect library reference\n\nGenerated from the docstrings by `make docs`; do not edit. The command "
             "line, `mvconnect`, is documented by `mvconnect --help`.\n\n" + "\n".join(rows) + "\n")
    (OUT / "README.md").write_text(index)
    print(f"{len(MODULES)} modules into {OUT}/")


if __name__ == "__main__":
    main()
